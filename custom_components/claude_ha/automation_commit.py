"""Create and manage Home Assistant automations, safely.

Creating one from a model-drafted config, and finding + deleting existing ones. The
drafted config is MODEL-AUTHORED, so this module is the trust boundary. It
re-validates the draft against Home Assistant's own automation schema, enforces a
strict reject-not-filter action allowlist (every service call reachable through ANY
nesting must be a literal call to an allowed domain), then persists and reloads it
in-process. Nothing here trusts the drafting side; a rejected or failed commit
raises :class:`ClaudeError` so the caller renders one clean chat message — it never
raises past the caller.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import re
from typing import Any
import uuid

import voluptuous as vol

from homeassistant.components.automation.config import async_validate_config_item
from homeassistant.components.automation.const import DOMAIN as AUTOMATION_DOMAIN
from homeassistant.config import AUTOMATION_CONFIG_PATH
from homeassistant.const import (
    ATTR_AREA_ID,
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    ATTR_FLOOR_ID,
    ATTR_FRIENDLY_NAME,
    ATTR_LABEL_ID,
    CONF_ID,
    ENTITY_MATCH_ALL,
    SERVICE_RELOAD,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.config_validation import (
    SCRIPT_ACTION_ACTIVATE_SCENE,
    SCRIPT_ACTION_CALL_SERVICE,
    SCRIPT_ACTION_CHECK_CONDITION,
    SCRIPT_ACTION_CHOOSE,
    SCRIPT_ACTION_DELAY,
    SCRIPT_ACTION_IF,
    SCRIPT_ACTION_PARALLEL,
    SCRIPT_ACTION_REPEAT,
    SCRIPT_ACTION_SEQUENCE,
    SCRIPT_ACTION_SET_CONVERSATION_RESPONSE,
    SCRIPT_ACTION_STOP,
    SCRIPT_ACTION_VARIABLES,
    SCRIPT_ACTION_WAIT_FOR_TRIGGER,
    SCRIPT_ACTION_WAIT_TEMPLATE,
    determine_script_action,
)
from homeassistant.helpers.template import Template
from homeassistant.util.file import write_utf8_file_atomic
from homeassistant.util.yaml import dump, load_yaml

from .api import ClaudeError

# The GATE (reject-not-filter): a Claude-created automation may only ACTUATE the
# user's own entities in these domains — every service call AND every entity it
# targets must be in this set. Kept to user-facing device / helper / notify domains;
# deliberately EXCLUDES homeassistant.* / script.* / group.* (generic/indirect
# actuation across domains) — a drafted automation uses the specific domain service.
# Deliberate carve-out: activating the user's OWN pre-defined scene (scene.turn_on /
# the `scene:` shorthand) is allowed even though the scene's stored states may touch
# any domain — it is the user's own config, confirm-gated, and its contents can't be
# bounded at commit (same TOCTOU class as area targeting).
# SOUNDNESS BOUNDARY: adding a domain here REQUIRES re-auditing that domain's
# services.yaml for (1) non-entity danger surfaces (url/command/file) and (2) entity
# references under non-standard data keys (see _ENTITY_DATA_KEYS) — else an escape.
_ALLOWED_DOMAINS: frozenset[str] = frozenset(
    {
        "light",
        "switch",
        "fan",
        "cover",
        "climate",
        "scene",
        "media_player",
        "lock",
        "notify",
        "persistent_notification",
        "input_boolean",
        "input_number",
        "input_text",
        "input_select",
        "input_datetime",
        "input_button",
        "timer",
        "counter",
        "button",
        "number",
        "select",
        "humidifier",
        "vacuum",
    }
)

# Rejected services in an otherwise-allowed domain whose danger is NON-entity — a
# payload the entity-reach rule can't bound: notify.file (arbitrary file write),
# media_player.play_media (arbitrary media_content_id URL fetch), vacuum.send_command
# (raw device command). Cross-domain-via-payload services (scene.apply/create's inline
# entity map) are NOT here — the entity-reach rule subsumes them (their entities get
# domain-checked like any other target). An audit of every allowed domain's
# services.yaml found no other non-entity danger surface.
_DENIED_SERVICES: frozenset[str] = frozenset(
    {"notify.file", "media_player.play_media", "vacuum.send_command"}
)

# The blocks HA merges into a service call whose contents must be inspected: the
# target selector, `data`, and the legacy `data_template` (both data keys are merged
# at runtime). Each must be a literal dict — a whole-block template can't be bounded.
_TARGET_DATA_KEYS = ("target", "data", "data_template")

# Target-selector keys that expand to entities of ANY domain via the live registries,
# with membership mutable AFTER commit (TOCTOU) — unbounded, so reject outright.
_TARGET_SELECTOR_KEYS = (ATTR_DEVICE_ID, ATTR_AREA_ID, ATTR_FLOOR_ID, ATTR_LABEL_ID)

# Data keys (besides target/entity_id) that carry ENTITY references on an allowed-
# domain service — audited exhaustively across the allow-list's services.yaml:
#   scene.apply/create `entities` (dict KEYED by entity_id), scene.create
#   `snapshot_entities` (list), media_player.join `group_members` (list).
# Re-audit when ALLOWED_DOMAINS changes (see the soundness-boundary note above).
_ENTITY_DATA_LIST_KEYS = ("snapshot_entities", "group_members")
_ENTITY_DATA_MAP_KEY = "entities"

# Action TYPES that neither invoke a service nor nest further actions — safe leaves.
# Anything not here, not a service call, and not a container is REJECTED (so a
# device action, an event, or a future/unknown action type can't slip through).
_SAFE_LEAF_ACTIONS: frozenset[str] = frozenset(
    {
        SCRIPT_ACTION_ACTIVATE_SCENE,
        SCRIPT_ACTION_CHECK_CONDITION,
        SCRIPT_ACTION_DELAY,
        SCRIPT_ACTION_SET_CONVERSATION_RESPONSE,
        SCRIPT_ACTION_STOP,
        SCRIPT_ACTION_VARIABLES,
        SCRIPT_ACTION_WAIT_FOR_TRIGGER,
        SCRIPT_ACTION_WAIT_TEMPLATE,
    }
)

# Keys holding a service NAME on a call-service action (str or Template). Validation
# renames the legacy `service` key to `action`, so `service` never survives into the
# walked (validated) tree; it is kept here as defense-in-depth so the walker stays
# safe even if ever called on a non-validated config.
_SERVICE_KEYS = ("action", "service_template", "service")

# Serialize OUR writers to automations.yaml. (Does not lock against the frontend
# editor; commits are rare and explicitly user-confirmed.)
_STORE_LOCK = asyncio.Lock()


def _walk_actions(actions: list[Any]) -> None:
    """Walk an action list, enforcing the payload-aware policy at every service call.

    Mirrors Home Assistant's own action grammar so nesting can't hide a call. Raises
    :class:`ClaudeError` on the first violation, or on an action type that is neither
    a safe leaf, a service call, nor a known container — reject-by-default.
    """
    for action in actions:
        kind = _script_action_kind(action)
        if kind == SCRIPT_ACTION_CALL_SERVICE:
            _check_call_service(action)
        elif kind == SCRIPT_ACTION_CHOOSE:
            for choice in action.get("choose", []):
                _walk_actions(choice.get("sequence", []))
            _walk_actions(action.get("default") or [])
        elif kind == SCRIPT_ACTION_IF:
            _walk_actions(action.get("then", []))
            _walk_actions(action.get("else") or [])
        elif kind == SCRIPT_ACTION_REPEAT:
            _walk_actions(action.get("repeat", {}).get("sequence", []))
        elif kind == SCRIPT_ACTION_PARALLEL:
            for item in action.get("parallel", []):
                _walk_actions(item.get("sequence", []))
        elif kind == SCRIPT_ACTION_SEQUENCE:
            _walk_actions(action.get("sequence", []))
        elif kind not in _SAFE_LEAF_ACTIONS:
            raise ClaudeError(
                f"The drafted automation uses an action type ('{kind}') that isn't "
                "permitted for a Claude-created automation; not created."
            )


def _script_action_kind(action: dict[str, Any]) -> str:
    """Classify one action the way HA does; an undeterminable action is rejected."""
    try:
        return determine_script_action(action)
    except ValueError as err:
        raise ClaudeError(
            "The drafted automation has an unrecognized action; not created."
        ) from err


def _is_templated(value: Any) -> bool:
    """Whether a value is a Jinja template — a Template object OR a ``{{``/``{%`` str.

    Service data is not schema-validated at config time, so a templated data value
    stays a plain str (never coerced to Template); both forms must be caught.
    """
    if isinstance(value, Template):
        return True
    return isinstance(value, str) and ("{{" in value or "{%" in value)


def _check_call_service(action: dict[str, Any]) -> None:
    """Enforce the payload-aware policy on one call-service action.

    Rejects a templated/denied/out-of-domain service; a device/area/floor/label
    target (unbounded, TOCTOU); and any entity target that is not a literal
    ``domain.object_id`` in an allowed domain (templated, ``all``, uuid, or a
    non-allowed domain). A templated NON-target data value (e.g. a message) is fine,
    but a whole ``target``/``data``/``data_template`` block that is itself a template
    (a non-dict) is rejected — its contents can't be statically bounded.
    """
    _check_service_name(action)
    refs: list[Any] = []
    # Top-level (legacy) selectors/entity_id live directly on the action dict.
    _reject_broad_selectors(action)
    if ATTR_ENTITY_ID in action:
        refs.append(action[ATTR_ENTITY_ID])
    # Both `data` and the legacy `data_template` are merged into the call at runtime,
    # as is `target`; inspect all three. A block that isn't a literal dict (e.g. a
    # whole-block Jinja template coerced to a Template, or a "{{ … }}" string) hides
    # its entity reach -> reject.
    for key in _TARGET_DATA_KEYS:
        block = action.get(key)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise ClaudeError(
                f"The drafted automation has a templated '{key}' block, which can't "
                "be verified as safe; not created."
            )
        _reject_broad_selectors(block)
        if ATTR_ENTITY_ID in block:
            refs.append(block[ATTR_ENTITY_ID])
        _collect_scene_entity_refs(block, refs)
    for value in refs:
        _check_entity_ref(value)


def _reject_broad_selectors(container: dict[str, Any]) -> None:
    """Reject device/area/floor/label targeting — it can reach any domain, mutably."""
    if any(key in container for key in _TARGET_SELECTOR_KEYS):
        raise ClaudeError(
            "The drafted automation targets a device or area rather than specific "
            "entities, which can't be bounded to safe domains; not created."
        )


def _collect_scene_entity_refs(block: dict[str, Any], refs: list[Any]) -> None:
    """Add the entity references a scene/join payload names (their KEYS/list values).

    A scene ``entities`` map that isn't a literal dict (a template) hides its keys ->
    reject; a templated list value is caught later by :func:`_check_entity_ref`.
    """
    state_map = block.get(_ENTITY_DATA_MAP_KEY)
    if state_map is not None:
        if not isinstance(state_map, dict):
            raise ClaudeError(
                "The drafted automation has a templated 'entities' map, which can't "
                "be verified as safe; not created."
            )
        refs.extend(state_map.keys())  # entity_id KEYS only (states are values)
    for key in _ENTITY_DATA_LIST_KEYS:
        if key in block:
            refs.append(block[key])


def _check_service_name(action: dict[str, Any]) -> None:
    """Require a literal ``domain.service`` in an allowed, non-denied domain."""
    for key in _SERVICE_KEYS:
        if key not in action:
            continue
        name = action[key]
        if _is_templated(name):
            raise ClaudeError(
                "The drafted automation uses a templated service name, which can't "
                "be verified as safe; not created."
            )
        service = str(name)
        domain, _, obj = service.partition(".")
        if not obj or domain not in _ALLOWED_DOMAINS or service in _DENIED_SERVICES:
            raise ClaudeError(
                f"The drafted automation calls '{service}', which isn't allowed for a "
                "Claude-created automation; not created."
            )


def _check_entity_ref(value: Any) -> None:
    """Every reached entity must be a literal ``domain.object_id`` in an allowed domain.

    Rejects a templated ref, the ``all`` wildcard, a uuid / any non-``domain.object_id``
    token, or an entity in a non-allowed domain. Handles str, comma-string and list.
    """
    if _is_templated(value):
        raise ClaudeError(
            "The drafted automation targets a templated entity, which can't be "
            "verified as safe; not created."
        )
    tokens: list[str] = []
    if isinstance(value, str):
        tokens = value.split(",")
    elif isinstance(value, list):
        for element in value:
            if _is_templated(element):
                raise ClaudeError(
                    "The drafted automation targets a templated entity, which can't "
                    "be verified as safe; not created."
                )
            if not isinstance(element, str):
                raise ClaudeError(
                    "The drafted automation has an unrecognized entity target; "
                    "not created."
                )
            tokens.extend(element.split(","))
    else:
        raise ClaudeError(
            "The drafted automation has an unrecognized entity target; not created."
        )
    for token in tokens:
        entity = token.strip()
        domain, _, obj = entity.partition(".")
        if entity == ENTITY_MATCH_ALL or not obj or domain not in _ALLOWED_DOMAINS:
            raise ClaudeError(
                f"The drafted automation targets '{entity}', which isn't a specific "
                "entity in an allowed domain; not created."
            )


def _enforce_action_policy(validated: dict[str, Any]) -> None:
    """Reject the commit unless every action's service AND entity reach are allowed."""
    _walk_actions(validated.get("actions", []))


def _read_store(path: str) -> list[Any]:
    """Load the current automations.yaml list (missing/empty/other -> [])."""
    if not os.path.isfile(path):
        return []
    data = load_yaml(path)
    return data if isinstance(data, list) else []


def _write_store(path: str, data: list[Any]) -> None:
    """Serialize then atomically write the automations list."""
    contents = dump(data)  # serialize BEFORE opening the file (no truncate on error)
    write_utf8_file_atomic(path, contents)


async def _validate_and_check(
    hass: HomeAssistant, config: dict[str, Any], config_key: str
) -> None:
    """Re-validate a model-authored config and enforce the action allowlist.

    Raises :class:`ClaudeError` on a blueprint draft, a schema failure, or a
    disallowed/dangerous/templated service. Shared by create and modify.
    """
    # A blueprint-based automation sources its actions from a blueprint, so the
    # action allowlist can't see (or bound) them — refuse it outright.
    if "use_blueprint" in config:
        raise ClaudeError(
            "Blueprint-based automations can't be managed this way; not saved."
        )
    try:
        validated = await async_validate_config_item(hass, config_key, config)
    except (vol.Invalid, HomeAssistantError) as err:
        raise ClaudeError(f"The automation isn't valid: {err}") from err
    except Exception as err:  # never surface a raw traceback to chat
        raise ClaudeError("The automation could not be validated; not saved.") from err
    if validated is None:
        raise ClaudeError("The automation could not be validated; not saved.")
    _enforce_action_policy(validated)


async def _persist_automation(
    hass: HomeAssistant, config: dict[str, Any], config_id: str
) -> str:
    """Write ``config`` under ``config_id`` (replacing any entry with that id).

    Our ``config_id`` ALWAYS wins: any model-supplied ``id`` is dropped, so a config
    can never target a different automation than the one intended. Returns the alias.
    """
    alias = str(config.get("alias") or "automation").strip() or "automation"
    body = {key: value for key, value in config.items() if key != CONF_ID}
    path = hass.config.path(AUTOMATION_CONFIG_PATH)
    try:
        async with _STORE_LOCK:
            current = await hass.async_add_executor_job(_read_store, path)
            current = [
                item
                for item in current
                if not (isinstance(item, dict) and item.get(CONF_ID) == config_id)
            ]
            current.append({**body, CONF_ID: config_id})
            await hass.async_add_executor_job(_write_store, path, current)
        await hass.services.async_call(
            AUTOMATION_DOMAIN, SERVICE_RELOAD, {CONF_ID: config_id}, blocking=True
        )
    except (HomeAssistantError, OSError) as err:
        raise ClaudeError(f"Couldn't save the automation: {err}") from err
    return alias


async def async_commit_automation(hass: HomeAssistant, config: dict[str, Any]) -> str:
    """Validate, security-check, persist and reload a NEWLY drafted automation.

    A fresh id is minted, so a create can never overwrite an existing automation.
    Raises :class:`ClaudeError` on any validation/policy/IO failure (nothing written).
    """
    config_key = uuid.uuid4().hex
    await _validate_and_check(hass, config, config_key)
    return await _persist_automation(hass, config, config_key)


async def async_update_automation(
    hass: HomeAssistant, config: dict[str, Any], target_id: str
) -> str:
    """Validate, security-check and write an updated config over ``target_id``.

    Same security bar as create; the id is the caller-resolved target (never a mint
    or a model-supplied value), so a modify updates exactly the intended automation
    in place. Raises :class:`ClaudeError` on any failure (nothing written).
    """
    await _validate_and_check(hass, config, target_id)
    return await _persist_automation(hass, config, target_id)


async def async_read_automation_config(
    hass: HomeAssistant, config_id: str
) -> dict[str, Any] | None:
    """Return the stored config (minus its id) of the automation with ``config_id``.

    Used to give the model the REAL automation to edit; None if it isn't in the store.
    """
    path = hass.config.path(AUTOMATION_CONFIG_PATH)
    current = await hass.async_add_executor_job(_read_store, path)
    for item in current:
        if isinstance(item, dict) and item.get(CONF_ID) == config_id:
            return {key: value for key, value in item.items() if key != CONF_ID}
    return None


# --- Find + delete existing automations -------------------------------------

# Filler words dropped before matching, so "delete my morning lights" matches the
# automation named "Morning lights". Kept small + multi-lingual (en/uk/pl).
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "my",
        "your",
        "this",
        "that",
        "please",
        "named",
        "called",
        "мою",
        "моя",
        "мій",
        "цю",
        "це",
        "будь",
        "ласка",
        "яка",
        "під",
        "назвою",
        "moja",
        "moje",
        "moj",
        "moją",
        "to",
        "ta",
        "ten",
        "o",
        "nazwie",
    }
)

# Minimum share of the query's words that must appear in an automation's name for
# it to count as a match. Above 0.5 so a single shared common word ("lights") in a
# multi-word query doesn't qualify an unrelated automation. The mandatory confirm is
# the real safety net; this just keeps unrelated automations out of the candidates.
_MATCH_THRESHOLD = 0.6


@dataclass(slots=True)
class AutomationMatch:
    """An existing automation that a delete/modify query resolved to."""

    entity_id: str
    config_id: str
    name: str
    score: float


def _tokens(text: str) -> set[str]:
    """Lowercase word tokens with punctuation stripped and filler words removed."""
    words = re.findall(r"[^\W_]+", text.lower(), flags=re.UNICODE)
    return {word for word in words if word not in _STOPWORDS}


def _match_score(query_tokens: set[str], name: str) -> float:
    """Share of query tokens present in ``name`` (0.0 when either side is empty)."""
    name_tokens = _tokens(name)
    if not query_tokens or not name_tokens:
        return 0.0
    return len(query_tokens & name_tokens) / len(query_tokens)


def find_automations(hass: HomeAssistant, query: str) -> list[AutomationMatch]:
    """Return existing automations whose name matches ``query``, best first.

    Only automations that carry a config id (deletable via automations.yaml) are
    considered. An empty/whitespace query returns no matches, so the caller asks
    which one rather than acting.
    """
    query_tokens = _tokens(query)
    if not query_tokens:
        return []
    matches: list[AutomationMatch] = []
    for state in hass.states.async_all(AUTOMATION_DOMAIN):
        config_id = state.attributes.get(CONF_ID)
        if not config_id:
            continue  # no id -> not in automations.yaml, can't delete by id
        name = state.attributes.get(ATTR_FRIENDLY_NAME) or state.name or ""
        score = _match_score(query_tokens, name)
        if score >= _MATCH_THRESHOLD:
            matches.append(
                AutomationMatch(state.entity_id, str(config_id), name, score)
            )
    matches.sort(key=lambda match: match.score, reverse=True)
    return matches


async def async_delete_automation(hass: HomeAssistant, config_id: str) -> None:
    """Remove an automation by its config id: drop from the store, unregister, reload.

    Raises :class:`ClaudeError` if the id is no longer present or the write/reload
    fails, so the caller renders one clean chat error.
    """
    path = hass.config.path(AUTOMATION_CONFIG_PATH)
    try:
        async with _STORE_LOCK:
            current = await hass.async_add_executor_job(_read_store, path)
            remaining = [
                item
                for item in current
                if not (isinstance(item, dict) and item.get(CONF_ID) == config_id)
            ]
            if len(remaining) == len(current):
                raise ClaudeError("That automation no longer exists; nothing deleted.")
            await hass.async_add_executor_job(_write_store, path, remaining)
        # Drop the (now orphaned) entity, then reload so the running automation stops.
        entity_id = er.async_get(hass).async_get_entity_id(
            AUTOMATION_DOMAIN, AUTOMATION_DOMAIN, config_id
        )
        if entity_id is not None:
            er.async_get(hass).async_remove(entity_id)
        await hass.services.async_call(
            AUTOMATION_DOMAIN, SERVICE_RELOAD, {CONF_ID: config_id}, blocking=True
        )
    except (HomeAssistantError, OSError) as err:
        raise ClaudeError(f"Couldn't delete the automation: {err}") from err

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
from homeassistant.const import ATTR_FRIENDLY_NAME, CONF_ID, SERVICE_RELOAD
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

# The GATE (reject-not-filter): a Claude-created automation may only call services in
# these domains. Any service outside this set rejects the WHOLE commit. Kept to
# user-facing device / helper / notify domains; deliberately EXCLUDES homeassistant.*
# (its generic turn_on/off live alongside restart/stop/reload_*) — a drafted
# automation uses the specific domain service (light.turn_on, cover.close_cover, …).
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

# Rejected services even though their domain is otherwise allowed — each carries a
# danger in its PAYLOAD that a name-only domain gate can't bound:
#  - notify.file writes an arbitrary file.
#  - scene.apply / scene.create reproduce arbitrary states on entities in ANY domain
#    (disarm an alarm, unlock a lock) via an inline `data.entities` map.
#  - media_player.play_media fetches an arbitrary `media_content_id` URL (SSRF-class
#    / plays attacker audio on the user's speakers).
#  - vacuum.send_command relays an attacker-chosen raw command straight to the device.
# (Activating a user-defined scene via scene.turn_on, or media_player.play/pause/
#  volume, or vacuum.start, all stay allowed — the danger is the payload service.)
_DENIED_SERVICES: frozenset[str] = frozenset(
    {
        "notify.file",
        "scene.apply",
        "scene.create",
        "media_player.play_media",
        "vacuum.send_command",
    }
)

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


def _collect_services(actions: list[Any], found: set[str]) -> None:
    """Walk an action list, adding every literal service reachable through it.

    Mirrors Home Assistant's own action grammar so nesting can't hide a call.
    Raises :class:`ClaudeError` on a templated service name (can't be statically
    bounded) or an action type that is neither a safe leaf, a service call, nor a
    known container — reject-by-default.
    """
    for action in actions:
        kind = _script_action_kind(action)
        if kind == SCRIPT_ACTION_CALL_SERVICE:
            _collect_leaf_service(action, found)
        elif kind == SCRIPT_ACTION_CHOOSE:
            for choice in action.get("choose", []):
                _collect_services(choice.get("sequence", []), found)
            _collect_services(action.get("default") or [], found)
        elif kind == SCRIPT_ACTION_IF:
            _collect_services(action.get("then", []), found)
            _collect_services(action.get("else") or [], found)
        elif kind == SCRIPT_ACTION_REPEAT:
            _collect_services(action.get("repeat", {}).get("sequence", []), found)
        elif kind == SCRIPT_ACTION_PARALLEL:
            for item in action.get("parallel", []):
                _collect_services(item.get("sequence", []), found)
        elif kind == SCRIPT_ACTION_SEQUENCE:
            _collect_services(action.get("sequence", []), found)
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


def _collect_leaf_service(action: dict[str, Any], found: set[str]) -> None:
    """Add the literal service of a call-service action; reject a templated one.

    A call-service action always carries exactly one service-name key (Home
    Assistant's schema makes ``action`` / ``service_template`` mutually exclusive
    and required), so there is always one name to check.
    """
    for key in _SERVICE_KEYS:
        if key not in action:
            continue
        name = action[key]
        if isinstance(name, Template):
            raise ClaudeError(
                "The drafted automation uses a templated service name, which can't "
                "be verified as safe; not created."
            )
        found.add(str(name))


def _enforce_action_policy(validated: dict[str, Any]) -> None:
    """Reject the commit unless every reachable service is allow-listed and literal."""
    found: set[str] = set()
    _collect_services(validated.get("actions", []), found)
    for service in sorted(found):
        domain, _, name = service.partition(".")
        if not name or domain not in _ALLOWED_DOMAINS or service in _DENIED_SERVICES:
            raise ClaudeError(
                f"The drafted automation calls '{service}', which isn't allowed for a "
                "Claude-created automation; not created."
            )


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

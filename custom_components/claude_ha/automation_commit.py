"""Commit a model-drafted automation into Home Assistant, safely.

The drafted config is MODEL-AUTHORED, so this module is the trust boundary. It
re-validates the draft against Home Assistant's own automation schema, enforces a
strict reject-not-filter action allowlist (every service call reachable through ANY
nesting must be a literal call to an allowed domain), then persists and reloads it
in-process. Nothing here trusts the drafting side; a rejected or failed commit
raises :class:`ClaudeError` so the caller renders one clean chat message — it never
raises past the caller.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
import uuid

import voluptuous as vol

from homeassistant.components.automation.config import async_validate_config_item
from homeassistant.components.automation.const import DOMAIN as AUTOMATION_DOMAIN
from homeassistant.config import AUTOMATION_CONFIG_PATH
from homeassistant.const import CONF_ID, SERVICE_RELOAD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
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


async def async_commit_automation(hass: HomeAssistant, config: dict[str, Any]) -> str:
    """Validate, security-check, persist and reload a drafted automation.

    Returns the created automation's alias. Raises :class:`ClaudeError` on ANY
    validation, policy or IO failure so the caller renders one clean chat error and
    nothing is written when the draft is rejected.
    """
    config_key = uuid.uuid4().hex

    # 0. A blueprint-based automation sources its actions from a blueprint, so the
    #    action allowlist below can't see (or bound) them — refuse it outright.
    if "use_blueprint" in config:
        raise ClaudeError(
            "Blueprint-based automations can't be created this way; not created."
        )

    # 1. Re-validate with HA's own automation schema (raise-on-error). Trust nothing.
    try:
        validated = await async_validate_config_item(hass, config_key, config)
    except (vol.Invalid, HomeAssistantError) as err:
        raise ClaudeError(f"The drafted automation isn't valid: {err}") from err
    except Exception as err:  # never surface a raw traceback to chat
        raise ClaudeError(
            "The drafted automation could not be validated; not created."
        ) from err
    if validated is None:
        raise ClaudeError("The drafted automation could not be validated; not created.")

    # 2. Strict reject-not-filter action allowlist over the validated action tree.
    _enforce_action_policy(validated)

    # 3. Persist the raw draft and reload just this automation. Our minted id ALWAYS
    #    wins: any model-supplied `id` is dropped before the mint is set last, so a
    #    draft can never target (and silently overwrite) an existing automation.
    alias = str(config.get("alias") or "automation").strip() or "automation"
    stored = {key: value for key, value in config.items() if key != CONF_ID}
    path = hass.config.path(AUTOMATION_CONFIG_PATH)
    try:
        async with _STORE_LOCK:
            current = await hass.async_add_executor_job(_read_store, path)
            current.append({**stored, CONF_ID: config_key})
            await hass.async_add_executor_job(_write_store, path, current)
        await hass.services.async_call(
            AUTOMATION_DOMAIN, SERVICE_RELOAD, {CONF_ID: config_key}, blocking=True
        )
    except (HomeAssistantError, OSError) as err:
        raise ClaudeError(f"Couldn't save the automation: {err}") from err

    return alias

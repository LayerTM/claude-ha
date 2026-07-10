"""Tests for the model-drafted automation commit path (validate + allowlist + write).

The heart of this is the security boundary: a model-authored config must be rejected
if any service call reachable through ANY nesting is outside the domain allowlist, is
a dangerous service, is templated, or is an unpermitted action type. The
hostile-input matrix below proves nesting can't smuggle a call past the walker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from custom_components.claude_ha.api import ClaudeError
from custom_components.claude_ha.automation_commit import (
    _enforce_action_policy,
    async_commit_automation,
    async_delete_automation,
    find_automations,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util.yaml import load_yaml


def _actions(tree: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap an action list as the minimal shape the policy checker consumes."""
    return {"actions": tree}


# Benign action trees (every container form, all leaves in allowed domains).
BENIGN_TREES: list[list[dict[str, Any]]] = [
    [{"action": "light.turn_on", "target": {"entity_id": "light.x"}}],
    [{"service_template": "switch.turn_on"}],  # literal legacy key is fine
    [
        {
            "choose": [{"conditions": [], "sequence": [{"action": "switch.turn_on"}]}],
            "default": [{"action": "light.turn_off"}],
        }
    ],
    [
        {
            "if": [],
            "then": [{"action": "fan.turn_on"}],
            "else": [{"action": "cover.open_cover"}],
        }
    ],
    [{"repeat": {"count": 3, "sequence": [{"action": "light.toggle"}]}}],
    [{"parallel": [{"sequence": [{"action": "scene.turn_on"}]}]}],
    [{"sequence": [{"action": "notify.mobile_app_phone"}]}],
    [{"delay": "00:01:00"}, {"stop": "done"}, {"variables": {"x": 1}}],
    [{"scene": "scene.night"}, {"wait_template": "{{ true }}"}],
    # allowed domains keep their safe services even though one service is denied
    [{"action": "media_player.media_pause"}, {"action": "vacuum.start"}],
]

# Dangerous action trees — each must be REJECTED. Every nesting site is covered, plus
# dangerous domains, a denied in-domain service, legacy key, and non-service types.
DANGEROUS_TREES: list[list[dict[str, Any]]] = [
    [{"action": "shell_command.run"}],
    [{"action": "python_script.exec"}],
    [{"action": "homeassistant.restart"}],
    [{"action": "hassio.host_reboot"}],
    [{"action": "mqtt.publish", "data": {"topic": "x"}}],
    [
        {"action": "notify.file", "data": {"message": "x"}}
    ],  # denied service, allowed domain
    [{"service_template": "shell_command.run"}],  # literal legacy key, bad domain
    [{"choose": [{"conditions": [], "sequence": [{"action": "shell_command.run"}]}]}],
    [
        {
            "choose": [{"conditions": [], "sequence": [{"action": "light.turn_on"}]}],
            "default": [{"action": "python_script.exec"}],  # hides in choose default
        }
    ],
    [{"if": [], "then": [{"action": "shell_command.run"}]}],
    [
        {
            "if": [],
            "then": [{"action": "light.turn_on"}],
            "else": [{"action": "shell_command.run"}],
        }
    ],
    [{"repeat": {"sequence": [{"action": "hassio.host_reboot"}]}}],
    [{"parallel": [{"sequence": [{"action": "shell_command.run"}]}]}],
    [{"sequence": [{"action": "python_script.exec"}]}],
    # deeply nested: choose -> sequence -> repeat -> sequence -> shell_command
    [
        {
            "choose": [
                {
                    "conditions": [],
                    "sequence": [
                        {"repeat": {"sequence": [{"action": "shell_command.run"}]}}
                    ],
                }
            ]
        }
    ],
    [
        {"device_id": "abc123", "domain": "light", "type": "turn_on"}
    ],  # device action type
    [{"event": "custom_event", "event_data": {}}],  # fire-event action type
    [{"action": "light"}],  # not a domain.service literal
    [{}],  # an action HA can't classify at all
    # scene.apply/create escape the domain allowlist via an inline entities payload.
    [
        {
            "action": "scene.apply",
            "data": {"entities": {"alarm_control_panel.home": {"state": "disarmed"}}},
        }
    ],
    [
        {
            "action": "scene.create",
            "data": {"scene_id": "x", "entities": {"lock.front": "unlocked"}},
        }
    ],
    # Legacy 'service' key: unreachable post-validation, caught as defense-in-depth.
    [{"service": "shell_command.run"}],
    # payload-danger services on allowed domains (URL fetch / raw device command)
    [{"action": "media_player.play_media", "data": {"media_content_id": "http://x/a"}}],
    [{"action": "vacuum.send_command", "data": {"command": "raw"}}],
]


@pytest.mark.parametrize("tree", BENIGN_TREES)
def test_policy_allows_benign(tree: list[dict[str, Any]]) -> None:
    """Every benign tree (all containers, allowed domains) passes the policy."""
    _enforce_action_policy(_actions(tree))  # must not raise


@pytest.mark.parametrize("tree", DANGEROUS_TREES)
def test_policy_rejects_dangerous(tree: list[dict[str, Any]]) -> None:
    """Every dangerous tree is rejected no matter how deeply the call is nested."""
    with pytest.raises(ClaudeError):
        _enforce_action_policy(_actions(tree))


def _valid_config(actions: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a schema-valid automation with a time trigger and given actions."""
    return {
        "alias": "Test automation",
        "triggers": [{"trigger": "time", "at": "08:00:00"}],
        "actions": actions,
    }


@pytest.fixture
def isolated_config(hass: HomeAssistant, tmp_path: Any) -> str:
    """Point hass.config at an empty temp dir so writes don't touch the shared one."""
    hass.config.config_dir = str(tmp_path)
    return hass.config.path("automations.yaml")


async def test_commit_writes_and_reloads(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """A benign config is validated, written with an id, and reloaded."""
    reloads: list[dict[str, Any]] = []
    hass.services.async_register(
        "automation", "reload", lambda call: reloads.append(dict(call.data))
    )

    alias = await async_commit_automation(
        hass, _valid_config([{"action": "notify.notify", "data": {"message": "hi"}}])
    )

    assert alias == "Test automation"
    stored = load_yaml(isolated_config)
    assert isinstance(stored, list) and len(stored) == 1
    assert stored[0]["alias"] == "Test automation"
    assert stored[0]["id"]  # a fresh id was minted
    assert len(reloads) == 1 and reloads[0]["id"] == stored[0]["id"]


async def test_commit_appends_to_existing_store(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """A new automation is appended to an existing automations.yaml, not overwritten."""
    hass.services.async_register("automation", "reload", lambda call: None)
    Path(isolated_config).write_text(
        "- id: existing\n  alias: Existing\n  triggers: []\n  actions: []\n",
        encoding="utf-8",
    )

    await async_commit_automation(hass, _valid_config([{"action": "light.turn_on"}]))

    stored = load_yaml(isolated_config)
    assert isinstance(stored, list) and len(stored) == 2
    assert {entry["alias"] for entry in stored} == {"Existing", "Test automation"}


async def test_commit_ignores_model_supplied_id(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """A draft's own `id` can't override the minted uuid (no overwriting)."""
    hass.services.async_register("automation", "reload", lambda call: None)
    config = _valid_config([{"action": "light.turn_on"}])
    config["id"] = "existing_user_automation"

    await async_commit_automation(hass, config)

    stored = load_yaml(isolated_config)
    assert len(stored) == 1
    assert stored[0]["id"] != "existing_user_automation"
    assert len(stored[0]["id"]) == 32  # a freshly minted uuid hex


async def test_commit_rejects_blueprint_automation(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """A blueprint-based draft is refused — its actions can't be allow-listed."""
    with pytest.raises(ClaudeError):
        await async_commit_automation(
            hass, {"alias": "bp", "use_blueprint": {"path": "x.yaml", "input": {}}}
        )
    assert not Path(isolated_config).is_file()


async def test_commit_rejects_invalid_schema(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """A config that fails HA's own schema is a clean error, nothing written."""
    with pytest.raises(ClaudeError):
        await async_commit_automation(hass, {"alias": "bad", "actions": []})
    assert not Path(isolated_config).is_file()


async def test_commit_rejects_templated_service_end_to_end(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """A templated service name is rejected end to end; nothing written."""
    hass.services.async_register("automation", "reload", lambda call: None)
    with pytest.raises(ClaudeError):
        await async_commit_automation(
            hass, _valid_config([{"action": "{{ 'light.turn_on' }}"}])
        )
    assert not Path(isolated_config).is_file()


async def test_commit_rejects_dangerous_service_end_to_end(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """A dangerous service is rejected at commit; nothing written."""
    hass.services.async_register("automation", "reload", lambda call: None)
    with pytest.raises(ClaudeError):
        await async_commit_automation(
            hass, _valid_config([{"action": "shell_command.run"}])
        )
    assert not Path(isolated_config).is_file()


async def test_commit_wraps_unexpected_validation_error(
    hass: HomeAssistant, isolated_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected error from HA's validator becomes a clean ClaudeError."""

    async def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(
        "custom_components.claude_ha.automation_commit.async_validate_config_item",
        _boom,
    )
    with pytest.raises(ClaudeError):
        await async_commit_automation(
            hass, _valid_config([{"action": "light.turn_on"}])
        )
    assert not Path(isolated_config).is_file()


async def test_commit_rejects_when_validation_returns_none(
    hass: HomeAssistant, isolated_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A None from the validator is a clean error; nothing written."""

    async def _none(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(
        "custom_components.claude_ha.automation_commit.async_validate_config_item",
        _none,
    )
    with pytest.raises(ClaudeError):
        await async_commit_automation(
            hass, _valid_config([{"action": "light.turn_on"}])
        )
    assert not Path(isolated_config).is_file()


async def test_commit_reports_write_or_reload_failure(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """A save/reload failure surfaces as a clean ClaudeError, never raises."""
    from homeassistant.exceptions import HomeAssistantError

    def _boom(_call: Any) -> None:
        raise HomeAssistantError("reload blew up")

    hass.services.async_register("automation", "reload", _boom)
    with pytest.raises(ClaudeError):
        await async_commit_automation(
            hass, _valid_config([{"action": "light.turn_on"}])
        )


# --- find + delete ----------------------------------------------------------


def _set_automation(hass: HomeAssistant, slug: str, name: str, config_id: str) -> None:
    hass.states.async_set(
        f"automation.{slug}", "on", {"id": config_id, "friendly_name": name}
    )


async def test_find_matches_by_alias(hass: HomeAssistant) -> None:
    """A query matches the automation whose name shares its words, best first."""
    _set_automation(hass, "a", "Morning Lights", "id-a")
    _set_automation(hass, "b", "Evening Lights", "id-b")
    matches = find_automations(hass, "morning lights")
    assert [m.config_id for m in matches] == ["id-a"]


async def test_find_returns_all_ambiguous_candidates(hass: HomeAssistant) -> None:
    """A query matching several automations returns them all (caller disambiguates)."""
    _set_automation(hass, "k", "Kitchen Lights", "id-k")
    _set_automation(hass, "b", "Bedroom Lights", "id-b")
    matches = find_automations(hass, "lights")
    assert {m.config_id for m in matches} == {"id-k", "id-b"}


async def test_find_skips_automations_without_id(hass: HomeAssistant) -> None:
    """An automation with no config id can't be deleted, so it isn't a candidate."""
    hass.states.async_set("automation.noid", "on", {"friendly_name": "Orphan Auto"})
    assert find_automations(hass, "orphan auto") == []


async def test_find_empty_query_returns_nothing(hass: HomeAssistant) -> None:
    """An empty/stopword-only query matches nothing (caller asks which)."""
    _set_automation(hass, "a", "Morning", "id-a")
    assert find_automations(hass, "  the  ") == []


async def test_find_ignores_name_with_no_words(hass: HomeAssistant) -> None:
    """An automation whose name has no word tokens scores 0 and isn't a candidate."""
    _set_automation(hass, "a", "Morning Lights", "id-a")
    hass.states.async_set(
        "automation.blank", "on", {"id": "blank", "friendly_name": "!!!"}
    )
    matches = find_automations(hass, "morning lights")
    assert [m.config_id for m in matches] == ["id-a"]


async def test_delete_removes_by_id_and_reloads(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """Deleting an id drops just that entry, keeps the rest, and reloads it."""
    reloads: list[dict[str, Any]] = []
    hass.services.async_register(
        "automation", "reload", lambda call: reloads.append(dict(call.data))
    )
    Path(isolated_config).write_text(
        "- id: keep\n  alias: Keep\n  triggers: []\n  actions: []\n"
        "- id: gone\n  alias: Gone\n  triggers: []\n  actions: []\n",
        encoding="utf-8",
    )

    await async_delete_automation(hass, "gone")

    stored = load_yaml(isolated_config)
    assert [entry["id"] for entry in stored] == ["keep"]
    assert reloads and reloads[0]["id"] == "gone"


async def test_delete_removes_the_entity_registry_entry(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """A registered automation entity is unregistered on delete."""
    registry = er.async_get(hass)
    registry.async_get_or_create("automation", "automation", "gone")
    hass.services.async_register("automation", "reload", lambda call: None)
    Path(isolated_config).write_text(
        "- id: gone\n  alias: Gone\n  triggers: []\n  actions: []\n", encoding="utf-8"
    )

    await async_delete_automation(hass, "gone")

    assert registry.async_get_entity_id("automation", "automation", "gone") is None


async def test_delete_unknown_id_errors_and_keeps_file(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """Deleting an id that isn't present is a clean error; the file is untouched."""
    hass.services.async_register("automation", "reload", lambda call: None)
    Path(isolated_config).write_text(
        "- id: keep\n  alias: Keep\n  triggers: []\n  actions: []\n", encoding="utf-8"
    )
    with pytest.raises(ClaudeError):
        await async_delete_automation(hass, "missing")
    assert [entry["id"] for entry in load_yaml(isolated_config)] == ["keep"]


async def test_delete_reload_failure_is_clean(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """A failure during delete surfaces as a clean ClaudeError, never raises."""
    from homeassistant.exceptions import HomeAssistantError

    def _boom(_call: Any) -> None:
        raise HomeAssistantError("reload blew up")

    hass.services.async_register("automation", "reload", _boom)
    Path(isolated_config).write_text(
        "- id: gone\n  alias: Gone\n  triggers: []\n  actions: []\n", encoding="utf-8"
    )
    with pytest.raises(ClaudeError):
        await async_delete_automation(hass, "gone")

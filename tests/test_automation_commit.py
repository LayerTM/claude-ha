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

from custom_components.claude_ha import automation_commit
from custom_components.claude_ha.api import ClaudeError
from custom_components.claude_ha.automation_commit import (
    _enforce_action_policy,
    async_commit_automation,
    async_delete_automation,
    async_read_automation_config,
    async_update_automation,
    find_automations,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util.file import WriteError
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
    # payload-aware: scene.apply is fine when every entity it reaches is allowed
    [{"action": "scene.apply", "data": {"entities": {"light.x": {"state": "on"}}}}],
    # a templated NON-target data value (a message) is fine — only refs are scanned
    [
        {
            "action": "notify.notify",
            "data": {"message": "It's {{ states('sensor.t') }} degrees"},
        }
    ],
    # entity_id as a list of allowed entities; comma-string of allowed entities
    [{"action": "light.turn_on", "target": {"entity_id": ["light.a", "switch.b"]}}],
    [{"action": "light.turn_on", "target": {"entity_id": "light.a,light.b"}}],
    # a call with no entity target at all (worst case: broad within an allowed domain)
    [{"action": "light.turn_off"}],
    # legacy `data_template` with an allowed entity is fine (inspected, not blocked)
    [{"action": "light.turn_on", "data_template": {"entity_id": "light.x"}}],
    # media_player.join group_members of allowed media_players is fine
    [
        {
            "action": "media_player.join",
            "target": {"entity_id": "media_player.a"},
            "data": {"group_members": ["media_player.b", "media_player.c"]},
        }
    ],
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
            "data": {"scene_id": "x", "entities": {"alarm_control_panel.home": "x"}},
        }
    ],
    # scene.create snapshot_entities reaching a non-allowed domain
    [
        {
            "action": "scene.create",
            "data": {"snapshot_entities": ["alarm_control_panel.h"]},
        }
    ],
    # media_player.join group_members (audited key) reaching a non-allowed domain
    [
        {
            "action": "media_player.join",
            "data": {"group_members": ["alarm_control_panel.home"]},
        }
    ],
    # Legacy 'service' key: unreachable post-validation, caught as defense-in-depth.
    [{"service": "shell_command.run"}],
    # payload-danger services on allowed domains (URL fetch / raw device command)
    [{"action": "media_player.play_media", "data": {"media_content_id": "http://x/a"}}],
    [{"action": "vacuum.send_command", "data": {"command": "raw"}}],
    # --- payload-aware entity-reach + fail-closed ---
    # a call reaching an entity OUTSIDE the allowed domains (target / legacy / data)
    [{"action": "light.turn_on", "target": {"entity_id": "alarm_control_panel.home"}}],
    [{"action": "light.turn_on", "entity_id": "alarm_control_panel.home"}],
    [{"action": "light.turn_on", "data": {"entity_id": "alarm_control_panel.home"}}],
    # scene.apply reaching a non-allowed domain via its inline entities map
    [
        {
            "action": "scene.apply",
            "data": {"entities": {"alarm_control_panel.home": {"state": "disarmed"}}},
        }
    ],
    # fail-closed: device / area / floor / label targeting (unbounded, TOCTOU)
    [{"action": "light.turn_on", "target": {"device_id": "abc"}}],
    [{"action": "light.turn_on", "target": {"area_id": "living_room"}}],
    [{"action": "light.turn_on", "target": {"floor_id": "ground"}}],
    [{"action": "light.turn_on", "target": {"label_id": "night"}}],
    # fail-closed: the "all" wildcard, a template in the entity ref, a uuid, a bad elem
    [{"action": "light.turn_off", "target": {"entity_id": "all"}}],
    [{"action": "light.turn_on", "target": {"entity_id": "{{ 'lock.front' }}"}}],
    [{"action": "light.turn_on", "data": {"entity_id": "{{ 'lock.front' }}"}}],
    [{"action": "light.turn_on", "target": {"entity_id": "0a1b2c3d4e5f"}}],
    [
        {
            "action": "light.turn_on",
            "target": {"entity_id": ["light.a", "climate.z", "z"]},
        }
    ],
    # non-literal entity references: templated list element, non-str element, non-list
    [{"action": "light.turn_on", "target": {"entity_id": ["{{ 'x' }}"]}}],
    [{"action": "light.turn_on", "target": {"entity_id": ["light.a", 123]}}],
    [{"action": "light.turn_on", "target": {"entity_id": 123}}],
    # legacy `data_template` is merged at runtime like `data` — must be inspected too
    [
        {
            "action": "scene.apply",
            "data_template": {"entities": {"alarm_control_panel.home": {"state": "x"}}},
        }
    ],
    [{"action": "scene.create", "data_template": {"snapshot_entities": ["camera.x"]}}],
    [
        {
            "action": "light.turn_on",
            "data_template": {"entity_id": "alarm_control_panel.h"},
        }
    ],
    [{"action": "light.turn_on", "data_template": {"area_id": "living_room"}}],
    # a whole target/data/entities BLOCK that is a template (non-dict) can't be bounded
    [{"action": "scene.apply", "data": "{{ x }}"}],
    [{"action": "scene.turn_on", "target": "{{ x }}"}],
    [{"action": "scene.apply", "data": {"entities": "{{ x }}"}}],
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


async def test_commit_rejects_whole_data_template_escape(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """A whole-`data` Jinja template can't smuggle an entity map past the gate.

    End-to-end through the real validator: `async_validate_config_item` coerces the
    whole-value template to a Template object; the gate rejects the non-dict block, so
    the alarm-disarming automation is never written.
    """
    hass.services.async_register("automation", "reload", lambda call: None)
    disarm = "{'entities': {'alarm_control_panel.home': {'state': 'disarmed'}}}"
    escape = {"action": "scene.apply", "data": f"{{{{ {disarm} }}}}"}
    with pytest.raises(ClaudeError):
        await async_commit_automation(hass, _valid_config([escape]))
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


# --- store integrity --------------------------------------------------------
#
# Two ways the store could be left worse than it was found, both fixed here and
# both asserted on the FILE rather than only on the error raised.

# A store this code did not write: neither shape is a list of automations.
NOT_A_LIST: list[str] = ["morning:\n  alias: Morning\n  actions: []\n", "broken\n"]


def _boom_reload(_call: Any) -> None:
    """Fail the reload the way a bad config makes it fail."""
    raise HomeAssistantError("reload blew up")


async def test_commit_treats_an_empty_store_file_as_an_empty_store(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """An existing but empty automations.yaml is empty, not unreadable."""
    hass.services.async_register("automation", "reload", lambda call: None)
    Path(isolated_config).write_text("", encoding="utf-8")

    await async_commit_automation(hass, _valid_config([{"action": "light.turn_on"}]))

    assert len(load_yaml(isolated_config)) == 1


@pytest.mark.parametrize("contents", NOT_A_LIST)
async def test_commit_refuses_a_store_that_is_not_a_list(
    hass: HomeAssistant, isolated_config: str, contents: str
) -> None:
    """A non-list store is refused by name, and the file is left byte-identical.

    Read as an empty store instead, the commit would have replaced the whole file
    with its one automation and reported success.
    """
    hass.services.async_register("automation", "reload", lambda call: None)
    Path(isolated_config).write_text(contents, encoding="utf-8")

    with pytest.raises(ClaudeError, match="rather than a list"):
        await async_commit_automation(
            hass, _valid_config([{"action": "light.turn_on"}])
        )

    assert Path(isolated_config).read_text(encoding="utf-8") == contents


@pytest.mark.parametrize("contents", NOT_A_LIST)
async def test_delete_refuses_a_store_that_is_not_a_list(
    hass: HomeAssistant, isolated_config: str, contents: str
) -> None:
    """The same refusal on the delete path; nothing is written."""
    hass.services.async_register("automation", "reload", lambda call: None)
    Path(isolated_config).write_text(contents, encoding="utf-8")

    with pytest.raises(ClaudeError, match="rather than a list"):
        await async_delete_automation(hass, "whatever")

    assert Path(isolated_config).read_text(encoding="utf-8") == contents


async def test_read_config_refuses_a_store_that_is_not_a_list(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """Reading says the store is unreadable rather than "no such automation"."""
    Path(isolated_config).write_text(NOT_A_LIST[0], encoding="utf-8")

    with pytest.raises(ClaudeError, match="rather than a list"):
        await async_read_automation_config(hass, "morning")


async def test_commit_reload_failure_restores_the_previous_file(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """A failed reload leaves the store exactly as it was, not holding the draft."""
    original = "- id: keep\n  alias: Keep\n  triggers: []\n  actions: []\n"
    Path(isolated_config).write_text(original, encoding="utf-8")
    hass.services.async_register("automation", "reload", _boom_reload)

    with pytest.raises(ClaudeError, match="Couldn't reload automations"):
        await async_commit_automation(
            hass, _valid_config([{"action": "light.turn_on"}])
        )

    assert Path(isolated_config).read_text(encoding="utf-8") == original


async def test_commit_reload_failure_removes_a_store_it_created(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """With no store to begin with, the rollback is removing the file we made."""
    assert not Path(isolated_config).exists()
    hass.services.async_register("automation", "reload", _boom_reload)

    with pytest.raises(ClaudeError, match="Couldn't reload automations"):
        await async_commit_automation(
            hass, _valid_config([{"action": "light.turn_on"}])
        )

    assert not Path(isolated_config).exists()


async def test_reload_failure_that_cannot_be_undone_says_so(
    hass: HomeAssistant, isolated_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the rollback itself fails, the message says the file holds the change.

    The failure injected is the one the writer really raises: Home Assistant's
    ``write_utf8_file_atomic`` wraps an ``OSError`` in ``WriteError``, which is a
    ``HomeAssistantError`` and NOT an ``OSError`` — a test that injected a bare
    ``OSError`` here would pass against code that cannot catch the real thing.
    Only the restore is failed; the first write has to succeed for there to be
    anything to roll back.
    """
    Path(isolated_config).write_text("[]\n", encoding="utf-8")
    hass.services.async_register("automation", "reload", _boom_reload)
    real_write = automation_commit.write_utf8_file_atomic
    calls: list[int] = []

    def _fail_the_restore(path: str, data: Any, **kwargs: Any) -> None:
        calls.append(1)
        if len(calls) > 1:  # the restore, not the write under test
            raise WriteError(OSError("read-only filesystem"))
        real_write(path, data, **kwargs)

    monkeypatch.setattr(automation_commit, "write_utf8_file_atomic", _fail_the_restore)

    with pytest.raises(ClaudeError, match="still holds the change"):
        await async_commit_automation(
            hass, _valid_config([{"action": "light.turn_on"}])
        )


async def test_reload_failure_restores_crlf_byte_for_byte(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """A CRLF store comes back with its line endings, not rewritten to LF.

    Snapshotting through text would read the file under universal newlines, and
    "restoring the exact previous contents" would silently rewrite every line
    ending in a file the user may share with Windows tooling.
    """
    original = b"- id: keep\r\n  alias: Keep\r\n  triggers: []\r\n  actions: []\r\n"
    Path(isolated_config).write_bytes(original)
    hass.services.async_register("automation", "reload", _boom_reload)

    with pytest.raises(ClaudeError, match="Couldn't reload automations"):
        await async_commit_automation(
            hass, _valid_config([{"action": "light.turn_on"}])
        )

    assert Path(isolated_config).read_bytes() == original


async def test_rollback_against_the_real_reload_service(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """The rollback works against Home Assistant's own reload, not just a stub.

    Every other case here registers a fake `automation.reload`, which proves the
    handling but not that the real service fails in a way this code catches. The
    real one reads the whole configuration, so an unreadable config makes it
    raise `FileNotFoundError` — an OSError, and a failure mode no stub was asked
    to imitate. The automations carry no triggers, so nothing is left armed.
    """
    original = b"- id: keep\n  alias: Keep\n  triggers: []\n  actions: []\n"
    Path(isolated_config).write_bytes(original)
    assert await async_setup_component(hass, "automation", {})
    await hass.async_block_till_done()
    assert not Path(hass.config.path("configuration.yaml")).exists()

    with pytest.raises(ClaudeError, match="Couldn't reload automations"):
        await async_commit_automation(
            hass, _valid_config([{"action": "light.turn_on"}])
        )

    assert Path(isolated_config).read_bytes() == original


async def test_rollback_leaves_a_store_someone_else_rewrote(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """If the file changed under us during the reload, the rollback stands down.

    Home Assistant's own automation editor writes this file too. Restoring a
    snapshot blindly would destroy whatever it had just saved, turning a failed
    save of ours into data loss that is entirely ours.
    """
    editor_wrote = b"- id: theirs\n  alias: Theirs\n  triggers: []\n  actions: []\n"

    def _reload_then_someone_else_writes(_call: Any) -> None:
        Path(isolated_config).write_bytes(editor_wrote)
        raise HomeAssistantError("reload blew up")

    Path(isolated_config).write_text(
        "- id: keep\n  alias: Keep\n  triggers: []\n  actions: []\n", encoding="utf-8"
    )
    hass.services.async_register(
        "automation", "reload", _reload_then_someone_else_writes
    )

    with pytest.raises(ClaudeError, match="changed by something else"):
        await async_commit_automation(
            hass, _valid_config([{"action": "light.turn_on"}])
        )

    assert Path(isolated_config).read_bytes() == editor_wrote


async def test_commit_reports_a_write_failure(
    hass: HomeAssistant, isolated_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that fails is a clean error; the reload is never reached."""
    reloads: list[dict[str, Any]] = []
    hass.services.async_register(
        "automation", "reload", lambda call: reloads.append(dict(call.data))
    )

    def _no_write(_path: str, _data: list[Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(automation_commit, "_write_store", _no_write)

    with pytest.raises(ClaudeError, match="Couldn't save the automation"):
        await async_commit_automation(
            hass, _valid_config([{"action": "light.turn_on"}])
        )

    assert reloads == []


async def test_delete_reports_a_write_failure(
    hass: HomeAssistant, isolated_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same on the delete path."""
    hass.services.async_register("automation", "reload", lambda call: None)
    Path(isolated_config).write_text(
        "- id: gone\n  alias: Gone\n  triggers: []\n  actions: []\n", encoding="utf-8"
    )

    def _no_write(_path: str, _data: list[Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(automation_commit, "_write_store", _no_write)

    with pytest.raises(ClaudeError, match="Couldn't delete the automation"):
        await async_delete_automation(hass, "gone")


async def test_delete_reload_failure_restores_the_file_and_the_entity(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """A failed delete leaves BOTH the store and the entity registry as they were."""
    original = "- id: gone\n  alias: Gone\n  triggers: []\n  actions: []\n"
    Path(isolated_config).write_text(original, encoding="utf-8")
    registry = er.async_get(hass)
    registry.async_get_or_create("automation", "automation", "gone")
    hass.services.async_register("automation", "reload", _boom_reload)

    with pytest.raises(ClaudeError, match="Couldn't reload automations"):
        await async_delete_automation(hass, "gone")

    assert Path(isolated_config).read_text(encoding="utf-8") == original
    assert registry.async_get_entity_id("automation", "automation", "gone") is not None


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


# --- modify (update-in-place) ----------------------------------------------


async def test_update_replaces_target_in_place(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """An update rewrites just the target entry, keeping its id and the others."""
    reloads: list[dict[str, Any]] = []
    hass.services.async_register(
        "automation", "reload", lambda call: reloads.append(dict(call.data))
    )
    Path(isolated_config).write_text(
        "- id: keep\n  alias: Keep\n  triggers: []\n  actions: []\n"
        "- id: target\n  alias: Old\n  triggers: []\n  actions: []\n",
        encoding="utf-8",
    )
    # A model-supplied id in the new config must be ignored (target id wins).
    new_config = _valid_config([{"action": "light.turn_on"}])
    new_config["alias"] = "New Morning"
    new_config["id"] = "attacker"

    alias = await async_update_automation(hass, new_config, "target")

    assert alias == "New Morning"
    stored = {entry["id"]: entry for entry in load_yaml(isolated_config)}
    assert set(stored) == {"keep", "target"}  # attacker id never lands
    assert stored["target"]["alias"] == "New Morning"
    assert reloads and reloads[0]["id"] == "target"


async def test_update_rejects_dangerous(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """An update runs the same allowlist; a dangerous action is rejected, no write."""
    hass.services.async_register("automation", "reload", lambda call: None)
    Path(isolated_config).write_text(
        "- id: target\n  alias: Old\n  triggers: []\n  actions: []\n", encoding="utf-8"
    )
    with pytest.raises(ClaudeError):
        await async_update_automation(
            hass, _valid_config([{"action": "shell_command.run"}]), "target"
        )
    # target entry unchanged
    assert load_yaml(isolated_config)[0]["alias"] == "Old"


async def test_read_automation_config_returns_config_without_id(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """Reading a target returns its stored config, minus the id, for editing."""
    Path(isolated_config).write_text(
        "- id: target\n  alias: Morning\n  triggers: []\n"
        "  actions: [{action: light.turn_on}]\n",
        encoding="utf-8",
    )
    config = await async_read_automation_config(hass, "target")
    assert config is not None
    assert "id" not in config
    assert config["alias"] == "Morning"
    assert config["actions"] == [{"action": "light.turn_on"}]


async def test_read_automation_config_missing_is_none(
    hass: HomeAssistant, isolated_config: str
) -> None:
    """Reading an unknown id returns None."""
    Path(isolated_config).write_text(
        "- id: other\n  alias: Other\n  triggers: []\n  actions: []\n", encoding="utf-8"
    )
    assert await async_read_automation_config(hass, "missing") is None

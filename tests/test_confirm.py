"""Tests for the two-phase confirm (actionable notification) flow."""

from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.confirm import EVENT_MOBILE_APP_ACTION
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .conftest import TEST_BASE_URL, setup_integration

NOTIFY = "mobile_app_test"

PROPOSAL = {
    "summary": "Turn off the heater",
    "intents": [{"intent": "HassTurnOff", "targets": ["switch.heater"], "data": {}}],
}
PROPOSAL_RESP = {
    "text": "I can turn off the heater.",
    "proposal": PROPOSAL,
    "tools_used": [],
    "truncated": False,
}


def _post_count(mock: AiohttpClientMocker) -> int:
    return len([c for c in mock.mock_calls if c[0] == "POST"])


async def _ask_notify(
    hass: HomeAssistant, mock: AiohttpClientMocker, resp=PROPOSAL_RESP
):
    """Run a read `ask` with a notify target; return the captured notify calls."""
    mock.post(f"{TEST_BASE_URL}/api/prompt", json=resp)
    calls = async_mock_service(hass, "notify", NOTIFY)
    await hass.services.async_call(
        "claude_ha",
        "ask",
        {"prompt": "turn off the heater", "notify": NOTIFY},
        blocking=True,
        return_response=True,
    )
    return calls


def _actions(notify_call) -> list[dict]:
    return notify_call.data["data"]["actions"]


async def _fire(hass: HomeAssistant, action: str) -> None:
    hass.bus.async_fire(EVENT_MOBILE_APP_ACTION, {"action": action})
    await hass.async_block_till_done()


async def test_notify_sends_actionable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A proposal + notify target sends an Approve/Dismiss notification."""
    await setup_integration(hass, mock_config_entry)
    calls = await _ask_notify(hass, aioclient_mock)
    assert len(calls) == 1
    actions = _actions(calls[0])
    assert actions[0]["action"].startswith("CLAUDE_HA_APPROVE_")
    assert actions[1]["action"].startswith("CLAUDE_HA_DISMISS_")
    assert "switch.heater" in calls[0].data["message"]


async def test_notify_without_proposal_is_silent(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """No proposal means no notification is sent."""
    await setup_integration(hass, mock_config_entry)
    plain = {"text": "ok", "proposal": None, "tools_used": [], "truncated": False}
    calls = await _ask_notify(hass, aioclient_mock, resp=plain)
    assert len(calls) == 0


async def test_approve_executes_write(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Approve runs the confirmed write with the proposal's intents."""
    await setup_integration(hass, mock_config_entry)
    calls = await _ask_notify(hass, aioclient_mock)
    executed = []
    hass.bus.async_listen(
        "claude_ha_action_executed", lambda e: executed.append(e.data)
    )

    before = _post_count(aioclient_mock)
    await _fire(hass, _actions(calls[0])[0]["action"])

    assert _post_count(aioclient_mock) == before + 1
    body = [c for c in aioclient_mock.mock_calls if c[0] == "POST"][-1][2]
    assert body["mode"] == "write"
    assert body["intents"] == PROPOSAL["intents"]
    assert executed and executed[0]["summary"] == "Turn off the heater"


async def test_dismiss_does_not_write(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Dismiss discards the proposal without a write."""
    await setup_integration(hass, mock_config_entry)
    calls = await _ask_notify(hass, aioclient_mock)
    before = _post_count(aioclient_mock)
    await _fire(hass, _actions(calls[0])[1]["action"])
    assert _post_count(aioclient_mock) == before


async def test_unknown_action_ignored(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An unrelated notification action is a no-op."""
    await setup_integration(hass, mock_config_entry)
    await _ask_notify(hass, aioclient_mock)
    before = _post_count(aioclient_mock)
    await _fire(hass, "SOME_OTHER_APP_ACTION")
    assert _post_count(aioclient_mock) == before


async def test_unknown_pid_ignored(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Approving an unknown proposal id does nothing."""
    await setup_integration(hass, mock_config_entry)
    await _ask_notify(hass, aioclient_mock)
    before = _post_count(aioclient_mock)
    await _fire(hass, "CLAUDE_HA_APPROVE_deadbeef")
    assert _post_count(aioclient_mock) == before


async def test_expired_proposal_not_executed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
    freezer,
) -> None:
    """A proposal past its TTL is not executed on Approve."""
    await setup_integration(hass, mock_config_entry)
    calls = await _ask_notify(hass, aioclient_mock)
    approve = _actions(calls[0])[0]["action"]

    freezer.tick(timedelta(minutes=11))
    before = _post_count(aioclient_mock)
    await _fire(hass, approve)
    assert _post_count(aioclient_mock) == before


async def test_prune_drops_expired_on_new_send(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
    freezer,
) -> None:
    """Sending a new proposal prunes an earlier expired one."""
    from custom_components.claude_ha.confirm import DATA_PENDING

    await setup_integration(hass, mock_config_entry)
    await _ask_notify(hass, aioclient_mock)
    assert len(hass.data[DATA_PENDING]) == 1
    freezer.tick(timedelta(minutes=11))
    await _ask_notify(hass, aioclient_mock)
    # the expired one was pruned; only the fresh one remains
    assert len(hass.data[DATA_PENDING]) == 1


async def test_approve_when_entry_unloaded(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """If the entry is unloaded before Approve, no write is attempted."""
    await setup_integration(hass, mock_config_entry)
    calls = await _ask_notify(hass, aioclient_mock)
    approve = _actions(calls[0])[0]["action"]

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED

    before = _post_count(aioclient_mock)
    await _fire(hass, approve)
    assert _post_count(aioclient_mock) == before


async def test_approve_write_failure_is_handled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A failing write on Approve is logged, not raised."""
    await setup_integration(hass, mock_config_entry)
    calls = await _ask_notify(hass, aioclient_mock)
    approve = _actions(calls[0])[0]["action"]

    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{TEST_BASE_URL}/api/prompt", status=503)
    await _fire(hass, approve)  # must not raise


async def test_setup_confirm_is_idempotent(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """Registering the confirm listener twice is a safe no-op."""
    from custom_components.claude_ha.confirm import (
        DATA_CONFIRM_LISTENER,
        async_setup_confirm,
    )

    await setup_integration(hass, mock_config_entry)
    listener = hass.data[DATA_CONFIRM_LISTENER]
    async_setup_confirm(hass)  # second call returns early
    assert hass.data[DATA_CONFIRM_LISTENER] is listener


async def test_proposal_without_targets(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A proposal without entity targets still notifies (no 'Affects' line)."""
    await setup_integration(hass, mock_config_entry)
    resp = {
        "text": "ok",
        "proposal": {"summary": "Run a scene", "intents": [{"intent": "HassX"}]},
        "tools_used": [],
        "truncated": False,
    }
    calls = await _ask_notify(hass, aioclient_mock, resp=resp)
    assert len(calls) == 1
    assert "Affects" not in calls[0].data["message"]

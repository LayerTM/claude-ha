"""Tests for the Claude status and usage sensors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.api import StatusResult
from custom_components.claude_ha.const import DOMAIN
from custom_components.claude_ha.sensor import (
    ClaudeBudgetSensor,
    ClaudeChatHealthSensor,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import TEST_BASE_URL, USAGE_PAYLOAD, setup_integration


def _sensor(hass: HomeAssistant, entry: MockConfigEntry, suffix: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{suffix}"
    )
    assert entity_id is not None
    return entity_id


async def test_status_sensor_ready(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """The status sensor reports ready with version/model/ha_mcp attributes."""
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_sensor(hass, mock_config_entry, "status"))
    assert state is not None
    assert state.state == "ready"
    assert state.attributes["claude_version"] == "2.0.1"
    assert state.attributes["model"] == "claude-sonnet-4-6"
    assert state.attributes["version"] == "1.14.0"
    assert state.attributes["ha_mcp"] is True
    assert state.attributes["ha_mcp_connected"] is True
    assert "health" in state.attributes


async def test_status_sensor_initializing(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A not-ready add-on reports the initializing state."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={
            "ready": False,
            "version": "1.7.0",
            "claude_version": None,
            "model": None,
        },
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_sensor(hass, mock_config_entry, "status"))
    assert state is not None
    assert state.state == "initializing"


async def test_status_sensor_becomes_unavailable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A poll failure after a healthy load marks the status sensor unavailable."""
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", json={"ready": True})
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)
    status_id = _sensor(hass, mock_config_entry, "status")
    assert hass.states.get(status_id).state == "ready"

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", status=500)
    await mock_config_entry.runtime_data.status.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(status_id).state == STATE_UNAVAILABLE
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_chat_health_sensor_unavailable_without_field(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """An add-on that doesn't report chat_health leaves the sensor unavailable."""
    await setup_integration(hass, mock_config_entry)
    state = hass.states.get(_sensor(hass, mock_config_entry, "chat_health"))
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


async def test_chat_health_sensor_ok(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Zero recent degraded reads → the soft indicator reads ok, counts as attrs."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={
            "ready": True,
            "chat_health": {
                "recent": 8,
                "degraded": 0,
                "recovered": 2,
                "last_reason": None,
            },
        },
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_sensor(hass, mock_config_entry, "chat_health"))
    assert state is not None
    assert state.state == "ok"
    assert state.attributes["recent"] == 8
    assert state.attributes["recent_ok"] == 8
    assert state.attributes["degraded"] == 0
    assert state.attributes["recovered"] == 2
    assert state.attributes["failure_rate"] == 0.0
    assert state.attributes.get("last_reason") is None
    assert state.attributes["last_failure"] is None
    assert state.attributes["window_from"] is None
    assert state.attributes["window_to"] is None


async def test_chat_health_sensor_degraded(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A failure rate over the threshold reads degraded, with the reason token.

    No stamps here, so this is also the fail-closed path: an add-on older than
    1.49.0 is judged on the rate alone and keeps warning.
    """
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={
            "ready": True,
            "chat_health": {
                "recent": 8,
                "degraded": 2,
                "recovered": 1,
                "last_reason": "model-error",
            },
        },
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_sensor(hass, mock_config_entry, "chat_health"))
    assert state is not None
    assert state.state == "degraded"
    assert state.attributes["degraded"] == 2
    assert state.attributes["recent_ok"] == 6
    assert state.attributes["failure_rate"] == 0.25
    assert state.attributes["consecutive_ok"] is None
    assert state.attributes["last_reason"] == "model-error"


def _ms_ago(hours: float) -> int:
    """Epoch ms that many hours before now, on the clock the sensor reads."""
    return int((dt_util.utcnow() - timedelta(hours=hours)).timestamp() * 1000)


async def test_chat_health_sensor_renders_the_timestamps_it_was_given(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Each stamp lands on its own attribute, at the right scale.

    Asserting only null-ness let three mutants live behind 100% line coverage: a
    milliseconds/seconds mix-up, the window ends swapped, and `last_failure` wired
    to the wrong field.
    """
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={
            "ready": True,
            "chat_health": {
                "recent": 10,
                "degraded": 0,
                "recovered": 0,
                "last_reason": None,
                "last_failure_ts": 1757000000000,
                "window_from_ts": 1756900000000,
                "window_to_ts": 1757100000000,
            },
        },
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_sensor(hass, mock_config_entry, "chat_health"))
    assert state is not None
    assert state.attributes["last_failure"] == datetime(
        2025, 9, 4, 15, 33, 20, tzinfo=UTC
    )
    assert state.attributes["window_from"] == datetime(
        2025, 9, 3, 11, 46, 40, tzinfo=UTC
    )
    assert state.attributes["window_to"] == datetime(2025, 9, 5, 19, 20, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("recent", "degraded", "expected"),
    [
        (10, 1, "degraded"),  # exactly at the threshold — the boundary is inclusive
        (20, 1, "ok"),  # one under it
    ],
)
async def test_chat_health_sensor_rate_boundary(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    recent: int,
    degraded: int,
    expected: str,
) -> None:
    """The threshold comparison is `>=`, pinned on both sides of the boundary."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={
            "ready": True,
            "chat_health": {
                "recent": recent,
                "degraded": degraded,
                "recovered": 0,
                "last_reason": "model-error",
                "last_failure_ts": _ms_ago(0.05),
                "consecutive_ok": 0,
            },
        },
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_sensor(hass, mock_config_entry, "chat_health"))
    assert state is not None
    assert state.state == expected


async def test_chat_health_sensor_ok_on_isolated_old_failure(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """One failure in 38 reads, 19 h old, is noise — the bug this fix exists for."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={
            "ready": True,
            "chat_health": {
                "recent": 38,
                "degraded": 1,
                "recovered": 1,
                "last_reason": "model-error",
                "last_failure_ts": _ms_ago(19),
                "window_from_ts": _ms_ago(72),
                "window_to_ts": _ms_ago(0.1),
            },
        },
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_sensor(hass, mock_config_entry, "chat_health"))
    assert state is not None
    assert state.state == "ok"
    assert state.attributes["recent_ok"] == 37
    assert state.attributes["failure_rate"] == 0.026
    # The context survives even though the state is ok.
    assert state.attributes["last_reason"] == "model-error"
    assert state.attributes["last_failure"] is not None
    assert state.attributes["window_from"] is not None


async def test_chat_health_sensor_ok_when_every_failure_is_stale(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A window that is all failures still reads ok once they age out."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={
            "ready": True,
            "chat_health": {
                "recent": 4,
                "degraded": 4,
                "recovered": 0,
                "last_reason": "model-error",
                "last_failure_ts": _ms_ago(8),
            },
        },
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_sensor(hass, mock_config_entry, "chat_health"))
    assert state is not None
    assert state.state == "ok"
    assert state.attributes["failure_rate"] == 1.0


async def test_chat_health_sensor_degraded_when_failure_is_fresh(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The same window with a minutes-old failure is a live outage, not noise."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={
            "ready": True,
            "chat_health": {
                "recent": 4,
                "degraded": 4,
                "recovered": 0,
                "last_reason": "model-error",
                "last_failure_ts": _ms_ago(0.05),
            },
        },
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_sensor(hass, mock_config_entry, "chat_health"))
    assert state is not None
    assert state.state == "degraded"


async def test_chat_health_sensor_ok_on_demonstrated_recovery(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A short window the rate can't rescue is rescued by clean runs since.

    1 failure in 3 is a 0.33 rate and the failure is minutes old, so neither the
    rate nor the clock clears it — but both successes came after it.
    """
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={
            "ready": True,
            "chat_health": {
                "recent": 3,
                "degraded": 1,
                "recovered": 0,
                "last_reason": "model-error",
                "last_failure_ts": _ms_ago(0.05),
                "consecutive_ok": 2,
            },
        },
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_sensor(hass, mock_config_entry, "chat_health"))
    assert state is not None
    assert state.state == "ok"
    assert state.attributes["consecutive_ok"] == 2


async def test_chat_health_sensor_degraded_when_whole_window_failed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An all-failed window has zero successes and zero since — not recovery.

    The loudest case there is, and the one a naive ``consecutive_ok >= successes``
    would clear: both sides are 0.
    """
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={
            "ready": True,
            "chat_health": {
                "recent": 4,
                "degraded": 4,
                "recovered": 0,
                "last_reason": "model-error",
                "last_failure_ts": _ms_ago(0.05),
                "consecutive_ok": 0,
            },
        },
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_sensor(hass, mock_config_entry, "chat_health"))
    assert state is not None
    assert state.state == "degraded"
    assert state.attributes["consecutive_ok"] == 0


async def test_chat_health_sensor_degraded_while_the_window_still_flaps(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Three clean runs don't clear a window that keeps failing every fourth read."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={
            "ready": True,
            "chat_health": {
                "recent": 12,
                "degraded": 3,
                "recovered": 0,
                "last_reason": "model-error",
                "last_failure_ts": _ms_ago(0.05),
                "consecutive_ok": 3,
            },
        },
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_sensor(hass, mock_config_entry, "chat_health"))
    assert state is not None
    assert state.state == "degraded"


def _budget_status(**budget: float) -> dict[str, object]:
    """Return a minimal /api/status body carrying a budget object."""
    return {"ready": True, "budget": budget}


async def test_budget_sensor_near_cap(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A budget with a cap reports spend, remaining, fraction and a near-cap flag."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status", json=_budget_status(limit=10.0, spent=9.0)
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_sensor(hass, mock_config_entry, "budget"))
    assert state is not None
    assert float(state.state) == 9.0
    assert state.attributes["limit"] == 10.0
    assert state.attributes["remaining"] == 1.0
    assert state.attributes["fraction_used"] == 0.9
    assert state.attributes["near_cap"] is True


async def test_budget_sensor_below_cap(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Well under the cap → not near_cap."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status", json=_budget_status(limit=10.0, spent=2.0)
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_sensor(hass, mock_config_entry, "budget"))
    assert state.attributes["near_cap"] is False
    assert state.attributes["fraction_used"] == 0.2
    assert state.attributes["remaining"] == 8.0


async def test_budget_sensor_unlimited(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An unlimited cap (limit 0) leaves the cap-derived attributes null."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status", json=_budget_status(limit=0.0, spent=2.0)
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_sensor(hass, mock_config_entry, "budget"))
    assert float(state.state) == 2.0
    assert state.attributes.get("remaining") is None
    assert state.attributes.get("fraction_used") is None
    assert state.attributes["near_cap"] is False


async def test_budget_sensor_unavailable_without_field(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """An add-on that doesn't report a budget leaves the sensor unavailable."""
    await setup_integration(hass, mock_config_entry)
    state = hass.states.get(_sensor(hass, mock_config_entry, "budget"))
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


def test_budget_sensor_none_data_guards(mock_config_entry: MockConfigEntry) -> None:
    """The None-budget guards return safe defaults (unreachable via the entity)."""
    coordinator = MagicMock()
    coordinator.config_entry = mock_config_entry
    coordinator.data = StatusResult(
        ready=True,
        version=None,
        claude_version=None,
        model=None,
        ha_mcp=None,
        ha_mcp_connected=None,
        budget=None,
    )
    sensor = ClaudeBudgetSensor(coordinator)
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


async def test_status_poll_updates_client_read_timeout(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A status poll feeds the add-on's prompt budget into the client's timeout."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={"ready": True, "prompt_timeout_ms": 200000},
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.runtime_data.client.read_timeout == 215.0


def test_chat_health_sensor_none_data_guards(
    mock_config_entry: MockConfigEntry,
) -> None:
    """The None-data guards return safe defaults (unreachable via the entity state).

    When chat_health is absent the sensor is unavailable, so HA never calls these;
    this exercises the defensive guards directly.
    """
    coordinator = MagicMock()
    coordinator.config_entry = mock_config_entry
    coordinator.data = StatusResult(
        ready=True,
        version=None,
        claude_version=None,
        model=None,
        ha_mcp=None,
        ha_mcp_connected=None,
        chat_health=None,
    )
    sensor = ClaudeChatHealthSensor(coordinator)
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


async def test_usage_sensors(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """The usage and cost sensors reflect the /api/usage report."""
    await setup_integration(hass, mock_config_entry)

    usage = hass.states.get(_sensor(hass, mock_config_entry, "usage"))
    assert usage is not None
    assert usage.state == "1500"  # today input 1000 + output 500
    assert usage.attributes["messages"]["today"] == 12
    assert usage.attributes["unit_of_measurement"] == "tokens"

    cost = hass.states.get(_sensor(hass, mock_config_entry, "prompt_api_cost"))
    assert cost is not None
    assert float(cost.state) == 1.23
    assert cost.attributes["today"] == 0.12


async def test_usage_unavailable_at_setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """If usage can't be produced, the usage sensors are unavailable but setup ok."""
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", json={"ready": True})
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/usage", status=503, json={"error": "usage unavailable"}
    )
    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert (
        hass.states.get(_sensor(hass, mock_config_entry, "usage")).state
        == STATE_UNAVAILABLE
    )
    assert (
        hass.states.get(_sensor(hass, mock_config_entry, "prompt_api_cost")).state
        == STATE_UNAVAILABLE
    )

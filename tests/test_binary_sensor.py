"""Tests for the Claude active-alerts binary sensor."""

from __future__ import annotations

from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.api import StatusResult
from custom_components.claude_ha.binary_sensor import ClaudeAlertsBinarySensor
from custom_components.claude_ha.const import DOMAIN
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import TEST_BASE_URL, USAGE_PAYLOAD, setup_integration


def _alerts_binary_sensor(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_alerts_active"
    )
    assert entity_id is not None
    return entity_id


def _alerts_status(**alerts: object) -> dict[str, object]:
    """Return a minimal /api/status body carrying an alerts object."""
    return {"ready": True, "alerts": alerts}


async def test_alerts_binary_sensor_on(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An active alert set turns the sensor on and surfaces counts + items."""
    items = [
        {
            "key": "offline:device_tracker.ucg_fiber",
            "critical": True,
            "line": "Offline: UCG Fiber",
        },
        {
            "key": "co2:sensor.bedroom_co2",
            "critical": False,
            "line": "High CO2: Bedroom CO2 (1850 ppm)",
        },
    ]
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json=_alerts_status(active=2, critical=1, items=items),
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_alerts_binary_sensor(hass, mock_config_entry))
    assert state is not None
    assert state.state == STATE_ON
    assert state.attributes["device_class"] == "problem"
    assert state.attributes["active_count"] == 2
    assert state.attributes["critical_count"] == 1
    assert state.attributes["items"] == items


async def test_alerts_binary_sensor_off(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An empty active set (proactive alerts on, nothing wrong) reads off."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json=_alerts_status(active=0, critical=0, items=[]),
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_alerts_binary_sensor(hass, mock_config_entry))
    assert state is not None
    assert state.state == STATE_OFF
    assert state.attributes["active_count"] == 0
    assert state.attributes["items"] == []


async def test_alerts_binary_sensor_unavailable_without_field(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An add-on that doesn't report alerts (< 1.39.0) leaves the sensor unavailable."""
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", json={"ready": True})
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_alerts_binary_sensor(hass, mock_config_entry))
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


async def test_alerts_binary_sensor_null_is_unavailable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A v1.39.0 add-on with proactive alerts off reports null → unavailable."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status", json={"ready": True, "alerts": None}
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_alerts_binary_sensor(hass, mock_config_entry))
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


def test_alerts_binary_sensor_none_data_guards(
    mock_config_entry: MockConfigEntry,
) -> None:
    """The None-alerts guards return safe defaults (unreachable via the entity state).

    When alerts is absent the sensor is unavailable, so HA never calls these; this
    exercises the defensive guards directly for coverage.
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
        alerts=None,
    )
    sensor = ClaudeAlertsBinarySensor(coordinator)
    assert sensor.is_on is None
    assert sensor.extra_state_attributes == {}

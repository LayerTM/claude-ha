"""Tests for the Claude status and usage sensors."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.const import DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

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
    assert state.attributes["version"] == "1.7.0"
    assert state.attributes["ha_mcp"] is True


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

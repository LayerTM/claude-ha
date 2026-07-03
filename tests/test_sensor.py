"""Tests for the Claude status sensor."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant

from .conftest import TEST_BASE_URL, setup_integration


def _sensor_id(hass: HomeAssistant) -> str:
    ids = hass.states.async_entity_ids("sensor")
    assert ids
    return ids[0]


async def test_status_sensor_ready(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """The status sensor reports ready with version/model attributes."""
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_sensor_id(hass))
    assert state is not None
    assert state.state == "ready"
    assert state.attributes["claude_version"] == "2.0.1"
    assert state.attributes["model"] == "claude-sonnet-4-6"
    assert state.attributes["version"] == "1.2.3"


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
            "version": "1.2.3",
            "claude_version": None,
            "model": None,
        },
    )
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(_sensor_id(hass))
    assert state is not None
    assert state.state == "initializing"


async def test_status_sensor_becomes_unavailable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A poll failure after a healthy load marks the sensor unavailable."""
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", json={"ready": True})
    await setup_integration(hass, mock_config_entry)
    sensor_id = _sensor_id(hass)
    assert hass.states.get(sensor_id).state == "ready"

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", status=500)
    await mock_config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(sensor_id).state == STATE_UNAVAILABLE
    assert mock_config_entry.state is ConfigEntryState.LOADED

"""Tests for the health-check button."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.const import DOMAIN
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import setup_integration


def _button(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_check_health"
    )
    assert entity_id is not None
    return entity_id


async def test_press_probes_and_refreshes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    mock_prompt: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Pressing fires a probe read and re-polls status."""
    await setup_integration(hass, mock_config_entry)
    aioclient_mock.mock_calls.clear()

    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: _button(hass, mock_config_entry)},
        blocking=True,
    )

    methods = [(c[0], c[1].path) for c in aioclient_mock.mock_calls]
    assert ("POST", "/api/prompt") in methods  # the probe read
    assert ("GET", "/api/status") in methods  # the re-poll

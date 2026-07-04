"""Tests for the options flow."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.claude_ha.const import CONF_AUTO_EXECUTE, CONF_CRITICAL_ENTITIES
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import setup_integration


async def test_options_flow(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """The options flow saves auto-execute and critical-entities settings."""
    await setup_integration(hass, mock_config_entry)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_AUTO_EXECUTE: False, CONF_CRITICAL_ENTITIES: ["light.special"]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options[CONF_AUTO_EXECUTE] is False
    assert mock_config_entry.options[CONF_CRITICAL_ENTITIES] == ["light.special"]

"""Tests for config-entry diagnostics."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.claude_ha.const import CONF_HOST, CONF_TOKEN
from custom_components.claude_ha.diagnostics import (
    async_get_config_entry_diagnostics,
)
from homeassistant.core import HomeAssistant

from .conftest import TEST_HOST, setup_integration


async def test_diagnostics_redacts_token(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """Diagnostics expose status but redact the bearer token."""
    await setup_integration(hass, mock_config_entry)

    data = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert data["entry_data"][CONF_HOST] == TEST_HOST
    assert data["entry_data"][CONF_TOKEN] == "**REDACTED**"
    assert data["status"]["ready"] is True
    assert data["status"]["model"] == "claude-sonnet-4-6"

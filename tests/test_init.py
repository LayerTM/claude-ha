"""Tests for setup and teardown of the Claude integration."""

from __future__ import annotations

from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.const import (
    DOMAIN,
    ISSUE_ADDON_NOT_INSTALLED,
    ISSUE_ADDON_NOT_RUNNING,
)
from homeassistant.components.hassio import AddonState
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .conftest import TEST_BASE_URL, make_addon_info, setup_integration


async def test_setup_and_unload(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """A healthy entry sets up and unloads cleanly."""
    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.services.has_service(DOMAIN, "ask")

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_status_unreachable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An unreachable add-on leaves the entry in retry."""
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", status=500)
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_on_supervisor_running(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    mock_addon_manager: object,
) -> None:
    """On Supervisor with the add-on running, setup proceeds."""
    with patch("custom_components.claude_ha.is_hassio", return_value=True):
        await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_setup_addon_task_in_progress(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    mock_addon_manager,
) -> None:
    """A pending Supervisor task defers setup."""
    mock_addon_manager.task_in_progress.return_value = True
    with patch("custom_components.claude_ha.is_hassio", return_value=True):
        await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_addon_not_running_creates_issue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    mock_addon_manager,
) -> None:
    """A stopped add-on schedules a start and raises a repair issue."""
    mock_addon_manager.async_get_addon_info.return_value = make_addon_info(
        AddonState.NOT_RUNNING
    )
    with patch("custom_components.claude_ha.is_hassio", return_value=True):
        await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    mock_addon_manager.async_schedule_start_addon.assert_called_once()
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, ISSUE_ADDON_NOT_RUNNING) is not None


async def test_setup_addon_not_installed_creates_issue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    mock_addon_manager,
) -> None:
    """A missing add-on schedules install+setup and raises a repair issue."""
    mock_addon_manager.async_get_addon_info.return_value = make_addon_info(
        AddonState.NOT_INSTALLED
    )
    with patch("custom_components.claude_ha.is_hassio", return_value=True):
        await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    mock_addon_manager.async_schedule_install_setup_addon.assert_called_once()
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, ISSUE_ADDON_NOT_INSTALLED) is not None


async def test_setup_addon_info_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    mock_addon_manager,
) -> None:
    """A Supervisor error while reading add-on info defers setup."""
    from homeassistant.components.hassio import AddonError

    mock_addon_manager.async_get_addon_info.side_effect = AddonError("boom")
    with patch("custom_components.claude_ha.is_hassio", return_value=True):
        await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY

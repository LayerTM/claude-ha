"""Tests for setup and teardown of the Claude integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.const import (
    ADDON_OUTAGE_GRACE,
    DOMAIN,
    HEALTH_ISSUES,
    ISSUE_ADDON_NOT_INSTALLED,
    ISSUE_ADDON_NOT_RUNNING,
    ISSUE_NO_HA_TOKEN,
    ISSUE_USAGE_HISTORY_RESET,
)
from custom_components.claude_ha.issues import async_raise_issue, entry_issue_id
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


async def test_unload_clears_only_its_own_health_issues(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """Unloading one entry leaves another entry's repairs in place."""
    await setup_integration(hass, mock_config_entry)
    registry = ir.async_get(hass)
    for entry_id in (mock_config_entry.entry_id, "other-entry"):
        async_raise_issue(
            hass, entry_id, ISSUE_NO_HA_TOKEN, severity=ir.IssueSeverity.ERROR
        )

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert (
        registry.async_get_issue(
            DOMAIN, entry_issue_id(ISSUE_NO_HA_TOKEN, mock_config_entry.entry_id)
        )
        is None
    )
    assert (
        registry.async_get_issue(
            DOMAIN, entry_issue_id(ISSUE_NO_HA_TOKEN, "other-entry")
        )
        is not None
    )


async def test_remove_clears_every_issue_of_the_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """A removed entry leaves no repair of its own behind, add-on ones included."""
    await setup_integration(hass, mock_config_entry)
    kinds = (
        *HEALTH_ISSUES,
        ISSUE_ADDON_NOT_INSTALLED,
        ISSUE_ADDON_NOT_RUNNING,
        ISSUE_USAGE_HISTORY_RESET,
    )
    for kind in kinds:
        async_raise_issue(
            hass, mock_config_entry.entry_id, kind, severity=ir.IssueSeverity.ERROR
        )

    assert await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    registry = ir.async_get(hass)
    for kind in kinds:
        assert (
            registry.async_get_issue(
                DOMAIN, entry_issue_id(kind, mock_config_entry.entry_id)
            )
            is None
        )


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


async def test_setup_leaves_stopped_addon_to_supervisor(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    mock_addon_manager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stopped add-on at setup is waited for, not started, and not a repair.

    At boot the Supervisor starts the add-on right after Core; starting it from
    setup raced that and flashed a repair on every Home Assistant restart.
    """
    mock_addon_manager.async_get_addon_info.return_value = make_addon_info(
        AddonState.NOT_RUNNING
    )
    with patch("custom_components.claude_ha.is_hassio", return_value=True):
        await setup_integration(hass, mock_config_entry)

        assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
        mock_addon_manager.async_schedule_start_addon.assert_not_called()
        registry = ir.async_get(hass)
        assert (
            registry.async_get_issue(
                DOMAIN,
                entry_issue_id(ISSUE_ADDON_NOT_RUNNING, mock_config_entry.entry_id),
            )
            is None
        )

        # The Supervisor brings the add-on up; the next setup retry succeeds.
        mock_addon_manager.async_get_addon_info.return_value = make_addon_info()
        freezer.tick(timedelta(seconds=11))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    mock_addon_manager.async_schedule_start_addon.assert_not_called()
    loud = [
        r
        for r in caplog.records
        if r.name.startswith("custom_components.claude_ha")
        and r.levelno >= logging.WARNING
    ]
    assert loud == []


async def test_setup_stopped_addon_raises_repair_after_grace(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    mock_addon_manager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An add-on still stopped past the grace period gets a WARNING and a repair."""
    mock_addon_manager.async_get_addon_info.return_value = make_addon_info(
        AddonState.NOT_RUNNING
    )
    with patch("custom_components.claude_ha.is_hassio", return_value=True):
        await setup_integration(hass, mock_config_entry)
        freezer.tick(ADDON_OUTAGE_GRACE + timedelta(seconds=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    mock_addon_manager.async_schedule_start_addon.assert_not_called()
    registry = ir.async_get(hass)
    assert (
        registry.async_get_issue(
            DOMAIN, entry_issue_id(ISSUE_ADDON_NOT_RUNNING, mock_config_entry.entry_id)
        )
        is not None
    )
    warnings = [
        r
        for r in caplog.records
        if r.name.startswith("custom_components.claude_ha")
        and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1


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
    assert (
        registry.async_get_issue(
            DOMAIN,
            entry_issue_id(ISSUE_ADDON_NOT_INSTALLED, mock_config_entry.entry_id),
        )
        is not None
    )


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

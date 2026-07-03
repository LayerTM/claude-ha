"""Tests for the repair flows."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.claude_ha.const import ISSUE_ADDON_NOT_RUNNING
from custom_components.claude_ha.repairs import (
    AddonNotRunningRepairFlow,
    async_create_fix_flow,
)
from homeassistant.components.hassio import AddonError
from homeassistant.components.repairs import ConfirmRepairFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import TEST_SLUG


async def test_addon_not_running_fix_flow(
    hass: HomeAssistant, mock_addon_manager: MagicMock
) -> None:
    """Confirming the repair starts the add-on."""
    flow = await async_create_fix_flow(
        hass, ISSUE_ADDON_NOT_RUNNING, {"addon_slug": TEST_SLUG}
    )
    assert isinstance(flow, AddonNotRunningRepairFlow)
    flow.hass = hass

    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"

    result = await flow.async_step_confirm({})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    mock_addon_manager.async_start_addon.assert_called_once()


async def test_addon_not_running_fix_flow_start_error(
    hass: HomeAssistant, mock_addon_manager: MagicMock
) -> None:
    """A start failure aborts the repair flow."""
    mock_addon_manager.async_start_addon.side_effect = AddonError("no")
    flow = await async_create_fix_flow(
        hass, ISSUE_ADDON_NOT_RUNNING, {"addon_slug": TEST_SLUG}
    )
    flow.hass = hass

    await flow.async_step_init()
    result = await flow.async_step_confirm({})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "start_failed"


async def test_unknown_issue_uses_confirm_flow(hass: HomeAssistant) -> None:
    """An unknown issue id falls back to a plain confirm flow."""
    flow = await async_create_fix_flow(hass, "something_else", None)
    assert isinstance(flow, ConfirmRepairFlow)

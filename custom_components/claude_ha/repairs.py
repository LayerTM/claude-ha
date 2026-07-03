"""Repair flows for the Claude for Home Assistant integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.hassio import AddonError
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .addon import get_addon_manager
from .const import ISSUE_ADDON_NOT_RUNNING, LOGGER


class AddonNotRunningRepairFlow(RepairsFlow):
    """Offer to start the stopped Claude Code add-on."""

    def __init__(self, slug: str) -> None:
        """Store the resolved add-on slug to act on."""
        self._slug = slug

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the first step of the fix flow."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Start the add-on on confirmation."""
        if user_input is not None:
            try:
                await get_addon_manager(self.hass, self._slug).async_start_addon()
            except AddonError as err:
                LOGGER.error("Repair could not start the Claude Code add-on: %s", err)
                return self.async_abort(reason="start_failed")
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the fix flow for a repair issue."""
    if issue_id == ISSUE_ADDON_NOT_RUNNING and data and "addon_slug" in data:
        return AddonNotRunningRepairFlow(str(data["addon_slug"]))
    return ConfirmRepairFlow()

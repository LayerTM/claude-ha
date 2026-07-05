"""Coordinators and runtime data for the Claude for Home Assistant integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ClaudeClient, ClaudeError, StatusResult, UsageResult
from .const import DOMAIN, LOGGER, SCAN_INTERVAL, USAGE_SCAN_INTERVAL

type ClaudeConfigEntry = ConfigEntry[ClaudeRuntimeData]


@dataclass
class ClaudeRuntimeData:
    """Objects shared across an entry's platforms."""

    client: ClaudeClient
    status: ClaudeStatusCoordinator
    usage: ClaudeUsageCoordinator


class ClaudeStatusCoordinator(DataUpdateCoordinator[StatusResult]):
    """Polls the add-on's ``/api/status`` endpoint for the status sensor."""

    config_entry: ClaudeConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ClaudeConfigEntry,
        client: ClaudeClient,
    ) -> None:
        """Init the coordinator with its API client."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_status",
            update_interval=timedelta(seconds=SCAN_INTERVAL),
        )
        self.client = client

    async def _async_update_data(self) -> StatusResult:
        """Fetch the latest add-on status."""
        try:
            status = await self.client.async_get_status()
        except ClaudeError as err:
            raise UpdateFailed(str(err) or "Add-on status unavailable") from err
        # Keep the prompt wall-clock just above the add-on's reported budget.
        self.client.note_prompt_timeout(status.prompt_timeout_ms)
        return status


class ClaudeUsageCoordinator(DataUpdateCoordinator[UsageResult]):
    """Polls the add-on's ``/api/usage`` endpoint (slow; cached add-on side)."""

    config_entry: ClaudeConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ClaudeConfigEntry,
        client: ClaudeClient,
    ) -> None:
        """Init the usage coordinator with its API client."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_usage",
            update_interval=timedelta(seconds=USAGE_SCAN_INTERVAL),
        )
        self.client = client

    async def _async_update_data(self) -> UsageResult:
        """Fetch the latest usage report."""
        try:
            return await self.client.async_get_usage()
        except ClaudeError as err:
            raise UpdateFailed(str(err) or "Usage unavailable") from err

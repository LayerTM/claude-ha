"""Status coordinator for the Claude for Home Assistant integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ClaudeClient, ClaudeError, StatusResult
from .const import DOMAIN, LOGGER, SCAN_INTERVAL

type ClaudeConfigEntry = ConfigEntry[ClaudeCoordinator]


class ClaudeCoordinator(DataUpdateCoordinator[StatusResult]):
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
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL),
        )
        self.client = client

    async def _async_update_data(self) -> StatusResult:
        """Fetch the latest add-on status."""
        try:
            return await self.client.async_get_status()
        except ClaudeError as err:
            raise UpdateFailed(str(err) or "Add-on status unavailable") from err

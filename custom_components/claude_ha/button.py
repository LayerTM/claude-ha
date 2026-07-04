"""Button platform for the Claude for Home Assistant integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import ClaudeConfigEntry, ClaudeStatusCoordinator
from .entity import build_device_info
from .health import async_probe

# Pressing runs a probe + refresh; keep them serialized rather than concurrent.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClaudeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the health-check button from a config entry."""
    async_add_entities([ClaudeHealthButton(entry.runtime_data.status)])


class ClaudeHealthButton(CoordinatorEntity[ClaudeStatusCoordinator], ButtonEntity):
    """Runs a deep health probe (a tiny read) and re-evaluates the repairs."""

    _attr_has_entity_name = True
    _attr_translation_key = "check_health"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ClaudeStatusCoordinator) -> None:
        """Init from the status coordinator (for its client and device)."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_check_health"
        self._attr_device_info = build_device_info(entry)

    async def async_press(self) -> None:
        """Probe the add-on, then refresh status so health is re-evaluated."""
        await async_probe(self.hass, self.coordinator.client)
        await self.coordinator.async_request_refresh()

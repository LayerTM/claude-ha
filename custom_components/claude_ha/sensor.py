"""Status sensor for the Claude for Home Assistant integration."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import STATUS_CLAUDE_VERSION, STATUS_MODEL, STATUS_VERSION
from .coordinator import ClaudeConfigEntry, ClaudeCoordinator
from .entity import build_device_info

# Read-only sensor fed by the coordinator; no outbound writes to serialize.
PARALLEL_UPDATES = 0

# Possible states of the status sensor (SensorDeviceClass.ENUM).
STATE_READY = "ready"
STATE_INITIALIZING = "initializing"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClaudeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the status sensor from a config entry."""
    async_add_entities([ClaudeStatusSensor(entry.runtime_data)])


class ClaudeStatusSensor(CoordinatorEntity[ClaudeCoordinator], SensorEntity):
    """Reports whether the add-on is ready, plus version/model attributes."""

    _attr_has_entity_name = True
    _attr_translation_key = "status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, coordinator: ClaudeCoordinator) -> None:
        """Init from the runtime coordinator."""
        super().__init__(coordinator)
        self._attr_options = [STATE_READY, STATE_INITIALIZING]
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_status"
        status = coordinator.data
        self._attr_device_info = build_device_info(
            entry,
            claude_version=status.claude_version if status else None,
            model=status.model if status else None,
        )

    @property
    def native_value(self) -> str:
        """Return whether the add-on reports itself ready."""
        return STATE_READY if self.coordinator.data.ready else STATE_INITIALIZING

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Expose the add-on and Claude versions and active model."""
        data = self.coordinator.data
        return {
            STATUS_VERSION: data.version,
            STATUS_CLAUDE_VERSION: data.claude_version,
            STATUS_MODEL: data.model,
        }

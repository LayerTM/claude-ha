"""Sensors for the Claude for Home Assistant integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    STATUS_CLAUDE_VERSION,
    STATUS_HA_MCP,
    STATUS_HA_MCP_CONNECTED,
    STATUS_MODEL,
    STATUS_VERSION,
)
from .coordinator import (
    ClaudeConfigEntry,
    ClaudeStatusCoordinator,
    ClaudeUsageCoordinator,
)
from .entity import build_device_info
from .health import evaluate as evaluate_health

# Read-only sensors fed by coordinators; no outbound writes to serialize.
PARALLEL_UPDATES = 0

# Possible states of the status sensor (SensorDeviceClass.ENUM).
STATE_READY = "ready"
STATE_INITIALIZING = "initializing"

# Possible states of the chat-health sensor (soft indicator, not a repair).
STATE_CHAT_OK = "ok"
STATE_CHAT_DEGRADED = "degraded"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClaudeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the status and usage sensors from a config entry."""
    data = entry.runtime_data
    async_add_entities(
        [
            ClaudeStatusSensor(data.status),
            ClaudeChatHealthSensor(data.status),
            ClaudeUsageSensor(data.usage),
            ClaudeCostSensor(data.usage),
        ]
    )


class ClaudeStatusSensor(CoordinatorEntity[ClaudeStatusCoordinator], SensorEntity):
    """Reports whether the add-on is ready, plus version/model attributes."""

    _attr_has_entity_name = True
    _attr_translation_key = "status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, coordinator: ClaudeStatusCoordinator) -> None:
        """Init from the status coordinator."""
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
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose versions, model, HA-MCP flags and the current health summary."""
        data = self.coordinator.data
        report = evaluate_health(self.hass, data)
        return {
            STATUS_VERSION: data.version,
            STATUS_CLAUDE_VERSION: data.claude_version,
            STATUS_MODEL: data.model,
            STATUS_HA_MCP: data.ha_mcp,
            STATUS_HA_MCP_CONNECTED: data.ha_mcp_connected,
            "health": report.problem or "ok",
            "exposed_to_assist": report.exposed_to_assist,
        }


class ClaudeChatHealthSensor(CoordinatorEntity[ClaudeStatusCoordinator], SensorEntity):
    """Soft indicator of recent chat reliability (degraded vs recovered reads).

    A glanceable diagnostic — never a repair — surfacing the add-on's rolling
    chat-health summary. ``degraded`` counts reads that failed even after a retry;
    ``recovered`` counts reads a retry rescued (a success, so it stays "ok").
    """

    _attr_has_entity_name = True
    _attr_translation_key = "chat_health"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, coordinator: ClaudeStatusCoordinator) -> None:
        """Init from the status coordinator."""
        super().__init__(coordinator)
        self._attr_options = [STATE_CHAT_OK, STATE_CHAT_DEGRADED]
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_chat_health"
        self._attr_device_info = build_device_info(entry)

    @property
    def available(self) -> bool:
        """Unavailable on add-ons that don't report chat health (< 1.20.0)."""
        data = self.coordinator.data
        return super().available and data is not None and data.chat_health is not None

    @property
    def native_value(self) -> str | None:
        """Degraded when a recent read failed even after retry, else OK."""
        health = self.coordinator.data.chat_health
        if health is None:
            return None
        return STATE_CHAT_DEGRADED if health.degraded > 0 else STATE_CHAT_OK

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the rolling counts and the last failure reason token."""
        health = self.coordinator.data.chat_health
        if health is None:
            return {}
        return {
            "recent": health.recent,
            "degraded": health.degraded,
            "recovered": health.recovered,
            "last_reason": health.last_reason,
        }


class ClaudeUsageSensor(CoordinatorEntity[ClaudeUsageCoordinator], SensorEntity):
    """Today's Claude token usage, with the full report as attributes."""

    _attr_has_entity_name = True
    _attr_translation_key = "usage"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "tokens"

    def __init__(self, coordinator: ClaudeUsageCoordinator) -> None:
        """Init from the usage coordinator."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_usage"
        self._attr_device_info = build_device_info(entry)

    @property
    def native_value(self) -> int | None:
        """Return today's input + output tokens."""
        return self.coordinator.data.today_tokens if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the full usage report."""
        return self.coordinator.data.report if self.coordinator.data else {}


class ClaudeCostSensor(CoordinatorEntity[ClaudeUsageCoordinator], SensorEntity):
    """Total prompt-API dollar cost (interactive-console usage is tokens only)."""

    _attr_has_entity_name = True
    _attr_translation_key = "prompt_api_cost"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "USD"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: ClaudeUsageCoordinator) -> None:
        """Init from the usage coordinator."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_prompt_api_cost"
        self._attr_device_info = build_device_info(entry)

    @property
    def native_value(self) -> float | None:
        """Return the total prompt-API cost in USD."""
        return self.coordinator.data.cost_total if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose today's prompt-API cost alongside the total."""
        data = self.coordinator.data
        return {"today": data.cost_today if data else None}

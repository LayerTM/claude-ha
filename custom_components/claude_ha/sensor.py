"""Sensors for the Claude for Home Assistant integration."""

from __future__ import annotations

from datetime import datetime
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
from homeassistant.util import dt as dt_util

from .const import (
    CHAT_HEALTH_DEGRADED_RATE,
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

# Fraction of the daily budget at which the soft "near the cap" flag trips.
_NEAR_CAP_FRACTION = 0.9


def _as_utc(epoch_ms: int | None) -> datetime | None:
    """Render a contract epoch-ms timestamp as a UTC datetime attribute."""
    return None if epoch_ms is None else dt_util.utc_from_timestamp(epoch_ms / 1000)


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
            ClaudeBudgetSensor(data.status),
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

    The state asks one question — are the failures still current? — and reads
    whichever evidence the add-on offers: an unbroken run of failures right now,
    the failure RATE across the window, a clean run of successes that outlasts the
    failures before it, and how long ago the last failure was. Never "a failure
    exists": the add-on trims its window by count, never by age, so a single blip
    used to pin the sensor to "degraded" until fifty further chats pushed it out.
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
        """Unavailable when the add-on reports no readable chat health.

        Two causes, and the second is the one worth knowing while debugging: an
        add-on older than 1.20.0 doesn't send the block at all, and a block whose
        counts can't be read is dropped rather than defaulted, because a count
        defaulted to zero would read as "no failures".
        """
        data = self.coordinator.data
        return super().available and data is not None and data.chat_health is not None

    @property
    def native_value(self) -> str | None:
        """Degraded while the failures look current, cleared once they don't.

        Two clauses raise it and two clear it, and each rests on a different
        field, so an add-on that reports fewer of them still gets a sound answer
        from the rest. An add-on older than 1.49.0 reports neither run counter nor
        stamps and is judged on the rate alone; absence never clears a warning,
        and never invents one.

        **Raising.** A run of failures right now is an outage whatever the rate
        says — a rate over a count-trimmed window cannot see a fresh outage until
        it has diluted that window. Otherwise the rate itself: frequency is what
        says a problem is real, and it stays the necessary condition for the
        slower path, because one fresh failure the add-on already retried is
        exactly the blip this sensor stopped shouting about.

        **Clearing.** A clean run that outlasts the failures behind it has
        demonstrated recovery; a last failure past the staleness horizon has aged
        out. Neither can clear a live run of failures, because both are checked
        after it.
        """
        health = self.coordinator.data.chat_health
        if health is None:
            return None
        stale = health.is_failure_stale(dt_util.utcnow())
        if health.is_outage_run and not stale:
            return STATE_CHAT_DEGRADED
        if health.failure_rate < CHAT_HEALTH_DEGRADED_RATE:
            return STATE_CHAT_OK
        if health.has_recovered or stale:
            return STATE_CHAT_OK
        return STATE_CHAT_DEGRADED

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the counts, the rate the state turns on, and the window's clock.

        The timestamps are ``None`` on an add-on older than 1.49.0, and on history
        it wrote before it started stamping.
        """
        health = self.coordinator.data.chat_health
        if health is None:
            return {}
        return {
            "recent": health.recent,
            "recent_ok": health.recent - health.degraded,
            "degraded": health.degraded,
            "recovered": health.recovered,
            "failure_rate": round(health.failure_rate, 3),
            "consecutive_ok": health.consecutive_ok,
            "consecutive_failed": health.consecutive_failed,
            "last_reason": health.last_reason,
            "last_failure": _as_utc(health.last_failure_ts),
            "window_from": _as_utc(health.window_from_ts),
            "window_to": _as_utc(health.window_to_ts),
        }


class ClaudeBudgetSensor(CoordinatorEntity[ClaudeStatusCoordinator], SensorEntity):
    """Today's spend against the add-on's daily budget, with a soft near-cap flag.

    A diagnostic dollar sensor — never a repair. The value is today's spend;
    attributes carry the cap, remaining, fraction used and a ``near_cap`` flag. An
    unlimited cap (limit 0) leaves the cap-derived attributes null; an add-on that
    reports no budget leaves the sensor unavailable.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "budget"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "USD"

    def __init__(self, coordinator: ClaudeStatusCoordinator) -> None:
        """Init from the status coordinator."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_budget"
        self._attr_device_info = build_device_info(entry)

    @property
    def available(self) -> bool:
        """Unavailable on add-ons that don't report a budget (< 1.21.0)."""
        data = self.coordinator.data
        return super().available and data is not None and data.budget is not None

    @property
    def native_value(self) -> float | None:
        """Today's spend in USD."""
        budget = self.coordinator.data.budget
        return budget.spent if budget is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Cap, remaining, fraction used and the soft near-cap flag."""
        budget = self.coordinator.data.budget
        if budget is None:
            return {}
        limited = budget.limit > 0
        return {
            "limit": budget.limit,
            "remaining": budget.limit - budget.spent if limited else None,
            "fraction_used": budget.spent / budget.limit if limited else None,
            "near_cap": limited and budget.spent >= _NEAR_CAP_FRACTION * budget.limit,
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

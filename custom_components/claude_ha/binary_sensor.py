"""Binary sensors for the Claude for Home Assistant integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import ClaudeConfigEntry, ClaudeStatusCoordinator
from .entity import build_device_info

# Read-only entity fed by the status coordinator; no outbound writes to serialize.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClaudeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the active-alerts binary sensor from a config entry."""
    async_add_entities([ClaudeAlertsBinarySensor(entry.runtime_data.status)])


class ClaudeAlertsBinarySensor(
    CoordinatorEntity[ClaudeStatusCoordinator], BinarySensorEntity
):
    """On while the add-on has any active proactive alert (add-on >= 1.39.0).

    A queryable mirror of the add-on's deterministic alert set (leak / offline /
    battery / CO2 / …), so the user's own automations and history can react to it,
    not just the push notification. ``critical_count`` and each item's ``critical``
    flag distinguish always-sent alerts (leak, offline) from the rest.

    Primary (no diagnostic category) because it is meant to be acted on. Unavailable
    on add-ons that don't report the field and while ``alerts`` is null (proactive
    alerts off or not yet run) — both fold to ``data.alerts is None``.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "alerts_active"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: ClaudeStatusCoordinator) -> None:
        """Init from the status coordinator."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_alerts_active"
        self._attr_device_info = build_device_info(entry)

    @property
    def available(self) -> bool:
        """Unavailable on add-ons that don't report alerts (< 1.39.0) or when null."""
        data = self.coordinator.data
        return super().available and data is not None and data.alerts is not None

    @property
    def is_on(self) -> bool | None:
        """On when any anomaly is active. Driven by the item list, not the count.

        The contract guarantees ``active == len(items)``; deriving from ``items``
        anyway means a future count/list mismatch can never claim a problem with an
        empty list (or the reverse).
        """
        alerts = self.coordinator.data.alerts
        return None if alerts is None else bool(alerts.items)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the active counts and the full anomaly list.

        ``items``/``line`` are the user's OWN home entity names and readings — home
        data that already lives in their HA, not chat content — so unlike the
        chat-health sensor (which surfaces only a reason token, never prompt text)
        surfacing them here is intentional and safe.
        """
        alerts = self.coordinator.data.alerts
        if alerts is None:
            return {}
        return {
            "active_count": alerts.active,
            "critical_count": alerts.critical,
            "items": [
                {"key": item.key, "critical": item.critical, "line": item.line}
                for item in alerts.items
            ],
        }

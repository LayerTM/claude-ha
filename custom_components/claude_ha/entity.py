"""Shared entity plumbing for the Claude for Home Assistant integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import device_registry as dr

from .const import ADDON_NAME, DOMAIN, MANUFACTURER


def build_device_info(
    entry: ConfigEntry,
    *,
    claude_version: str | None = None,
    model: str | None = None,
) -> dr.DeviceInfo:
    """Build the single service device shared by all of this entry's entities."""
    return dr.DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=ADDON_NAME,
        manufacturer=MANUFACTURER,
        model=model or "Claude Code add-on",
        sw_version=claude_version,
        entry_type=dr.DeviceEntryType.SERVICE,
    )

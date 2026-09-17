"""Shared entity plumbing for the Claude for Home Assistant integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import device_registry as dr

from .api import StatusResult
from .const import DOMAIN
from .engines import engine_for_entry


def build_device_info(
    entry: ConfigEntry, status: StatusResult | None = None
) -> dr.DeviceInfo:
    """Build the single service device shared by all of this entry's entities."""
    engine = engine_for_entry(entry)
    assert engine is not None  # async_setup_entry refuses an unknown engine
    engine_version = None
    model = None
    if status is not None:
        engine_version = status.engine_version or status.claude_version
        model = status.model
    return dr.DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=engine.addon_name,
        manufacturer=engine.manufacturer,
        model=model or engine.device_model,
        sw_version=engine_version,
        entry_type=dr.DeviceEntryType.SERVICE,
    )

"""Diagnostics for the Claude for Home Assistant integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_TOKEN
from .coordinator import ClaudeConfigEntry

TO_REDACT = {CONF_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ClaudeConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry, with the bearer token redacted."""
    data = entry.runtime_data
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "status": asdict(data.status.data) if data.status.data else None,
        "usage": data.usage.data.report if data.usage.data else None,
    }

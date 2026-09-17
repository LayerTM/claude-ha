"""The AI engines a companion add-on can run, and how each one is presented.

An entry talks to one add-on, and that add-on runs one engine. The engine of an
entry is stored once, in ``entry.data[CONF_ENGINE]``, and checked against the
``engine`` the add-on reports in ``/api/status``. Everything engine-shaped
(names, the add-on slug suffix, the device's manufacturer) is looked up here by
that key. What an engine can do is not listed here: the add-on reports it
(inbound fields by their presence, request fields in ``request_fields``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from homeassistant.config_entries import ConfigEntry

from .const import CONF_ENGINE


@dataclass(frozen=True, slots=True)
class Engine:
    """How one engine and its add-on are identified and shown."""

    # Token in /api/status ``engine`` and in the entry's data.
    key: str
    # The engine as a user names it.
    name: str
    # The add-on's name: device name, entry title, add-on log lines.
    addon_name: str
    # The add-on's Supervisor slug is repository-prefixed and therefore varies
    # per install (e.g. "abc123de_claude-code", "local_claude-code"), so it is
    # matched by this suffix, never hardcoded.
    slug_suffix: str
    manufacturer: str
    device_model: str


CLAUDE: Final = Engine(
    key="claude",
    name="Claude",
    addon_name="Claude Code",
    slug_suffix="_claude-code",
    manufacturer="Anthropic",
    device_model="Claude Code add-on",
)

ENGINES: Final[Mapping[str, Engine]] = {engine.key: engine for engine in (CLAUDE,)}

# The engine of everything that predates the engine field: entries created
# before it was stored, and add-ons whose /api/status does not report one. Only
# Claude add-ons existed then.
LEGACY_ENGINE: Final = CLAUDE


def engine_for_slug(slug: str) -> Engine | None:
    """Return the engine whose add-on has slug ``slug``, if any."""
    for engine in ENGINES.values():
        if slug.endswith(engine.slug_suffix):
            return engine
    return None


def engine_for_entry(entry: ConfigEntry) -> Engine | None:
    """Return the engine an entry was created for, or ``None`` if unknown here."""
    return ENGINES.get(entry.data[CONF_ENGINE])

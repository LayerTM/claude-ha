"""Supervisor add-on management for the Claude Code companion add-on.

The add-on slug is repository-prefixed and varies per install, so it is resolved
at runtime (from discovery, or by matching :data:`ADDON_SLUG_SUFFIX` against the
installed/store add-on lists) rather than hardcoded.
"""

from __future__ import annotations

from homeassistant.components.hassio import (
    AddonManager,
    get_addons_info,
    get_supervisor_client,
)
from homeassistant.core import HomeAssistant, callback

from .const import ADDON_NAME, ADDON_SLUG_SUFFIX, DOMAIN, LOGGER

DATA_ADDON_MANAGERS = f"{DOMAIN}_addon_managers"


@callback
def get_addon_manager(hass: HomeAssistant, slug: str) -> AddonManager:
    """Return a cached :class:`AddonManager` for the resolved add-on slug.

    Cached per slug in ``hass.data`` (rather than via ``@singleton``) because the
    slug is only known at runtime. There is one Claude Code add-on per install,
    so the slug is stable for the lifetime of the entry.
    """
    managers: dict[str, AddonManager] = hass.data.setdefault(DATA_ADDON_MANAGERS, {})
    if slug not in managers:
        managers[slug] = AddonManager(hass, LOGGER, ADDON_NAME, slug)
    return managers[slug]


async def async_resolve_addon_slug(hass: HomeAssistant) -> str | None:
    """Find the Claude Code add-on's slug, or ``None`` if it is not available.

    Prefers an already-installed add-on (the common case: the user installed the
    add-on, which then advertised itself via discovery), and falls back to the
    add-on store so the config flow can offer to install it.
    """
    installed = _resolve_from_installed(hass)
    if installed is not None:
        return installed
    return await _resolve_from_store(hass)


@callback
def _resolve_from_installed(hass: HomeAssistant) -> str | None:
    """Match the slug suffix against installed add-ons (sync Supervisor cache)."""
    try:
        addons = get_addons_info(hass)
    except Exception:  # noqa: BLE001 - Supervisor data may not be loaded yet
        return None
    if not addons:
        return None
    return next(
        (slug for slug in addons if slug.endswith(ADDON_SLUG_SUFFIX)),
        None,
    )


async def _resolve_from_store(hass: HomeAssistant) -> str | None:
    """Match the slug suffix against store add-ons that are not yet installed."""
    client = get_supervisor_client(hass)
    try:
        store_addons = await client.store.addons_list()
    except Exception:  # noqa: BLE001 - store may be unavailable
        return None
    return next(
        (
            addon.slug
            for addon in store_addons
            if not addon.installed and addon.slug.endswith(ADDON_SLUG_SUFFIX)
        ),
        None,
    )

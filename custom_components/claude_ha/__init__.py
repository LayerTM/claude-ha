"""The Claude for Home Assistant integration."""

from __future__ import annotations

from homeassistant.components.hassio import (
    AddonError,
    AddonManager,
    AddonState,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.hassio import is_hassio
from homeassistant.helpers.typing import ConfigType

from .addon import get_addon_manager
from .api import ClaudeClient
from .confirm import async_setup_confirm
from .const import (
    CONF_ADDON_SLUG,
    CONF_HOST,
    CONF_PORT,
    CONF_TOKEN,
    DOMAIN,
    ISSUE_ADDON_NOT_INSTALLED,
    ISSUE_ADDON_NOT_RUNNING,
)
from .coordinator import ClaudeConfigEntry, ClaudeCoordinator
from .frontend import async_register_card
from .services import async_setup_services

PLATFORMS = (Platform.CONVERSATION, Platform.SENSOR)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services, the confirm listener and the card, before any entry."""
    async_setup_services(hass)
    async_setup_confirm(hass)
    await async_register_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ClaudeConfigEntry) -> bool:
    """Set up Claude from a config entry."""
    slug = entry.data.get(CONF_ADDON_SLUG)
    if slug and is_hassio(hass):
        await _async_ensure_addon_running(hass, entry, slug)

    client = ClaudeClient(
        async_get_clientsession(hass),
        base_url=f"http://{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}",
        token=entry.data[CONF_TOKEN],
    )
    coordinator = ClaudeCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ClaudeConfigEntry) -> bool:
    """Unload a config entry and its platforms."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_ensure_addon_running(
    hass: HomeAssistant, entry: ClaudeConfigEntry, slug: str
) -> None:
    """Make sure the companion add-on is installed and running before setup.

    Raises :class:`ConfigEntryNotReady` (so HA retries) and surfaces a
    user-actionable repair issue while the add-on is down; clears the issues
    once it is running.
    """
    addon: AddonManager = get_addon_manager(hass, slug)

    if addon.task_in_progress():
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="addon_not_ready"
        )

    try:
        info = await addon.async_get_addon_info()
    except AddonError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="addon_info_failed"
        ) from err

    if info.state is AddonState.NOT_INSTALLED:
        addon.async_schedule_install_setup_addon(info.options, catch_error=True)
        _create_addon_issue(hass, ISSUE_ADDON_NOT_INSTALLED, slug, fixable=False)
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="addon_not_installed"
        )

    if info.state is not AddonState.RUNNING:
        addon.async_schedule_start_addon(catch_error=True)
        _create_addon_issue(hass, ISSUE_ADDON_NOT_RUNNING, slug, fixable=True)
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="addon_not_running"
        )

    _clear_addon_issues(hass)


def _create_addon_issue(
    hass: HomeAssistant, issue_id: str, slug: str, *, fixable: bool
) -> None:
    """Raise a repair issue for a missing/stopped add-on."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=fixable,
        severity=ir.IssueSeverity.ERROR,
        translation_key=issue_id,
        translation_placeholders={"addon_slug": slug},
        data={"addon_slug": slug},
    )


def _clear_addon_issues(hass: HomeAssistant) -> None:
    """Remove any add-on availability repair issues."""
    for issue_id in (ISSUE_ADDON_NOT_RUNNING, ISSUE_ADDON_NOT_INSTALLED):
        ir.async_delete_issue(hass, DOMAIN, issue_id)

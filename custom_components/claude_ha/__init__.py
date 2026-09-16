"""The Claude for Home Assistant integration."""

from __future__ import annotations

from homeassistant.components.hassio import (
    AddonError,
    AddonManager,
    AddonState,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.hassio import is_hassio
from homeassistant.helpers.typing import ConfigType

from .addon import (
    AddonWatch,
    async_clear_addon_issues,
    async_create_addon_issue,
    async_drop_addon_watch,
    get_addon_manager,
    get_addon_watch,
)
from .api import ClaudeClient
from .confirm import async_setup_confirm
from .const import (
    CONF_ADDON_SLUG,
    CONF_CAMERA_VISION,
    CONF_HOST,
    CONF_PORT,
    CONF_TOKEN,
    DOMAIN,
    HEALTH_ISSUES,
    ISSUE_ADDON_NOT_INSTALLED,
    ISSUE_ADDON_NOT_RUNNING,
)
from .coordinator import (
    ClaudeAccountLimitsCoordinator,
    ClaudeConfigEntry,
    ClaudeRuntimeData,
    ClaudeStatusCoordinator,
    ClaudeUsageCoordinator,
)
from .frontend import (
    async_ensure_card_resource,
    async_register_card,
    async_remove_card_resource,
)
from .health import (
    async_apply as async_apply_health,
    debounce_mcp_unreachable,
    evaluate as evaluate_health,
)
from .issues import async_clear_issues
from .services import async_setup_services

PLATFORMS = (
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CONVERSATION,
    Platform.SENSOR,
)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services, the confirm listener and the card, before any entry."""
    async_setup_services(hass)
    async_setup_confirm(hass)
    await async_register_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ClaudeConfigEntry) -> bool:
    """Set up Claude from a config entry."""
    await async_ensure_card_resource(hass)
    # The add-on is Supervisor-managed only on a Supervisor install with a slug.
    slug: str | None = entry.data.get(CONF_ADDON_SLUG) if is_hassio(hass) else None
    if slug:
        await _async_ensure_addon_running(hass, entry, slug)

    client = ClaudeClient(
        async_get_clientsession(hass),
        base_url=f"http://{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}",
        token=entry.data[CONF_TOKEN],
    )
    watch = (
        get_addon_watch(hass, entry.entry_id, slug)
        if slug
        else AddonWatch(hass, entry.entry_id, None)
    )
    status = ClaudeStatusCoordinator(hass, entry, client, watch)
    await status.async_config_entry_first_refresh()

    # Usage is supplementary and needs add-on >= 1.7.0; a non-blocking refresh
    # keeps setup working (its sensors just stay unavailable) if it is missing.
    usage = ClaudeUsageCoordinator(hass, entry, client, watch)
    await usage.async_refresh()

    # Account-wide limits need an add-on new enough to have the endpoint; an older
    # one answers 404 and its sensors simply never appear.
    limits = ClaudeAccountLimitsCoordinator(hass, entry, client, watch)
    await limits.async_refresh()

    entry.runtime_data = ClaudeRuntimeData(
        client=client, status=status, usage=usage, limits=limits
    )

    _async_setup_health(hass, entry, status)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


@callback
def _async_setup_health(
    hass: HomeAssistant, entry: ClaudeConfigEntry, status: ClaudeStatusCoordinator
) -> None:
    """Re-evaluate health repairs after each status poll (no Claude cost)."""
    mcp_unreachable_streak = 0

    @callback
    def _refresh_health() -> None:
        nonlocal mcp_unreachable_streak
        camera_vision = entry.options.get(CONF_CAMERA_VISION, False)
        report = evaluate_health(hass, status.data, camera_vision)
        report, mcp_unreachable_streak = debounce_mcp_unreachable(
            report, mcp_unreachable_streak
        )
        async_apply_health(hass, entry.entry_id, report)

    entry.async_on_unload(status.async_add_listener(_refresh_health))
    _refresh_health()


async def async_unload_entry(hass: HomeAssistant, entry: ClaudeConfigEntry) -> bool:
    """Unload a config entry and its platforms."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        async_clear_issues(hass, entry.entry_id, *HEALTH_ISSUES)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ClaudeConfigEntry) -> None:
    """Forget everything a removed entry left behind: its issues and its watch."""
    async_clear_issues(
        hass,
        entry.entry_id,
        *HEALTH_ISSUES,
        ISSUE_ADDON_NOT_INSTALLED,
        ISSUE_ADDON_NOT_RUNNING,
    )
    async_drop_addon_watch(hass, entry.entry_id)
    others = [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
    ]
    if not others:
        await async_remove_card_resource(hass)


async def _async_ensure_addon_running(
    hass: HomeAssistant, entry: ClaudeConfigEntry, slug: str
) -> None:
    """Make sure the companion add-on is installed and running before setup.

    Raises :class:`ConfigEntryNotReady` (so HA retries) while the add-on is
    missing or down: a missing one is installed and raises a repair at once, a
    stopped one is left to the Supervisor and reported by the add-on watch.
    Clears the issues once it is running.
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
        async_create_addon_issue(
            hass, entry.entry_id, ISSUE_ADDON_NOT_INSTALLED, slug, fixable=False
        )
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="addon_not_installed"
        )

    if info.state is not AddonState.RUNNING:
        # Not ours to start: at boot the Supervisor starts the add-on right
        # after Core, and one the user stopped stays stopped. The watch raises
        # the repair (which offers to start it) only if it stays down.
        get_addon_watch(hass, entry.entry_id, slug).async_down(info.state)
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="addon_not_running"
        )

    async_clear_addon_issues(hass, entry.entry_id)

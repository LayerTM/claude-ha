"""Supervisor add-on management for the Claude Code companion add-on.

The add-on slug is repository-prefixed and varies per install, so it is resolved
at runtime (from discovery, or by matching :data:`ADDON_SLUG_SUFFIX` against the
installed/store add-on lists) rather than hardcoded.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.hassio import (
    AddonError,
    AddonManager,
    AddonState,
    get_addons_info,
    get_supervisor_client,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import (
    ADDON_NAME,
    ADDON_OUTAGE_GRACE,
    ADDON_SLUG_SUFFIX,
    DOMAIN,
    ISSUE_ADDON_NOT_INSTALLED,
    ISSUE_ADDON_NOT_RUNNING,
    LOGGER,
)

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


def async_create_addon_issue(
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


def async_clear_addon_issues(hass: HomeAssistant) -> None:
    """Remove any add-on availability repair issues."""
    for issue_id in (ISSUE_ADDON_NOT_RUNNING, ISSUE_ADDON_NOT_INSTALLED):
        ir.async_delete_issue(hass, DOMAIN, issue_id)


class AddonWatch:
    """Tell an expected add-on restart apart from an outage.

    An update, a restart from the add-on page and a host reboot all leave the
    add-on's API refusing connections for a few seconds; that is a normal event,
    not an error. While the Supervisor still manages the add-on, a failed poll
    is therefore reported once at INFO and the entities just go unavailable. If
    the add-on has not answered for :data:`ADDON_OUTAGE_GRACE`, it is an outage:
    one WARNING, plus a repair offering to start it when it is not running.

    One watch is shared by every poller of an entry, so the INFO, the WARNING
    and the repair are emitted once per outage, not once per coordinator.
    """

    def __init__(self, hass: HomeAssistant, slug: str | None) -> None:
        """Watch the add-on ``slug``; ``None`` when no Supervisor manages it."""
        self._hass = hass
        self._slug = slug
        self._down_since: datetime | None = None
        self._outage_reported = False

    @property
    def in_grace(self) -> bool:
        """Whether a failure is inside the expected-restart window."""
        return self._down_since is not None and not self._outage_reported

    async def async_unreachable(self) -> bool:
        """Record a failed poll; return ``True`` if it is an expected restart.

        ``False`` means the Supervisor cannot vouch for the add-on (no
        Supervisor, its info is unreadable, or the add-on is gone), and the
        failure must be reported as an error by the caller as usual.
        """
        if self._slug is None:
            return False
        try:
            info = await get_addon_manager(
                self._hass, self._slug
            ).async_get_addon_info()
        except AddonError:
            return False
        if info.state is AddonState.NOT_INSTALLED:
            return False

        now = dt_util.utcnow()
        if self._down_since is None:
            self._down_since = now
            LOGGER.info(
                "The %s add-on is not answering (Supervisor state: %s); its "
                "entities are unavailable until it is back",
                ADDON_NAME,
                info.state.value,
            )
        elif not self._outage_reported and now - self._down_since >= ADDON_OUTAGE_GRACE:
            self._outage_reported = True
            self._report_outage(info.state)
        return True

    @callback
    def async_reachable(self) -> None:
        """Record a successful poll, closing any restart window or outage."""
        if self._down_since is None:
            return
        if self._outage_reported:
            async_clear_addon_issues(self._hass)
        self._down_since = None
        self._outage_reported = False

    def _report_outage(self, state: AddonState) -> None:
        """Surface an add-on that stayed unreachable past the grace period."""
        assert self._slug is not None
        minutes = int(ADDON_OUTAGE_GRACE.total_seconds() // 60)
        if state is AddonState.RUNNING:
            LOGGER.warning(
                "The %s add-on is running but its API has not answered for %d "
                "minutes; check the add-on log",
                ADDON_NAME,
                minutes,
            )
            return
        LOGGER.warning(
            "The %s add-on has not been running for %d minutes (Supervisor "
            "state: %s); Claude is unavailable until it is started",
            ADDON_NAME,
            minutes,
            state.value,
        )
        async_create_addon_issue(
            self._hass, ISSUE_ADDON_NOT_RUNNING, self._slug, fixable=True
        )

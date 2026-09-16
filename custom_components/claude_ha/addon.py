"""Supervisor add-on management for the Claude Code companion add-on.

The add-on slug is repository-prefixed and varies per install, so it is resolved
at runtime (from discovery, or by matching :data:`ADDON_SLUG_SUFFIX` against the
installed/store add-on lists) rather than hardcoded. More than one add-on can
match (e.g. a store build and a local build), so lookups return every match and
the caller decides; nothing here picks one silently.
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
from .issues import async_clear_issues, async_raise_issue

DATA_ADDON_MANAGERS = f"{DOMAIN}_addon_managers"
DATA_ADDON_WATCHES = f"{DOMAIN}_addon_watches"


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


async def async_find_addon_slugs(hass: HomeAssistant) -> list[str]:
    """Return every slug the Claude Code add-on could have, sorted.

    Installed add-ons win (the common case: the user installed the add-on,
    which then advertised itself via discovery); only when none is installed
    is the add-on store searched, so the config flow can offer to install one.
    """
    installed = _find_installed(hass)
    if installed:
        return installed
    return await _find_in_store(hass)


@callback
def _find_installed(hass: HomeAssistant) -> list[str]:
    """Match the slug suffix against installed add-ons (sync Supervisor cache)."""
    try:
        addons = get_addons_info(hass)
    except Exception:  # noqa: BLE001 - Supervisor data may not be loaded yet
        return []
    if not addons:
        return []
    return sorted(slug for slug in addons if slug.endswith(ADDON_SLUG_SUFFIX))


async def _find_in_store(hass: HomeAssistant) -> list[str]:
    """Match the slug suffix against store add-ons that are not yet installed."""
    client = get_supervisor_client(hass)
    try:
        store_addons = await client.store.addons_list()
    except Exception:  # noqa: BLE001 - store may be unavailable
        return []
    return sorted(
        addon.slug
        for addon in store_addons
        if not addon.installed and addon.slug.endswith(ADDON_SLUG_SUFFIX)
    )


@callback
def get_addon_watch(hass: HomeAssistant, entry_id: str, slug: str) -> AddonWatch:
    """Return the entry's add-on watch, kept across setup retries and reloads.

    An outage that starts while the entry is still retrying its setup must be
    timed from its first sighting, so the watch outlives any one setup attempt.
    """
    watches: dict[str, AddonWatch] = hass.data.setdefault(DATA_ADDON_WATCHES, {})
    if entry_id not in watches:
        watches[entry_id] = AddonWatch(hass, entry_id, slug)
    return watches[entry_id]


@callback
def async_drop_addon_watch(hass: HomeAssistant, entry_id: str) -> None:
    """Forget a removed entry's watch."""
    hass.data.get(DATA_ADDON_WATCHES, {}).pop(entry_id, None)


@callback
def async_create_addon_issue(
    hass: HomeAssistant, entry_id: str, issue: str, slug: str, *, fixable: bool
) -> None:
    """Raise one entry's repair issue for its missing/stopped add-on."""
    async_raise_issue(
        hass,
        entry_id,
        issue,
        severity=ir.IssueSeverity.ERROR,
        fixable=fixable,
        placeholders={"addon_slug": slug},
        data={"addon_slug": slug},
    )


@callback
def async_clear_addon_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Remove one entry's add-on availability repair issues."""
    async_clear_issues(
        hass, entry_id, ISSUE_ADDON_NOT_RUNNING, ISSUE_ADDON_NOT_INSTALLED
    )


class AddonWatch:
    """Tell an expected add-on restart apart from an outage.

    An update, a restart from the add-on page and a host reboot all leave the
    add-on's API refusing connections for a few seconds; that is a normal event,
    not an error. While the Supervisor still manages the add-on, a failed poll
    is therefore reported once at INFO and the entities just go unavailable. If
    the add-on has not answered for :data:`ADDON_OUTAGE_GRACE`, it is an outage:
    one WARNING, plus a repair offering to start it when it is not running.

    The setup of an entry reports a stopped add-on here too, rather than
    starting it: the Supervisor starts the add-on itself (at boot, right after
    Core), and one the user stopped stays stopped until the repair is used.

    One watch is shared by setup and every poller of an entry, so the INFO, the
    WARNING and the repair are emitted once per outage, not once per caller.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str, slug: str | None) -> None:
        """Watch entry ``entry_id``'s add-on ``slug``; ``None`` without Supervisor."""
        self._hass = hass
        self._entry_id = entry_id
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
        self.async_down(info.state)
        return True

    @callback
    def async_down(self, state: AddonState) -> None:
        """Record that the add-on is not answering, in Supervisor ``state``."""
        now = dt_util.utcnow()
        if self._down_since is None:
            self._down_since = now
            LOGGER.info(
                "The %s add-on is not answering (Supervisor state: %s); Claude "
                "is unavailable until it is back",
                ADDON_NAME,
                state.value,
            )
        elif not self._outage_reported and now - self._down_since >= ADDON_OUTAGE_GRACE:
            self._outage_reported = True
            self._report_outage(state)

    @callback
    def async_reachable(self) -> None:
        """Record a successful poll, closing any restart window or outage."""
        if self._down_since is None:
            return
        if self._outage_reported:
            async_clear_addon_issues(self._hass, self._entry_id)
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
            self._hass,
            self._entry_id,
            ISSUE_ADDON_NOT_RUNNING,
            self._slug,
            fixable=True,
        )

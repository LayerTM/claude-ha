"""Coordinators and runtime data for the Claude for Home Assistant integration."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, ClassVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .addon import AddonWatch
from .api import (
    AccountLimitsResult,
    ClaudeClient,
    ClaudeConnectionError,
    ClaudeEngineMismatchError,
    ClaudeError,
    ClaudeNotFoundError,
    ClaudeRateLimitError,
    StatusResult,
    UsageResult,
)
from .const import (
    ADDON_RESTART_RETRY,
    DOMAIN,
    ISSUE_ENGINE_MISMATCH,
    ISSUE_USAGE_HISTORY_RESET,
    LOGGER,
    SCAN_INTERVAL,
    USAGE_SCAN_INTERVAL,
)
from .issues import async_clear_issues, async_raise_issue, entry_issue_id

type ClaudeConfigEntry = ConfigEntry[ClaudeRuntimeData]


@dataclass
class ClaudeRuntimeData:
    """Objects shared across an entry's platforms."""

    client: ClaudeClient
    status: ClaudeStatusCoordinator
    usage: ClaudeUsageCoordinator
    limits: ClaudeAccountLimitsCoordinator


class _AddonCoordinator[DataT](DataUpdateCoordinator[DataT]):
    """Polls one add-on endpoint, staying quiet through an add-on restart."""

    config_entry: ClaudeConfigEntry

    # Failures that mean "this endpoint has nothing for us" rather than "something
    # broke": reported by going unavailable, without the coordinator's ERROR line.
    # Subclasses opt in; everything else stays loud.
    quiet_errors: ClassVar[tuple[type[ClaudeError], ...]] = ()

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ClaudeConfigEntry,
        client: ClaudeClient,
        watch: AddonWatch,
        *,
        name: str,
        interval: int,
        unavailable: str,
    ) -> None:
        """Init the coordinator with its API client and the entry's add-on watch."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{name}",
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        self._watch = watch
        self._unavailable = unavailable

    @abstractmethod
    async def _async_fetch(self) -> DataT:
        """Fetch this coordinator's endpoint."""

    async def _async_update_data(self) -> DataT:
        """Fetch the endpoint, telling an add-on restart apart from a failure."""
        try:
            data = await self._async_fetch()
        except ClaudeConnectionError as err:
            if not await self._watch.async_unreachable():
                raise UpdateFailed(str(err) or self._unavailable) from err
            # DataUpdateCoordinator logs "Error fetching …" at ERROR on the first
            # failed poll, and only while last_update_success is still True. The
            # watch has already said what is happening, so mark the failure here
            # and the restart window goes unavailable without an error.
            self.last_update_success = False
            raise UpdateFailed(
                str(err) or self._unavailable,
                retry_after=ADDON_RESTART_RETRY if self._watch.in_grace else None,
            ) from err
        except ClaudeError as err:
            if isinstance(err, self.quiet_errors):
                # The add-on answered, and its answer is "no data". Mark the
                # failure here for the same reason as above: the ERROR line is
                # logged only while last_update_success is still True.
                self.last_update_success = False
            raise UpdateFailed(str(err) or self._unavailable) from err
        self._watch.async_reachable()
        return data


class ClaudeStatusCoordinator(_AddonCoordinator[StatusResult]):
    """Polls the add-on's ``/api/status`` endpoint for the status sensor."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ClaudeConfigEntry,
        client: ClaudeClient,
        watch: AddonWatch,
    ) -> None:
        """Init the status coordinator."""
        super().__init__(
            hass,
            entry,
            client,
            watch,
            name="status",
            interval=SCAN_INTERVAL,
            unavailable="Add-on status unavailable",
        )

    async def _async_fetch(self) -> StatusResult:
        """Fetch the latest add-on status.

        An add-on that runs another engine than the entry's is not this entry's
        add-on any more: the poll fails and a repair says so, until it matches.
        """
        entry_id = self.config_entry.entry_id
        try:
            status = await self.client.async_get_status()
        except ClaudeEngineMismatchError as err:
            async_raise_issue(
                self.hass,
                entry_id,
                ISSUE_ENGINE_MISMATCH,
                severity=ir.IssueSeverity.ERROR,
                placeholders={"engine": err.reported},
            )
            raise
        async_clear_issues(self.hass, entry_id, ISSUE_ENGINE_MISMATCH)
        # Keep the prompt wall-clock just above the add-on's reported budget.
        self.client.note_prompt_timeout(status.prompt_timeout_ms)
        # Only send optional request fields the add-on accepts.
        self.client.note_status(status)
        return status


def _async_note_history_reset(
    hass: HomeAssistant, entry_id: str, report: dict[str, Any]
) -> None:
    """Raise ISSUE_USAGE_HISTORY_RESET once per distinct ``history_since``.

    ``history_reset`` never reverts to false once the add-on sets it, so a
    plain level check would re-raise on every poll. The issue registry's own
    stored ``history_since`` is the only "already told" state this needs, and
    the issue must be persistent for that memory to survive a real restart
    (a non-persistent one comes back with ``data=None``): unchanged means
    stay quiet, and a later, different reset recreates the issue so a user
    who dismissed the earlier one still sees the new one.
    """
    if not report.get("history_reset"):
        return
    history_since = report.get("history_since")
    if history_since is None:
        return
    issue_id = entry_issue_id(ISSUE_USAGE_HISTORY_RESET, entry_id)
    existing = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    if (
        existing is not None
        and existing.data is not None
        and existing.data.get("history_since") == history_since
    ):
        return
    async_clear_issues(hass, entry_id, ISSUE_USAGE_HISTORY_RESET)
    async_raise_issue(
        hass,
        entry_id,
        ISSUE_USAGE_HISTORY_RESET,
        severity=ir.IssueSeverity.WARNING,
        persistent=True,
        placeholders={"history_since": history_since},
        data={"history_since": history_since},
    )


class ClaudeUsageCoordinator(_AddonCoordinator[UsageResult]):
    """Polls the add-on's ``/api/usage`` endpoint (slow; cached by the add-on)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ClaudeConfigEntry,
        client: ClaudeClient,
        watch: AddonWatch,
    ) -> None:
        """Init the usage coordinator."""
        super().__init__(
            hass,
            entry,
            client,
            watch,
            name="usage",
            interval=USAGE_SCAN_INTERVAL,
            unavailable="Usage unavailable",
        )

    async def _async_fetch(self) -> UsageResult:
        """Fetch the latest usage report, flagging a lost history once."""
        result = await self.client.async_get_usage()
        _async_note_history_reset(self.hass, self.config_entry.entry_id, result.report)
        return result


class ClaudeAccountLimitsCoordinator(_AddonCoordinator[AccountLimitsResult]):
    """Polls ``/api/account_limits`` for the whole account's limit utilisation.

    Two answers mean "no figures to show" rather than a fault, and both leave the
    sensors unavailable with nothing in the log: an add-on too old to have the
    endpoint (404), and one that has OAuth credentials but cannot reach upstream
    (503). An API-key account is a third case and not a failure at all — it
    answers 200 with an empty list, and no limit entity is created for it.
    """

    quiet_errors: ClassVar[tuple[type[ClaudeError], ...]] = (
        ClaudeNotFoundError,
        ClaudeRateLimitError,
    )

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ClaudeConfigEntry,
        client: ClaudeClient,
        watch: AddonWatch,
    ) -> None:
        """Init the account-limits coordinator."""
        super().__init__(
            hass,
            entry,
            client,
            watch,
            name="account_limits",
            interval=USAGE_SCAN_INTERVAL,
            unavailable="Account limits unavailable",
        )

    async def _async_fetch(self) -> AccountLimitsResult:
        """Fetch the account's current limit utilisation."""
        return await self.client.async_get_account_limits()

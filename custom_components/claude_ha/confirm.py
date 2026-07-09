"""Two-phase write confirmation via actionable mobile notifications.

A read call may return a ``proposal``. When the ``claude_ha.ask`` action is given
a ``notify`` target, the proposal is sent as an actionable mobile notification
(Approve / Dismiss). On Approve, the confirmed ``intents`` are sent back to the
add-on in ``mode:"write"`` — the untrusted original prompt is never re-used to
drive the write (the add-on ignores it in write mode; see CONTRACT §2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import secrets
from typing import Any

from homeassistant.components import logbook
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .api import ClaudeError, Proposal
from .const import DOMAIN, EVENT_ACTION_EXECUTED, LOGGER, MODE_WRITE

EVENT_MOBILE_APP_ACTION = "mobile_app_notification_action"
APPROVE_PREFIX = "CLAUDE_HA_APPROVE_"
DISMISS_PREFIX = "CLAUDE_HA_DISMISS_"

# Pending proposals awaiting a notification Approve/Dismiss, keyed by pid.
DATA_PENDING = f"{DOMAIN}_pending_proposals"
DATA_CONFIRM_LISTENER = f"{DOMAIN}_confirm_listener"
# Pending chat confirmations, keyed by conversation_id (see conversation.py).
DATA_PENDING_CHAT = f"{DOMAIN}_pending_chat"

PENDING_TTL = timedelta(minutes=10)
CHAT_PENDING_TTL = timedelta(minutes=2)
LOGBOOK_NAME = "Claude"


@dataclass(slots=True)
class ConfirmationRequest:
    """The read-mode result the user is being asked to confirm."""

    prompt: str
    proposal: Proposal
    caller: str | None


@dataclass(slots=True)
class PendingProposal:
    """A proposal awaiting the user's Approve/Dismiss decision.

    Carries either entity-control ``intents`` (the notification/chat write path) or,
    when ``automation`` is set, a drafted automation config awaiting a confirmed
    in-process commit (the chat-only automation path).
    """

    entry_id: str
    prompt: str
    intents: list[dict[str, Any]]
    caller: str | None
    summary: str
    expires_at: datetime
    automation: dict[str, Any] | None = None


@callback
def async_setup_confirm(hass: HomeAssistant) -> None:
    """Register the mobile-app action listener once."""
    if DATA_CONFIRM_LISTENER in hass.data:
        return
    hass.data.setdefault(DATA_PENDING, {})

    async def _listener(event: Event) -> None:
        await _async_handle_action(hass, event)

    hass.data[DATA_CONFIRM_LISTENER] = hass.bus.async_listen(
        EVENT_MOBILE_APP_ACTION, _listener
    )


async def async_send_proposal_notification(
    hass: HomeAssistant,
    entry: ConfigEntry,
    notify_service: str,
    request: ConfirmationRequest,
) -> None:
    """Store a proposal and send an actionable Approve/Dismiss notification."""
    proposal = request.proposal
    pending: dict[str, PendingProposal] = hass.data.setdefault(DATA_PENDING, {})
    _prune(pending)

    pid = secrets.token_hex(6)
    pending[pid] = PendingProposal(
        entry_id=entry.entry_id,
        prompt=request.prompt,
        intents=proposal.intents,
        caller=request.caller,
        summary=proposal.summary,
        expires_at=dt_util.utcnow() + PENDING_TTL,
    )

    message = f"Claude suggests: {proposal.summary or 'a change'}."
    targets = sorted({t for item in proposal.intents for t in item.get("targets", [])})
    if targets:
        message += f" Affects: {', '.join(targets)}."

    await hass.services.async_call(
        "notify",
        notify_service,
        {
            "message": message,
            "data": {
                "tag": f"{DOMAIN}_{pid}",
                "actions": [
                    {"action": f"{APPROVE_PREFIX}{pid}", "title": "Approve"},
                    {"action": f"{DISMISS_PREFIX}{pid}", "title": "Dismiss"},
                ],
            },
        },
        blocking=True,
    )


@callback
def _prune(pending: dict[str, PendingProposal]) -> None:
    """Drop expired proposals."""
    now = dt_util.utcnow()
    for pid in [p for p, v in pending.items() if v.expires_at < now]:
        del pending[pid]


async def _async_handle_action(hass: HomeAssistant, event: Event) -> None:
    """Execute or discard a proposal in response to a notification action."""
    action = str(event.data.get("action", ""))
    if action.startswith(APPROVE_PREFIX):
        pid, approve = action[len(APPROVE_PREFIX) :], True
    elif action.startswith(DISMISS_PREFIX):
        pid, approve = action[len(DISMISS_PREFIX) :], False
    else:
        return

    pending: dict[str, PendingProposal] = hass.data.get(DATA_PENDING, {})
    proposal = pending.pop(pid, None)
    if proposal is None or proposal.expires_at < dt_util.utcnow():
        LOGGER.warning("Claude proposal %s is unknown or expired", pid)
        return

    if not approve:
        logbook.async_log_entry(
            hass, LOGBOOK_NAME, f"dismissed the proposal: {proposal.summary}", DOMAIN
        )
        return

    entry = hass.config_entries.async_get_entry(proposal.entry_id)
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        LOGGER.error("Cannot apply Claude proposal: config entry not loaded")
        return

    caller = event.context.user_id or proposal.caller
    try:
        await entry.runtime_data.client.async_prompt(
            proposal.prompt,
            mode=MODE_WRITE,
            intents=proposal.intents,
            caller=caller,
        )
    except ClaudeError as err:
        LOGGER.error("Claude write failed: %s", err)
        logbook.async_log_entry(
            hass, LOGBOOK_NAME, f"could not apply: {proposal.summary} ({err})", DOMAIN
        )
        return

    logbook.async_log_entry(
        hass, LOGBOOK_NAME, f"applied the proposal: {proposal.summary}", DOMAIN
    )
    hass.bus.async_fire(
        EVENT_ACTION_EXECUTED,
        {"summary": proposal.summary, "intents": proposal.intents},
    )

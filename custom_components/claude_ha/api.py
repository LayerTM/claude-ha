"""HTTP client for the Claude Code add-on's bearer-authenticated prompt server.

This is the CLIENT half of the contract in ``.research/CONTRACT.md``. The add-on
(repo ``LayerTM/ClaudeInHA``) implements the matching server on an internal-only
port. The two repos are developed independently and connect ONLY through that
contract, so keep request/response shapes here in lockstep with it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
import json
import math
from typing import Any, Final, NoReturn

from aiohttp import ClientError, ClientSession
from awesomeversion import AwesomeVersion, AwesomeVersionException

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import (
    ADDON_MIN_EDIT_VERSION,
    ADDON_MIN_SURFACE_VERSION,
    API_PROMPT,
    API_STATUS,
    API_USAGE,
    CHAT_HEALTH_OUTAGE_RUN,
    CHAT_HEALTH_STALE_FAILURE_S,
    CONTENT_TYPE_NDJSON,
    DOMAIN,
    HEADER_CALLER,
    MODE_READ,
    MODE_WRITE,
    PROPOSAL_INTENTS,
    PROPOSAL_SUMMARY,
    REQUEST_EDIT_AUTOMATION,
    REQUEST_IMAGE_ENTITY,
    REQUEST_LANGUAGE,
    REQUEST_STREAM,
    REQUEST_SURFACE,
    REQUEST_TIMEOUT,
    RESP_AUTOMATION,
    RESP_PROPOSAL,
    RESP_TEXT,
    RESP_TOOLS_USED,
    RESP_TRUNCATED,
    STATUS_ALERTS,
    STATUS_BUDGET,
    STATUS_CHAT_HEALTH,
    STATUS_CLAUDE_VERSION,
    STATUS_HA_MCP,
    STATUS_HA_MCP_CONNECTED,
    STATUS_MODEL,
    STATUS_PROMPT_TIMEOUT_MS,
    STATUS_READY,
    STATUS_TIMEOUT,
    STATUS_VERSION,
    STREAM_ERROR,
    STREAM_KIND,
    STREAM_KIND_DELTA,
    STREAM_KIND_DONE,
    STREAM_KIND_ERROR,
    TIMEOUT_MARGIN,
)


class ClaudeError(HomeAssistantError):
    """Base error for the Claude add-on client.

    Subclasses carry a ``translation_key`` so they render through the
    integration's ``exceptions`` strings when surfaced to the user.
    """

    translation_key = "unknown"

    def __init__(self, message: str | None = None) -> None:
        """Init with a translated message key, keeping raw detail for the log."""
        super().__init__(
            message,
            translation_domain=DOMAIN,
            translation_key=self.translation_key,
        )


class ClaudeConnectionError(ClaudeError):
    """The add-on prompt server is unreachable, timed out, or is busy."""

    translation_key = "cannot_connect"


class ClaudeAuthError(ClaudeError):
    """The shared bearer token was rejected (401) or the source was blocked (403)."""

    translation_key = "auth_error"


class ClaudeRateLimitError(ClaudeError):
    """The add-on rate-limited or shed the request (429/503)."""

    translation_key = "rate_limited"


class ClaudeRequestError(ClaudeError):
    """The request was rejected as invalid before Claude ran (e.g. 413 too large)."""

    translation_key = "request_rejected"


@dataclass(slots=True)
class Proposal:
    """A state change Claude proposes but does not perform in read mode."""

    summary: str
    intents: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class PromptResult:
    """Parsed 200 response of ``POST /api/prompt``."""

    text: str
    proposal: Proposal | None
    tools_used: list[str]
    truncated: bool
    automation: dict[str, Any] | None = None


@dataclass(slots=True)
class StreamDelta:
    """An incremental chunk of answer text from a streaming read."""

    text: str


@dataclass(slots=True)
class ChatHealth:
    """Rolling chat-reliability summary from ``/api/status`` (add-on >= 1.20.0).

    The timestamps are epoch milliseconds and arrive with add-on 1.49.0; ``None``
    means UNKNOWN, either because the add-on is older or because the entry was
    written before it started stamping. ``consecutive_ok`` arrives in the same
    version but is counted by entry order rather than by clock, so it is a plain
    number even on unstamped history — ``None`` here only ever means "older add-on".

    The add-on trims this window by count (cap 50), never by age — deciding what
    counts as healthy is deliberately left to this side.

    ``degraded`` and ``recovered`` are disjoint subsets of ``recent`` (a recovered
    read is a success), so ``recent - degraded`` is the window's success count.
    """

    recent: int
    degraded: int
    recovered: int
    last_reason: str | None
    last_failure_ts: int | None = None
    window_from_ts: int | None = None
    window_to_ts: int | None = None
    consecutive_ok: int | None = None
    consecutive_failed: int | None = None

    @property
    def failure_rate(self) -> float:
        """Share of the window that failed even after a retry; 0.0 on an empty one."""
        return self.degraded / self.recent if self.recent > 0 else 0.0

    @property
    def has_recovered(self) -> bool:
        """Whether the window has a clean run that outlasts the trouble before it.

        This is the evidence-shaped answer to "is this still happening", and it
        rescues the case no rate can: a short window where one old failure keeps
        the rate high however many clean runs follow it. Two things must hold, and
        each names a different fact.

        **Every success came after the last failure.** Written as ``==`` rather
        than ``>=`` on purpose. ``consecutive_ok`` cannot exceed the success count
        while the add-on counts correctly, so the two agree on every valid input —
        but ``>=`` is the LENIENT direction, and it lets a count that is too high
        clear a window it never earned. Equality also disposes of a nonsensical
        ``degraded > recent``, where the success count goes negative and any
        positive count would sit above it.

        **The clean run is longer than the failures behind it.** Without this, an
        almost-entirely-failed window is cleared by the single success it happens
        to contain: 49 failures and one success satisfies "every success came
        after the last failure" for the trivial reason that there is only one. A
        rescue that overrules the sole clause able to raise a warning has to rest
        on more than one observation, and this is the floor that scales with the
        window instead of being picked. It also subsumes the zero case — an
        all-failed window has no successes and none since, and ``0 == 0`` alone
        would clear the loudest case there is.

        Strictly longer, not merely as long. Measured over every window up to 50,
        ``>`` and ``>=`` disagree on 26 inputs and all of them sit at a failure
        rate of exactly 0.5, so the choice only ever decides whether a window that
        is half failures may clear itself. The disputed case that looks worst — a
        first chat that fails and a second that succeeds — resolves on the user's
        very next successful chat, not on the six-hour timer, so the strict form
        costs one chat of patience and buys refusing to call a coin-flip
        recovered. It also leaves an empty window reporting False for free, where
        the lenient form needs a guard to avoid claiming recovery from no evidence
        at all.
        """
        if self.consecutive_ok is None:
            return False
        return (
            self.consecutive_ok == self.recent - self.degraded
            and self.consecutive_ok > self.degraded
        )

    @property
    def is_outage_run(self) -> bool:
        """Whether the newest runs are an unbroken run of failures, not a blip.

        The only signal here that can RAISE the state on its own. Everything else
        is a rate over a count-trimmed window, which by construction cannot see a
        fresh outage until it has diluted that window — five failures deep in a
        full one. A run of failures is blind to how often the install fails and
        sensitive to whether it is failing right now, which is the opposite blind
        spot, so the two together cover each other.

        ``None`` on an add-on older than 1.49.0, which raises nothing early — the
        same fail-closed direction as every other missing field, since absence
        neither clears a warning nor invents one.
        """
        return (
            self.consecutive_failed is not None
            and self.consecutive_failed >= CHAT_HEALTH_OUTAGE_RUN
        )

    def is_failure_stale(self, now: datetime) -> bool:
        """Whether the last recorded failure is too old to count against health.

        An unknown timestamp is NOT stale: an add-on older than 1.49.0 stamps
        nothing, and a missing stamp must never clear a warning on its own.
        """
        if self.last_failure_ts is None:
            return False
        return (
            now.timestamp() - self.last_failure_ts / 1000 > CHAT_HEALTH_STALE_FAILURE_S
        )


@dataclass(slots=True)
class Budget:
    """Daily spend cap from ``/api/status`` (add-on >= 1.21.0); limit 0 = unlimited."""

    limit: float
    spent: float


@dataclass(slots=True)
class AlertItem:
    """One active proactive-alert anomaly from ``/api/status`` (add-on >= 1.39.0).

    ``line`` is the user's own home-entity string (e.g. "Offline: UCG Fiber"); it is
    home data the user already sees in HA, NOT chat content.
    """

    key: str
    critical: bool
    line: str


@dataclass(slots=True)
class Alerts:
    """Active proactive-alert set from ``/api/status`` (add-on >= 1.39.0).

    ``None`` (rather than an instance) both when an older add-on omits the field and
    when the add-on reports ``null`` (proactive_alerts off or not yet ticked); either
    way the alerts binary sensor is unavailable.
    """

    active: int
    critical: int
    items: list[AlertItem]


@dataclass(slots=True)
class StatusResult:
    """Parsed 200 response of ``GET /api/status``."""

    ready: bool
    version: str | None
    claude_version: str | None
    model: str | None
    ha_mcp: bool | None
    ha_mcp_connected: bool | None
    chat_health: ChatHealth | None = None
    prompt_timeout_ms: int | None = None
    budget: Budget | None = None
    alerts: Alerts | None = None


@dataclass(slots=True)
class UsageResult:
    """Parsed 200 response of ``GET /api/usage`` (contract §3a)."""

    today_tokens: int
    cost_today: float
    cost_total: float
    report: dict[str, Any]


class ClaudeClient:
    """Thin async client over the add-on's internal prompt server."""

    def __init__(self, session: ClientSession, base_url: str, token: str) -> None:
        """Store the shared session, add-on base URL and bearer token."""
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._read_timeout = float(REQUEST_TIMEOUT)
        self._addon_version: str | None = None

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    @property
    def read_timeout(self) -> float:
        """The current prompt-read wall-clock (tracks the add-on's budget)."""
        return self._read_timeout

    def _addon_at_least(self, min_version: str) -> bool:
        """Whether the last-observed add-on version is >= ``min_version``.

        Additive request fields are rejected by an add-on that predates them (its
        body-key allowlist 400s an unknown key), so a field is only put on the wire
        once a new-enough version is observed. Absent/unparseable version -> False.
        """
        if self._addon_version is None:
            return False
        try:
            return AwesomeVersion(self._addon_version) >= min_version
        except AwesomeVersionException:
            return False

    @property
    def _supports_surface(self) -> bool:
        """Whether the connected add-on accepts the ``surface`` field (>= 1.28.0)."""
        return self._addon_at_least(ADDON_MIN_SURFACE_VERSION)

    @property
    def supports_edit_automation(self) -> bool:
        """Whether the connected add-on accepts ``edit_automation`` (>= 1.36.0)."""
        return self._addon_at_least(ADDON_MIN_EDIT_VERSION)

    def note_version(self, version: str | None) -> None:
        """Record the add-on version last reported by ``/api/status``.

        Gates additive request fields (e.g. ``surface``) that older add-ons would
        reject, so a field is only put on the wire once the add-on supports it.
        """
        self._addon_version = version

    def note_prompt_timeout(self, prompt_timeout_ms: int | None) -> None:
        """Track the add-on's prompt budget so our wall-clock stays just above it.

        Keeps the read timeout at ``max(REQUEST_TIMEOUT, budget + margin)`` so the
        add-on's graceful timeout answer always lands before the client gives up,
        even if the user raises the add-on's prompt timeout. Falls back to the floor
        when the add-on doesn't report a budget.
        """
        if prompt_timeout_ms is None:
            self._read_timeout = float(REQUEST_TIMEOUT)
        else:
            self._read_timeout = max(
                float(REQUEST_TIMEOUT), prompt_timeout_ms / 1000 + TIMEOUT_MARGIN
            )

    async def async_get_status(self) -> StatusResult:
        """Fetch add-on readiness/versions (contract §3)."""
        data = await self._request("GET", API_STATUS, timeout_s=STATUS_TIMEOUT)
        ha_mcp = data.get(STATUS_HA_MCP)
        connected = data.get(STATUS_HA_MCP_CONNECTED)
        chat_health = _parse_chat_health(data.get(STATUS_CHAT_HEALTH))
        raw_timeout = data.get(STATUS_PROMPT_TIMEOUT_MS)
        prompt_timeout_ms = (
            int(raw_timeout) if isinstance(raw_timeout, (int, float)) else None
        )
        raw_budget = data.get(STATUS_BUDGET)
        budget = (
            Budget(
                limit=float(raw_budget.get("limit", 0.0)),
                spent=float(raw_budget.get("spent", 0.0)),
            )
            if isinstance(raw_budget, dict)
            else None
        )
        raw_alerts = data.get(STATUS_ALERTS)
        alerts = (
            Alerts(
                active=int(raw_alerts.get("active", 0)),
                critical=int(raw_alerts.get("critical", 0)),
                items=[
                    AlertItem(
                        key=str(item.get("key", "")),
                        critical=bool(item.get("critical", False)),
                        line=str(item.get("line", "")),
                    )
                    for item in raw_alerts.get("items", [])
                    if isinstance(item, dict)
                ],
            )
            if isinstance(raw_alerts, dict)
            else None
        )
        return StatusResult(
            ready=bool(data.get(STATUS_READY, False)),
            version=data.get(STATUS_VERSION),
            claude_version=data.get(STATUS_CLAUDE_VERSION),
            model=data.get(STATUS_MODEL),
            ha_mcp=None if ha_mcp is None else bool(ha_mcp),
            ha_mcp_connected=None if connected is None else bool(connected),
            chat_health=chat_health,
            prompt_timeout_ms=prompt_timeout_ms,
            budget=budget,
            alerts=alerts,
        )

    async def async_get_usage(self) -> UsageResult:
        """Fetch token/cost usage (contract §3a)."""
        data = await self._request("GET", API_USAGE, timeout_s=STATUS_TIMEOUT)
        today = data.get("tokens", {}).get("today", {})
        cost = data.get("prompt_api_cost_usd", {})
        return UsageResult(
            today_tokens=int(today.get("input", 0)) + int(today.get("output", 0)),
            cost_today=float(cost.get("today", 0.0)),
            cost_total=float(cost.get("total", 0.0)),
            report=data,
        )

    async def async_prompt(
        self,
        prompt: str,
        *,
        mode: str = MODE_READ,
        conversation_id: str | None = None,
        caller: str | None = None,
        intents: list[dict[str, Any]] | None = None,
        confirmation: str | None = None,
        image_entity: str | None = None,
        language: str | None = None,
        surface: str | None = None,
    ) -> PromptResult:
        """Send a prompt to Claude and return the structured result (contract §2).

        ``intents`` (the user-confirmed proposal intents) and ``confirmation``
        ("auto"/"confirmed") are sent only for ``mode="write"``, never for read.
        ``image_entity`` (an Assist-exposed camera) is a read-only visual hint.
        ``language`` (the HA conversation language) lets the add-on localize its
        server-authored messages; additive, ignored by older add-ons.
        ``surface`` ("voice"/"text") is only sent to add-ons that accept it
        (>= 1.28.0); older ones reject unknown keys, so it is dropped for them.
        """
        payload: dict[str, object] = {"prompt": prompt, "mode": mode}
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        if language is not None:
            payload[REQUEST_LANGUAGE] = language
        if surface is not None and self._supports_surface:
            payload[REQUEST_SURFACE] = surface
        if mode == MODE_WRITE:
            payload["intents"] = intents or []
            if confirmation is not None:
                payload["confirmation"] = confirmation
        elif image_entity is not None:
            payload[REQUEST_IMAGE_ENTITY] = image_entity
        headers = self._auth_headers
        if caller:
            headers[HEADER_CALLER] = caller

        data = await self._request(
            "POST",
            API_PROMPT,
            json=payload,
            headers=headers,
            timeout_s=self._read_timeout,
        )
        return _parse_prompt_result(data)

    async def async_prompt_stream(
        self,
        prompt: str,
        *,
        conversation_id: str | None = None,
        caller: str | None = None,
        image_entity: str | None = None,
        language: str | None = None,
        surface: str | None = None,
        edit_automation: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamDelta | PromptResult]:
        """Stream a read: yield text deltas, then one final ``PromptResult``.

        Requests the add-on's NDJSON stream (add-on >= 1.17.0). An add-on that
        can't stream answers a normal JSON body instead — detected by
        Content-Type — so a single ``PromptResult`` is yielded and no deltas.
        Streaming is read-only (contract §2). The last item is always the
        authoritative ``PromptResult`` (its proposal drives auto/confirm).
        ``edit_automation`` (the current config of an automation to modify) is only
        sent to add-ons that accept it (>= 1.36.0).
        """
        payload: dict[str, object] = {
            "prompt": prompt,
            "mode": MODE_READ,
            REQUEST_STREAM: True,
        }
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        if image_entity is not None:
            payload[REQUEST_IMAGE_ENTITY] = image_entity
        if language is not None:
            payload[REQUEST_LANGUAGE] = language
        if surface is not None and self._supports_surface:
            payload[REQUEST_SURFACE] = surface
        if edit_automation is not None and self.supports_edit_automation:
            payload[REQUEST_EDIT_AUTOMATION] = edit_automation
        headers = self._auth_headers
        if caller:
            headers[HEADER_CALLER] = caller

        url = f"{self._base_url}{API_PROMPT}"
        try:
            async with (
                asyncio.timeout(self._read_timeout),
                self._session.request(
                    "POST", url, json=payload, headers=headers
                ) as resp,
            ):
                if resp.status >= HTTPStatus.BAD_REQUEST:
                    _raise_for_status(resp.status)
                content_type = resp.headers.get("Content-Type", "")
                if CONTENT_TYPE_NDJSON not in content_type:
                    data = await resp.json(content_type=None) or {}
                    yield _parse_prompt_result(data)
                    return
                async for chunk in _iter_ndjson(resp.content):
                    yield chunk
        except TimeoutError as err:
            raise ClaudeConnectionError("Timed out talking to the add-on") from err
        except ClientError as err:
            raise ClaudeConnectionError(str(err)) from err

    async def _request(
        self,
        method: str,
        path: str,
        *,
        timeout_s: float,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Perform one request, mapping transport/HTTP failures to typed errors."""
        url = f"{self._base_url}{path}"
        try:
            async with (
                asyncio.timeout(timeout_s),
                self._session.request(
                    method,
                    url,
                    json=json,
                    headers=headers or self._auth_headers,
                ) as resp,
            ):
                if resp.status >= HTTPStatus.BAD_REQUEST:
                    _raise_for_status(resp.status)
                return await resp.json(content_type=None) or {}
        except TimeoutError as err:
            raise ClaudeConnectionError("Timed out talking to the add-on") from err
        except ClientError as err:
            raise ClaudeConnectionError(str(err)) from err


# Largest integer Home Assistant's JSON encoder will carry in a state attribute;
# the next value up raises `TypeError: Integer exceeds 64-bit range` in the
# recorder and on the websocket, after the state has already been set. Measured
# against `homeassistant.helpers.json.json_bytes`, not assumed.
_MAX_JSON_INT: Final = 2**64 - 1


def _parse_chat_health(raw: Any) -> ChatHealth | None:
    """Build a ``ChatHealth`` from the status block, or ``None`` if it says nothing.

    The three counts decide the sensor's state, so a block whose counts cannot be
    read is worth less than no block at all: ``None`` leaves the sensor
    unavailable, which is honest, where defaulting them to zero would read as "no
    failures" — the one answer that must never be invented. Malformed values are
    also how the poll used to die: ``int(None)`` and ``int("abc")`` raise
    ``TypeError``/``ValueError``, neither a ``ClaudeError``, so the coordinator
    could not catch them and every status entity went unavailable each minute.
    """
    if not isinstance(raw, dict):
        return None
    recent = _non_negative_int(raw.get("recent", 0))
    degraded = _non_negative_int(raw.get("degraded", 0))
    recovered = _non_negative_int(raw.get("recovered", 0))
    if recent is None or degraded is None or recovered is None:
        return None
    return ChatHealth(
        recent=recent,
        degraded=degraded,
        recovered=recovered,
        last_reason=raw.get("last_reason"),
        last_failure_ts=_epoch_ms(raw.get("last_failure_ts")),
        window_from_ts=_epoch_ms(raw.get("window_from_ts")),
        window_to_ts=_epoch_ms(raw.get("window_to_ts")),
        consecutive_ok=_non_negative_int(raw.get("consecutive_ok")),
        consecutive_failed=_non_negative_int(raw.get("consecutive_failed")),
    )


def _epoch_ms(raw: Any) -> int | None:
    """Coerce a contract timestamp to epoch ms, or ``None`` when it says nothing.

    The contract spells ``null`` as UNKNOWN — never "now", never 0 — so anything
    that isn't a positive whole millisecond reads as unknown. That direction is
    deliberate: an unknown timestamp leaves the failure counting against health,
    while a 0 taken at face value would date it to 1970 and silently clear the
    warning.

    The test is applied to the TRUNCATED value, not the raw one. Testing the raw
    value first let anything in ``(0, 1)`` pass ``> 0`` and then truncate to
    exactly the 0 being guarded against — the one direction this must never fail
    in.

    Validity is decided by performing the conversion the caller needs, rather than
    by predicting which values survive it. stdlib ``json`` — which aiohttp decodes
    with — accepts both ``Infinity`` and an arbitrarily long integer literal, and
    each of those crashes a different step: ``int(inf)`` raises ``OverflowError``,
    and so does ``math.isfinite`` on an int too large to become a float, which is
    how the guard that closed the first case reintroduced it. Neither is a
    ``ClaudeError``, so either would escape the coordinator and take every status
    entity unavailable once a minute.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        value = int(raw)
        if value <= 0:
            return None
        dt_util.utc_from_timestamp(value / 1000)
    except OverflowError, OSError, ValueError:
        return None
    return value


def _non_negative_int(raw: Any) -> int | None:
    """Coerce a contract count to an int, or ``None`` when it says nothing.

    Absent means an add-on older than 1.49.0. Anything else that isn't a
    non-negative number is read as unknown, which withholds the recovery rescue
    rather than granting it on a value nobody can explain.

    The sign is tested BEFORE truncation, unlike ``_epoch_ms``: ``int(-0.5)`` is
    ``0``, and 0 is a claim here — "the newest run failed" — not an absence.

    Validity is again decided by what the value has to survive downstream, and a
    count has two such steps rather than one. It is divided, in ``failure_rate``:
    stdlib ``json`` parses a 400-digit literal into an exact ``int``, which
    survives every type check here and then raises ``OverflowError`` there. And it
    is PUBLISHED, as a state attribute: Home Assistant's JSON encoder carries
    integers up to ``2**64 - 1`` and refuses the next one, which fails later still
    — in the recorder and on the websocket, after the state has already been set.

    Both bounds come from the consumers rather than from a number someone picked,
    which is the only reason either can be defended. Neither is reachable for a
    window the add-on caps at 50 runs; they are here because every earlier version
    of this guard predicted which values would survive a later step, and each
    prediction missed a family.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        if not math.isfinite(float(raw)) or raw < 0:
            return None
    except OverflowError:
        return None
    value = int(raw)
    return value if value <= _MAX_JSON_INT else None


def _parse_prompt_result(data: dict[str, Any]) -> PromptResult:
    """Build a ``PromptResult`` from a 200 body or a stream's ``done`` object."""
    proposal_raw = data.get(RESP_PROPOSAL)
    proposal: Proposal | None = None
    if isinstance(proposal_raw, dict):
        proposal = Proposal(
            summary=str(proposal_raw.get(PROPOSAL_SUMMARY, "")),
            intents=list(proposal_raw.get(PROPOSAL_INTENTS, []) or []),
        )
    automation_raw = data.get(RESP_AUTOMATION)
    automation = automation_raw if isinstance(automation_raw, dict) else None
    return PromptResult(
        text=str(data.get(RESP_TEXT, "")),
        proposal=proposal,
        tools_used=list(data.get(RESP_TOOLS_USED, []) or []),
        truncated=bool(data.get(RESP_TRUNCATED, False)),
        automation=automation,
    )


async def _iter_ndjson(
    stream: AsyncIterable[bytes],
) -> AsyncIterator[StreamDelta | PromptResult]:
    """Yield deltas then the final result from an NDJSON stream (contract §2)."""
    async for raw in stream:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as err:
            raise ClaudeConnectionError("Malformed stream from the add-on") from err
        kind = event.get(STREAM_KIND)
        if kind == STREAM_KIND_DELTA:
            yield StreamDelta(str(event.get(RESP_TEXT, "")))
        elif kind == STREAM_KIND_DONE:
            yield _parse_prompt_result(event)
            return
        elif kind == STREAM_KIND_ERROR:
            raise ClaudeConnectionError(str(event.get(STREAM_ERROR, "stream error")))
    raise ClaudeConnectionError("Stream ended without a final result")


def _raise_for_status(status: int) -> NoReturn:
    """Map an HTTP status code (contract §2) onto a typed error."""
    if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        raise ClaudeAuthError
    if status in (HTTPStatus.TOO_MANY_REQUESTS, HTTPStatus.SERVICE_UNAVAILABLE):
        raise ClaudeRateLimitError
    if status in (HTTPStatus.REQUEST_ENTITY_TOO_LARGE, HTTPStatus.BAD_REQUEST):
        raise ClaudeRequestError
    if status in (HTTPStatus.GATEWAY_TIMEOUT, HTTPStatus.BAD_GATEWAY):
        raise ClaudeConnectionError("The add-on timed out running Claude")
    raise ClaudeError

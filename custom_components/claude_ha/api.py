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
from http import HTTPStatus
import json
from typing import Any, NoReturn

from aiohttp import ClientError, ClientSession
from awesomeversion import AwesomeVersion, AwesomeVersionException

from homeassistant.exceptions import HomeAssistantError

from .const import (
    ADDON_MIN_SURFACE_VERSION,
    API_PROMPT,
    API_STATUS,
    API_USAGE,
    CONTENT_TYPE_NDJSON,
    DOMAIN,
    HEADER_CALLER,
    MODE_READ,
    MODE_WRITE,
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
    """Rolling chat-reliability summary from ``/api/status`` (add-on >= 1.20.0)."""

    recent: int
    degraded: int
    recovered: int
    last_reason: str | None


@dataclass(slots=True)
class Budget:
    """Daily spend cap from ``/api/status`` (add-on >= 1.21.0); limit 0 = unlimited."""

    limit: float
    spent: float


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

    @property
    def _supports_surface(self) -> bool:
        """Whether the connected add-on accepts the ``surface`` field.

        A pre-1.28.0 add-on rejects unknown request keys, so the caller must only
        send ``surface`` once we've observed a new-enough version via a status
        poll. Absent/unparseable version -> treat as unsupported (send nothing).
        """
        if self._addon_version is None:
            return False
        try:
            return AwesomeVersion(self._addon_version) >= ADDON_MIN_SURFACE_VERSION
        except AwesomeVersionException:
            return False

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
        raw_health = data.get(STATUS_CHAT_HEALTH)
        chat_health = (
            ChatHealth(
                recent=int(raw_health.get("recent", 0)),
                degraded=int(raw_health.get("degraded", 0)),
                recovered=int(raw_health.get("recovered", 0)),
                last_reason=raw_health.get("last_reason"),
            )
            if isinstance(raw_health, dict)
            else None
        )
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
    ) -> AsyncIterator[StreamDelta | PromptResult]:
        """Stream a read: yield text deltas, then one final ``PromptResult``.

        Requests the add-on's NDJSON stream (add-on >= 1.17.0). An add-on that
        can't stream answers a normal JSON body instead — detected by
        Content-Type — so a single ``PromptResult`` is yielded and no deltas.
        Streaming is read-only (contract §2). The last item is always the
        authoritative ``PromptResult`` (its proposal drives auto/confirm).
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


def _parse_prompt_result(data: dict[str, Any]) -> PromptResult:
    """Build a ``PromptResult`` from a 200 body or a stream's ``done`` object."""
    proposal_raw = data.get(RESP_PROPOSAL)
    proposal: Proposal | None = None
    if isinstance(proposal_raw, dict):
        proposal = Proposal(
            summary=str(proposal_raw.get("summary", "")),
            intents=list(proposal_raw.get("intents", []) or []),
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

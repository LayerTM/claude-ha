"""HTTP client for the Claude Code add-on's bearer-authenticated prompt server.

This is the CLIENT half of the contract in ``.research/CONTRACT.md``. The add-on
(repo ``LayerTM/ClaudeInHA``) implements the matching server on an internal-only
port. The two repos are developed independently and connect ONLY through that
contract, so keep request/response shapes here in lockstep with it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, NoReturn

from aiohttp import ClientError, ClientSession

from homeassistant.exceptions import HomeAssistantError

from .const import (
    API_PROMPT,
    API_STATUS,
    DOMAIN,
    HEADER_CALLER,
    MODE_READ,
    MODE_WRITE,
    REQUEST_TIMEOUT,
    RESP_PROPOSAL,
    RESP_TEXT,
    RESP_TOOLS_USED,
    RESP_TRUNCATED,
    STATUS_CLAUDE_VERSION,
    STATUS_MODEL,
    STATUS_READY,
    STATUS_TIMEOUT,
    STATUS_VERSION,
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


@dataclass(slots=True)
class StatusResult:
    """Parsed 200 response of ``GET /api/status``."""

    ready: bool
    version: str | None
    claude_version: str | None
    model: str | None


class ClaudeClient:
    """Thin async client over the add-on's internal prompt server."""

    def __init__(self, session: ClientSession, base_url: str, token: str) -> None:
        """Store the shared session, add-on base URL and bearer token."""
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._token = token

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def async_get_status(self) -> StatusResult:
        """Fetch add-on readiness/versions (contract §3)."""
        data = await self._request("GET", API_STATUS, timeout_s=STATUS_TIMEOUT)
        return StatusResult(
            ready=bool(data.get(STATUS_READY, False)),
            version=data.get(STATUS_VERSION),
            claude_version=data.get(STATUS_CLAUDE_VERSION),
            model=data.get(STATUS_MODEL),
        )

    async def async_prompt(
        self,
        prompt: str,
        *,
        mode: str = MODE_READ,
        conversation_id: str | None = None,
        caller: str | None = None,
        intents: list[dict[str, Any]] | None = None,
    ) -> PromptResult:
        """Send a prompt to Claude and return the structured result (contract §2).

        ``intents`` (the user-confirmed proposal intents) are sent only for
        ``mode="write"`` and never for read, per the contract.
        """
        payload: dict[str, object] = {"prompt": prompt, "mode": mode}
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        if mode == MODE_WRITE:
            payload["intents"] = intents or []
        headers = self._auth_headers
        if caller:
            headers[HEADER_CALLER] = caller

        data = await self._request(
            "POST",
            API_PROMPT,
            json=payload,
            headers=headers,
            timeout_s=REQUEST_TIMEOUT,
        )

        proposal_raw = data.get(RESP_PROPOSAL)
        proposal: Proposal | None = None
        if isinstance(proposal_raw, dict):
            proposal = Proposal(
                summary=str(proposal_raw.get("summary", "")),
                intents=list(proposal_raw.get("intents", []) or []),
            )
        return PromptResult(
            text=str(data.get(RESP_TEXT, "")),
            proposal=proposal,
            tools_used=list(data.get(RESP_TOOLS_USED, []) or []),
            truncated=bool(data.get(RESP_TRUNCATED, False)),
        )

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

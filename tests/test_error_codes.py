"""Tests for the add-on's error codes and its published prompt limit."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.api import (
    _ERROR_CODES,
    ClaudeAuthError,
    ClaudeClient,
    ClaudeConnectionError,
    ClaudeError,
    ClaudeNotFoundError,
    ClaudePromptTooLargeError,
    ClaudeRateLimitError,
    ClaudeRequestError,
    async_error_message,
)
from custom_components.claude_ha.engines import CLAUDE
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er, intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .conftest import (
    LEGACY_STATUS_PAYLOAD,
    STATUS_PAYLOAD,
    TEST_BASE_URL,
    TEST_TOKEN,
    USAGE_PAYLOAD,
    setup_integration,
)

_STATUS_URL = f"{TEST_BASE_URL}/api/status"
_PROMPT_URL = f"{TEST_BASE_URL}/api/prompt"

# The codes the add-on's prompt server answers with (core API 3), as its
# documentation lists them. A code added there needs a row here and in the table.
_CORE_CODES = {
    "unauthorized",
    "forbidden",
    "invalid_json",
    "invalid_body",
    "body_too_large",
    "unknown_field",
    "invalid_field",
    "mode_mismatch",
    "invalid_intents",
    "prompt_too_large",
    "confirmation_required",
    "rate_limited",
    "write_unavailable",
    "busy",
    "timeout",
    "internal",
    "usage_unavailable",
    "limits_unavailable",
    "not_found",
}


def _client(hass: HomeAssistant) -> ClaudeClient:
    return ClaudeClient(
        async_get_clientsession(hass), TEST_BASE_URL, TEST_TOKEN, engine=CLAUDE
    )


def test_every_core_code_is_mapped() -> None:
    """The table names exactly the codes the add-on can send."""
    assert set(_ERROR_CODES) == _CORE_CODES


@pytest.mark.parametrize(
    ("status", "body", "error", "message"),
    [
        (
            413,
            {
                "error": "prompt too large (max 8 KB)",
                "code": "prompt_too_large",
                "field": "prompt",
                "limit_bytes": 8192,
            },
            ClaudePromptTooLargeError,
            "The prompt is too large. The maximum size is 8192 bytes.",
        ),
        (
            413,
            {"error": "body too large", "code": "body_too_large", "limit_bytes": 65536},
            ClaudeRequestError,
            "The request is too large for the add-on. The maximum size is 65536 bytes.",
        ),
        (
            400,
            {
                "error": "unknown field: surface",
                "code": "unknown_field",
                "field": "surface",
            },
            ClaudeRequestError,
            "The add-on rejected the request field surface.",
        ),
        (
            400,
            {
                "error": 'stream is only valid with mode "read"',
                "code": "mode_mismatch",
                "field": "stream",
            },
            ClaudeRequestError,
            "The add-on rejected the request field stream.",
        ),
        (
            400,
            {"error": "intent data too large", "code": "invalid_intents"},
            ClaudeRequestError,
            "The add-on rejected the request as invalid.",
        ),
        (
            403,
            {
                "error": "sensitive action requires explicit confirmation",
                "code": "confirmation_required",
                "domains": ["lock"],
            },
            ClaudeRequestError,
            "This action needs your explicit confirmation before it runs.",
        ),
        (
            403,
            {"error": "forbidden", "code": "forbidden"},
            ClaudeAuthError,
            "The add-on rejected the request. The shared token may be out of date.",
        ),
        (
            503,
            {
                "error": "write mode unavailable: no HA MCP configured",
                "code": "write_unavailable",
            },
            ClaudeError,
            "The add-on can't make changes because it has no Home Assistant token. "
            "Set one in the add-on options.",
        ),
        (
            503,
            {"error": "busy", "code": "busy"},
            ClaudeRateLimitError,
            "The add-on is busy or rate-limited. Try again shortly.",
        ),
        (
            504,
            {"error": "timeout", "code": "timeout"},
            ClaudeConnectionError,
            "The add-on took too long to answer. Try again.",
        ),
        (
            500,
            {"error": "internal error", "code": "internal"},
            ClaudeError,
            "Unexpected error talking to the add-on.",
        ),
        (
            404,
            {"error": "not found", "code": "not_found"},
            ClaudeNotFoundError,
            "Unexpected error talking to the add-on.",
        ),
    ],
)
async def test_coded_answers_name_their_error(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    body: dict[str, Any],
    error: type[ClaudeError],
    message: str,
) -> None:
    """A coded answer raises its own error with its own message."""
    aioclient_mock.post(_PROMPT_URL, status=status, json=body)

    with pytest.raises(ClaudeError) as err:
        await _client(hass).async_prompt("hi")

    assert type(err.value) is error
    assert await async_error_message(hass, "en", err.value) == message


@pytest.mark.parametrize(
    ("status", "body", "error", "key"),
    [
        # An add-on that predates codes: the status decides, as before.
        (
            413,
            {"error": "prompt too large (max 8 KB)"},
            ClaudeRequestError,
            "request_too_large_unsized",
        ),
        (400, None, ClaudeRequestError, "request_rejected"),
        (
            403,
            {"error": "sensitive action requires explicit confirmation"},
            ClaudeAuthError,
            "auth_error",
        ),
        (503, {"error": "busy"}, ClaudeRateLimitError, "rate_limited"),
        # A code this version does not know.
        (
            400,
            {"error": "x", "code": "future_code"},
            ClaudeRequestError,
            "request_rejected",
        ),
        # A code without the value its message needs.
        (
            413,
            {"code": "prompt_too_large"},
            ClaudeRequestError,
            "request_too_large_unsized",
        ),
        (
            413,
            {"code": "body_too_large", "limit_bytes": 0},
            ClaudeRequestError,
            "request_too_large_unsized",
        ),
        (
            413,
            {"code": "body_too_large", "limit_bytes": "big"},
            ClaudeRequestError,
            "request_too_large_unsized",
        ),
        (400, {"code": "unknown_field"}, ClaudeRequestError, "request_rejected"),
        (
            400,
            {"code": "invalid_field", "field": ""},
            ClaudeRequestError,
            "request_rejected",
        ),
        (
            400,
            {"code": "invalid_field", "field": ["x"]},
            ClaudeRequestError,
            "request_rejected",
        ),
        # Not an object at all.
        (400, ["unknown_field"], ClaudeRequestError, "request_rejected"),
    ],
)
async def test_answers_without_a_usable_code_fall_back_to_the_status(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    body: Any,
    error: type[ClaudeError],
    key: str,
) -> None:
    """Without a known code and its value, the HTTP status decides."""
    if body is None:
        aioclient_mock.post(_PROMPT_URL, status=status, text="not json")
    else:
        aioclient_mock.post(_PROMPT_URL, status=status, json=body)

    with pytest.raises(ClaudeError) as err:
        await _client(hass).async_prompt("hi")

    assert type(err.value) is error
    assert err.value.translation_key == key


async def test_streamed_read_uses_the_code_too(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The streaming request reads the same answer the same way."""
    aioclient_mock.post(
        _PROMPT_URL,
        status=400,
        json={"error": "unknown field: x", "code": "unknown_field", "field": "x"},
    )

    with pytest.raises(ClaudeRequestError) as err:
        async for _ in _client(hass).async_prompt_stream("hi"):
            pass  # pragma: no cover - refused before the first item

    assert err.value.translation_placeholders == {"field": "x"}


@pytest.mark.parametrize(
    ("raw", "limit"),
    [(8192, 8192), (None, None), (0, None), (-1, None), ("8192", None), (True, None)],
)
async def test_status_prompt_limit_parsing(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    raw: Any,
    limit: int | None,
) -> None:
    """Only a positive whole number is a limit; anything else means unknown."""
    status = dict(STATUS_PAYLOAD)
    if raw is None:
        status.pop("prompt_max_bytes")
    else:
        status["prompt_max_bytes"] = raw
    aioclient_mock.get(_STATUS_URL, json=status)

    result = await _client(hass).async_get_status()

    assert result.prompt_max_bytes == limit


@pytest.mark.parametrize(
    ("prompt", "sent"),
    [("x" * 8192, True), ("x" * 8193, False), ("é" * 4097, False)],
)
async def test_prompt_is_checked_against_the_published_limit(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    prompt: str,
    sent: bool,
) -> None:
    """The limit counts UTF-8 bytes; a prompt at the limit is sent."""
    aioclient_mock.get(_STATUS_URL, json=STATUS_PAYLOAD)
    aioclient_mock.post(
        _PROMPT_URL,
        json={"text": "ok", "proposal": None, "tools_used": [], "truncated": False},
    )
    client = _client(hass)
    client.note_status(await client.async_get_status())

    if sent:
        await client.async_prompt(prompt)
    else:
        with pytest.raises(ClaudePromptTooLargeError) as err:
            await client.async_prompt(prompt)
        assert isinstance(err.value, ServiceValidationError)
        assert err.value.translation_placeholders == {"max_bytes": "8192"}
    posts = [call for call in aioclient_mock.mock_calls if call[0] == "POST"]
    assert len(posts) == (1 if sent else 0)


async def test_without_a_published_limit_the_addon_decides(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An add-on that publishes no limit gets the prompt and answers for it."""
    aioclient_mock.get(_STATUS_URL, json=LEGACY_STATUS_PAYLOAD)
    aioclient_mock.post(_PROMPT_URL, status=413, json={"error": "too large"})
    client = _client(hass)
    client.note_status(await client.async_get_status())

    with pytest.raises(ClaudeRequestError) as err:
        async for _ in client.async_prompt_stream("x" * 9000):
            pass  # pragma: no cover - refused before the first item

    assert err.value.translation_key == "request_too_large_unsized"
    assert await async_error_message(hass, "en", err.value) == (
        "The request is too large for the add-on."
    )
    assert [call[0] for call in aioclient_mock.mock_calls].count("POST") == 1


async def test_chat_names_the_limit_without_sending(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A too-long chat message is answered with the limit and never sent."""
    aioclient_mock.get(_STATUS_URL, json=STATUS_PAYLOAD)
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    aioclient_mock.get(f"{TEST_BASE_URL}/api/account_limits", status=404)
    await setup_integration(hass, mock_config_entry)
    agent = er.async_get(hass).async_get_entity_id(
        "conversation", "claude_ha", mock_config_entry.entry_id
    )

    result = await conversation.async_converse(
        hass, "x" * 9000, None, Context(), agent_id=agent
    )

    assert result.response.response_type is intent.IntentResponseType.ERROR
    assert result.response.speech["plain"]["speech"] == (
        "The prompt is too large. The maximum size is 8192 bytes."
    )
    assert not [call for call in aioclient_mock.mock_calls if call[0] == "POST"]


@pytest.mark.parametrize(
    ("event", "error", "key"),
    [
        (
            {"type": "error", "error": "timeout", "code": "timeout"},
            ClaudeConnectionError,
            "addon_timeout",
        ),
        (
            {"type": "error", "error": "internal error", "code": "internal"},
            ClaudeError,
            "unknown",
        ),
        (
            {"type": "error", "error": "stream broke"},
            ClaudeConnectionError,
            "cannot_connect",
        ),
    ],
)
async def test_stream_error_event_uses_its_code(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    event: dict[str, Any],
    error: type[ClaudeError],
    key: str,
) -> None:
    """A failure the add-on reports inside a stream is read by its code too."""
    aioclient_mock.post(
        _PROMPT_URL,
        text=json.dumps(event) + "\n",
        headers={"Content-Type": "application/x-ndjson"},
    )

    with pytest.raises(ClaudeError) as err:
        async for _ in _client(hass).async_prompt_stream("hi"):
            pass  # pragma: no cover - fails before the first item

    assert type(err.value) is error
    assert err.value.translation_key == key


@pytest.mark.parametrize(
    ("status", "key"), [(504, "addon_timeout"), (502, "cannot_connect")]
)
async def test_uncoded_gateway_answers(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int, key: str
) -> None:
    """An uncoded 504 is a timeout; a 502 means the add-on is not reachable."""
    aioclient_mock.post(_PROMPT_URL, status=status)

    with pytest.raises(ClaudeConnectionError) as err:
        await _client(hass).async_prompt("hi")

    assert err.value.translation_key == key

"""Tests for the add-on HTTP client (contract mapping)."""

from __future__ import annotations

from aiohttp import ClientError
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.api import (
    ClaudeAuthError,
    ClaudeClient,
    ClaudeConnectionError,
    ClaudeError,
    ClaudeRateLimitError,
    ClaudeRequestError,
)
from custom_components.claude_ha.const import HEADER_CALLER, MODE_WRITE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .conftest import STATUS_PAYLOAD, TEST_BASE_URL, TEST_TOKEN


def _client(hass: HomeAssistant) -> ClaudeClient:
    return ClaudeClient(async_get_clientsession(hass), TEST_BASE_URL, TEST_TOKEN)


async def test_status_parsing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A healthy status response parses into a StatusResult."""
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", json=STATUS_PAYLOAD)
    status = await _client(hass).async_get_status()
    assert status.ready is True
    assert status.model == "claude-sonnet-4-6"


async def test_prompt_sends_headers_and_body(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Prompt requests carry the bearer token, caller header, mode and id."""
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={
            "text": "hi",
            "proposal": {"summary": "s", "intents": [{"targets": ["light.x"]}]},
            "tools_used": ["t"],
            "truncated": True,
        },
    )
    intents = [{"intent": "HassTurnOff", "targets": ["light.x"], "data": {}}]
    result = await _client(hass).async_prompt(
        "hello",
        mode=MODE_WRITE,
        conversation_id="conv-1",
        caller="user-1",
        intents=intents,
    )
    assert result.text == "hi"
    assert result.proposal is not None
    assert result.proposal.summary == "s"
    assert result.truncated is True

    _method, _url, body, headers = aioclient_mock.mock_calls[-1]
    assert body == {
        "prompt": "hello",
        "mode": MODE_WRITE,
        "conversation_id": "conv-1",
        "intents": intents,
    }
    assert headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    assert headers[HEADER_CALLER] == "user-1"


async def test_prompt_read_mode_omits_intents(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Read requests never carry an intents field (contract §2)."""
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={"text": "ok", "proposal": None, "tools_used": [], "truncated": False},
    )
    await _client(hass).async_prompt("hello")
    body = aioclient_mock.mock_calls[-1][2]
    assert "intents" not in body
    assert body["mode"] == "read"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ClaudeAuthError),
        (403, ClaudeAuthError),
        (413, ClaudeRequestError),
        (400, ClaudeRequestError),
        (429, ClaudeRateLimitError),
        (503, ClaudeRateLimitError),
        (504, ClaudeConnectionError),
        (502, ClaudeConnectionError),
        (500, ClaudeError),
    ],
)
async def test_status_code_mapping(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    expected: type[ClaudeError],
) -> None:
    """Each documented HTTP status maps to the right typed error."""
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", status=status)
    with pytest.raises(expected):
        await _client(hass).async_get_status()


async def test_transport_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A transport failure maps to a connection error."""
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", exc=ClientError())
    with pytest.raises(ClaudeConnectionError):
        await _client(hass).async_get_status()


async def test_timeout(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A timeout maps to a connection error."""
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", exc=TimeoutError())
    with pytest.raises(ClaudeConnectionError):
        await _client(hass).async_get_status()

"""Tests for NDJSON streaming reads (Design 7)."""

from __future__ import annotations

from collections.abc import AsyncIterator
import json
from typing import Any

from aiohttp import ClientError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.api import (
    ClaudeClient,
    ClaudeError,
    ClaudeRateLimitError,
    PromptResult,
    StreamDelta,
)
from custom_components.claude_ha.const import DOMAIN
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er, intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .conftest import TEST_BASE_URL, TEST_TOKEN, setup_integration

_URL = f"{TEST_BASE_URL}/api/prompt"
_NDJSON = {"Content-Type": "application/x-ndjson"}


def _ndjson(*objs: dict[str, Any]) -> str:
    return "".join(json.dumps(o) + "\n" for o in objs)


def _client(hass: HomeAssistant) -> ClaudeClient:
    return ClaudeClient(async_get_clientsession(hass), TEST_BASE_URL, TEST_TOKEN)


async def _collect(
    it: AsyncIterator[StreamDelta | PromptResult],
) -> list[StreamDelta | PromptResult]:
    return [chunk async for chunk in it]


async def test_stream_yields_deltas_then_result(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Delta lines are yielded in order, then the final result."""
    aioclient_mock.post(
        _URL,
        text=_ndjson(
            {"type": "delta", "text": "The living "},
            {"type": "delta", "text": "room is 21 °C."},
            {
                "type": "done",
                "text": "The living room is 21 °C.",
                "proposal": None,
                "tools_used": ["mcp__ha__GetLiveContext"],
                "truncated": False,
            },
        ),
        headers=_NDJSON,
    )
    chunks = await _collect(_client(hass).async_prompt_stream("temp?"))

    deltas = [c.text for c in chunks if isinstance(c, StreamDelta)]
    assert deltas == ["The living ", "room is 21 °C."]
    result = chunks[-1]
    assert isinstance(result, PromptResult)
    assert result.text == "The living room is 21 °C."
    assert result.tools_used == ["mcp__ha__GetLiveContext"]
    # The request opted into streaming.
    body = aioclient_mock.mock_calls[0][2]
    assert body["stream"] is True
    assert body["mode"] == "read"


async def test_stream_done_carries_proposal(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The done object's proposal is parsed into the final result."""
    aioclient_mock.post(
        _URL,
        text=_ndjson(
            {"type": "delta", "text": "Turning off the heater."},
            {
                "type": "done",
                "text": "Turning off the heater.",
                "proposal": {
                    "summary": "Turn off the heater",
                    "intents": [
                        {"intent": "HassTurnOff", "targets": ["switch.heater"]}
                    ],
                },
                "tools_used": [],
                "truncated": False,
            },
        ),
        headers=_NDJSON,
    )
    chunks = await _collect(_client(hass).async_prompt_stream("heater off"))
    result = chunks[-1]
    assert isinstance(result, PromptResult)
    assert result.proposal is not None
    assert result.proposal.intents[0]["targets"] == ["switch.heater"]


async def test_stream_json_fallback(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A non-NDJSON body (old add-on) yields a single result, no deltas."""
    aioclient_mock.post(
        _URL,
        json={"text": "hi", "proposal": None, "tools_used": [], "truncated": False},
    )
    chunks = await _collect(_client(hass).async_prompt_stream("hi"))
    assert len(chunks) == 1
    assert isinstance(chunks[0], PromptResult)
    assert chunks[0].text == "hi"


async def test_stream_error_line_raises(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A terminal error line surfaces as a ClaudeError."""
    aioclient_mock.post(
        _URL,
        text=_ndjson(
            {"type": "delta", "text": "partial"},
            {"type": "error", "error": "timeout"},
        ),
        headers=_NDJSON,
    )
    with pytest.raises(ClaudeError):
        await _collect(_client(hass).async_prompt_stream("q"))


async def test_stream_without_done_raises(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A stream that ends without a done object is an error."""
    aioclient_mock.post(
        _URL, text=_ndjson({"type": "delta", "text": "partial"}), headers=_NDJSON
    )
    with pytest.raises(ClaudeError):
        await _collect(_client(hass).async_prompt_stream("q"))


async def test_stream_malformed_line_raises(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A malformed NDJSON line is a clean error, not a crash."""
    aioclient_mock.post(_URL, text="{not json}\n", headers=_NDJSON)
    with pytest.raises(ClaudeError):
        await _collect(_client(hass).async_prompt_stream("q"))


async def test_stream_prestream_http_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A pre-stream HTTP status maps to the usual typed error."""
    aioclient_mock.post(_URL, status=429)
    with pytest.raises(ClaudeRateLimitError):
        await _collect(_client(hass).async_prompt_stream("q"))


async def test_stream_transport_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A transport failure maps to a connection error."""
    aioclient_mock.post(_URL, exc=ClientError("boom"))
    with pytest.raises(ClaudeError):
        await _collect(_client(hass).async_prompt_stream("q"))


async def test_stream_timeout(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A timeout maps to a connection error."""
    aioclient_mock.post(_URL, exc=TimeoutError())
    with pytest.raises(ClaudeError):
        await _collect(_client(hass).async_prompt_stream("q"))


async def test_stream_skips_blank_lines(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Blank NDJSON lines are ignored."""
    body = (
        "\n"
        + _ndjson({"type": "delta", "text": "hi"})
        + "\n"
        + _ndjson(
            {
                "type": "done",
                "text": "hi",
                "proposal": None,
                "tools_used": [],
                "truncated": False,
            }
        )
    )
    aioclient_mock.post(_URL, text=body, headers=_NDJSON)
    chunks = await _collect(_client(hass).async_prompt_stream("q"))
    assert [c.text for c in chunks if isinstance(c, StreamDelta)] == ["hi"]


def _agent(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "conversation", DOMAIN, entry.entry_id
    )
    assert entity_id is not None
    return entity_id


async def _say(
    hass: HomeAssistant, entry: MockConfigEntry, text: str
) -> conversation.ConversationResult:
    return await conversation.async_converse(
        hass, text, None, context=Context(user_id="u"), agent_id=_agent(hass, entry)
    )


def _speech(result: conversation.ConversationResult) -> str:
    return result.response.speech["plain"]["speech"]


async def test_conversation_streams_answer(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A streamed pure answer becomes the assistant turn."""
    aioclient_mock.post(
        _URL,
        text=_ndjson(
            {"type": "delta", "text": "The living "},
            {"type": "delta", "text": "room is 21 °C."},
            {
                "type": "done",
                "text": "ignored — deltas win",
                "proposal": None,
                "tools_used": [],
                "truncated": False,
            },
        ),
        headers=_NDJSON,
    )
    await setup_integration(hass, mock_config_entry)

    result = await _say(hass, mock_config_entry, "temperature?")
    assert _speech(result) == "The living room is 21 °C."


async def test_conversation_streamed_proposal_confirms(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A streamed answer with a sensitive proposal still asks to confirm."""
    aioclient_mock.post(
        _URL,
        text=_ndjson(
            {"type": "delta", "text": "I'll turn off the heater."},
            {
                "type": "done",
                "text": "I'll turn off the heater.",
                "proposal": {
                    "summary": "Turn off the heater",
                    "intents": [
                        {"intent": "HassTurnOff", "targets": ["switch.heater"]}
                    ],
                },
                "tools_used": [],
                "truncated": False,
            },
        ),
        headers=_NDJSON,
    )
    await setup_integration(hass, mock_config_entry)

    result = await _say(hass, mock_config_entry, "turn off the heater")
    assert "Confirm? (yes/no)" in _speech(result)
    assert "switch.heater" in _speech(result)


async def test_conversation_stream_no_result_errors(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream that produces no result yields an error response, not a crash."""
    await setup_integration(hass, mock_config_entry)

    async def _empty(*_a: Any, **_k: Any) -> AsyncIterator[PromptResult]:
        for _ in ():
            yield PromptResult("", None, [], False)

    monkeypatch.setattr(
        mock_config_entry.runtime_data.client, "async_prompt_stream", _empty
    )

    result = await _say(hass, mock_config_entry, "hi")
    assert result.response.response_type is intent.IntentResponseType.ERROR


async def test_stream_sends_edit_automation_when_supported(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """`edit_automation` rides the read only when the add-on is new enough."""
    aioclient_mock.post(
        _URL, text=_ndjson({"type": "done", "text": "ok"}), headers=_NDJSON
    )
    client = _client(hass)
    client.note_version("1.36.0")

    await _collect(
        client.async_prompt_stream("change it", edit_automation={"alias": "X"})
    )

    assert aioclient_mock.mock_calls[-1][2]["edit_automation"] == {"alias": "X"}


async def test_stream_omits_edit_automation_when_unsupported(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A pre-1.36.0 add-on never sees the edit_automation key (would 400)."""
    aioclient_mock.post(
        _URL, text=_ndjson({"type": "done", "text": "ok"}), headers=_NDJSON
    )
    client = _client(hass)
    client.note_version("1.35.0")

    await _collect(
        client.async_prompt_stream("change it", edit_automation={"alias": "X"})
    )

    assert "edit_automation" not in aioclient_mock.mock_calls[-1][2]

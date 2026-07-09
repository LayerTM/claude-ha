"""Tests for the Claude conversation agent."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.api import Proposal
from custom_components.claude_ha.const import DOMAIN, HEADER_CALLER
from custom_components.claude_ha.conversation import _render_proposal, _spoken_confirm
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er, intent

from .conftest import (
    STATUS_PAYLOAD,
    TEST_BASE_URL,
    USAGE_PAYLOAD,
    setup_integration,
)

# A voice satellite entity id — HA sets this on ConversationInput only for turns
# that arrive through an assist_satellite (audio in, TTS out).
VOICE_SATELLITE = "assist_satellite.kitchen"


def _agent_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the entity id of this integration's conversation agent."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "conversation", DOMAIN, entry.entry_id
    )
    assert entity_id is not None
    return entity_id


async def test_conversation_reply(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    mock_prompt: None,
) -> None:
    """A chat turn forwards to the add-on and returns Claude's answer."""
    await setup_integration(hass, mock_config_entry)

    result = await conversation.async_converse(
        hass,
        "How warm is the living room?",
        None,
        context=Context(),
        agent_id=_agent_id(hass, mock_config_entry),
    )

    assert result.response.response_type is intent.IntentResponseType.ACTION_DONE
    assert "living room is 21" in result.response.speech["plain"]["speech"]


async def test_conversation_forwards_language(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The turn's language is forwarded so the add-on can localize its messages."""
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={"text": "ok", "proposal": None, "tools_used": [], "truncated": False},
    )
    await setup_integration(hass, mock_config_entry)

    await conversation.async_converse(
        hass,
        "what's the weather?",
        None,
        context=Context(),
        language="uk",
        agent_id=_agent_id(hass, mock_config_entry),
    )

    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert posts[-1][2]["language"] == "uk"


async def test_conversation_surfaces_proposal(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A proposal without a low-risk hint is surfaced and confirmed, not run."""
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={
            "text": "You asked me to turn off the heater.",
            "proposal": {
                "summary": "Turn off the heater",
                "intents": [
                    {"intent": "HassTurnOff", "targets": ["switch.heater"], "data": {}}
                ],
            },
            "tools_used": [],
            "truncated": False,
        },
    )
    await setup_integration(hass, mock_config_entry)

    result = await conversation.async_converse(
        hass,
        "turn off the heater",
        None,
        context=Context(),
        agent_id=_agent_id(hass, mock_config_entry),
    )
    speech = result.response.speech["plain"]["speech"]
    assert "Turn off the heater" in speech
    assert "switch.heater" in speech
    assert "Confirm? (yes/no)" in speech


async def test_conversation_supported_languages(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """The agent advertises support for all languages."""
    from custom_components.claude_ha.conversation import ClaudeConversationEntity
    from homeassistant.const import MATCH_ALL

    await setup_integration(hass, mock_config_entry)
    entity = ClaudeConversationEntity(mock_config_entry.runtime_data.status)
    assert entity.supported_languages == MATCH_ALL


async def test_conversation_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An add-on error is returned as an error response, not an exception."""
    aioclient_mock.post(f"{TEST_BASE_URL}/api/prompt", status=503)
    await setup_integration(hass, mock_config_entry)

    result = await conversation.async_converse(
        hass,
        "hello",
        None,
        context=Context(),
        agent_id=_agent_id(hass, mock_config_entry),
    )
    assert result.response.response_type is intent.IntentResponseType.ERROR


async def test_conversation_propagates_id_and_caller(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    mock_prompt: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The entity forwards the caller and reuses the conversation id."""
    await setup_integration(hass, mock_config_entry)
    agent = _agent_id(hass, mock_config_entry)

    first = await conversation.async_converse(
        hass, "hi", None, context=Context(user_id="user-42"), agent_id=agent
    )
    body = aioclient_mock.mock_calls[-1][2]
    headers = aioclient_mock.mock_calls[-1][3]
    assert body["conversation_id"] == first.conversation_id
    assert headers[HEADER_CALLER] == "user-42"

    await conversation.async_converse(
        hass,
        "and again",
        first.conversation_id,
        context=Context(user_id="user-42"),
        agent_id=agent,
    )
    assert aioclient_mock.mock_calls[-1][2]["conversation_id"] == first.conversation_id


def test_render_proposal_no_proposal() -> None:
    """With no proposal, the reply is Claude's text verbatim."""
    assert _render_proposal("just an answer", None) == "just an answer"


@pytest.mark.parametrize(
    ("text", "proposal", "expect_present", "expect_absent"),
    [
        (
            "answer",
            Proposal(summary="", intents=[]),
            ["answer"],
            ["Proposed:", "Affects:"],
        ),
        (
            "answer",
            Proposal(summary="Do X", intents=[{"intent": "HassX"}]),
            ["answer", "Proposed: Do X"],
            ["Affects:"],
        ),
        (
            "",
            Proposal(summary="Do X", intents=[{"targets": ["light.x"]}]),
            ["Proposed: Do X", "Affects: light.x"],
            [],
        ),
    ],
)
def test_render_proposal_variants(
    text: str,
    proposal: Proposal,
    expect_present: list[str],
    expect_absent: list[str],
) -> None:
    """Proposal rendering omits empty parts (no execute/disclaimer text)."""
    reply = _render_proposal(text, proposal)
    for fragment in expect_present:
        assert fragment in reply
    for fragment in expect_absent:
        assert fragment not in reply


def _mock_status(aioclient_mock: AiohttpClientMocker, version: str) -> None:
    """Register /api/status (with a chosen add-on version) and /api/usage."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status", json={**STATUS_PAYLOAD, "version": version}
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)


@pytest.mark.parametrize(
    ("satellite_id", "expected"),
    [(VOICE_SATELLITE, "voice"), (None, "text")],
)
async def test_conversation_forwards_surface(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    satellite_id: str | None,
    expected: str,
) -> None:
    """A new-enough add-on gets surface=voice for satellite turns, text otherwise."""
    _mock_status(aioclient_mock, "1.28.0")
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={"text": "ok", "proposal": None, "tools_used": [], "truncated": False},
    )
    await setup_integration(hass, mock_config_entry)

    await conversation.async_converse(
        hass,
        "what's the weather?",
        None,
        context=Context(),
        agent_id=_agent_id(hass, mock_config_entry),
        satellite_id=satellite_id,
    )

    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert posts[-1][2]["surface"] == expected


async def test_conversation_omits_surface_for_old_addon(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An add-on below 1.28.0 rejects unknown keys, so surface is never sent."""
    _mock_status(aioclient_mock, "1.27.9")
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={"text": "ok", "proposal": None, "tools_used": [], "truncated": False},
    )
    await setup_integration(hass, mock_config_entry)

    await conversation.async_converse(
        hass,
        "what's the weather?",
        None,
        context=Context(),
        agent_id=_agent_id(hass, mock_config_entry),
        satellite_id=VOICE_SATELLITE,
    )

    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert "surface" not in posts[-1][2]


async def test_voice_confirmation_is_spoken_friendly(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A voice proposal is confirmed with a short spoken prompt, not markdown."""
    _mock_status(aioclient_mock, "1.28.0")
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={
            "text": "You asked me to turn off the heater.",
            "proposal": {
                "summary": "Turn off the heater",
                "intents": [
                    {"intent": "HassTurnOff", "targets": ["switch.heater"], "data": {}}
                ],
            },
            "tools_used": [],
            "truncated": False,
        },
    )
    await setup_integration(hass, mock_config_entry)

    result = await conversation.async_converse(
        hass,
        "turn off the heater",
        None,
        context=Context(),
        agent_id=_agent_id(hass, mock_config_entry),
        satellite_id=VOICE_SATELLITE,
    )
    speech = result.response.speech["plain"]["speech"]
    assert "Say yes to confirm" in speech
    # None of the aloud-noisy affordances leak into a spoken turn.
    assert "Confirm? (yes/no)" not in speech
    assert "switch.heater" not in speech
    assert "Affects:" not in speech


def test_spoken_confirm_keeps_unstreamed_answer() -> None:
    """When the read hasn't streamed, the answer is kept before the confirm line."""
    assert _spoken_confirm("It is 21 degrees.", "Turn on the fan") == (
        "It is 21 degrees. Turn on the fan. Say yes to confirm."
    )
    assert (
        _spoken_confirm("", "Turn on the fan") == "Turn on the fan. Say yes to confirm."
    )

"""The words of a write's verdict follow the conversation's language."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.api import MemoryChange
from custom_components.claude_ha.conversation import ClaudeConversationEntity
from homeassistant.components import conversation
from homeassistant.components.conversation.agent_manager import async_get_agent
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import chat_session

from .conftest import setup_integration
from .test_conversation import (
    _FAILED_B,
    _OK_A,
    _agent_id,
    _confirmed_write,
    _spoken,
)

_PACKAGE = Path(__file__).parents[1] / "custom_components" / "claude_ha"
_KEYS = ("write_done", "write_done_bare", "write_not_done", "write_went_through")
_PARTIAL = {
    "text": "ok",
    "actions": [_OK_A, _FAILED_B],
}

_DONE = {"en": "Done: Turn off the lamps", "de": "Erledigt: Turn off the lamps"}
_NOT_DONE = {
    "en": (
        "Not done: Turn off the lamps. Failed: Desk lamp (light.b is unavailable). "
        "Went through: light.a."
    ),
    "de": (
        "Nicht erledigt: Turn off the lamps. Fehlgeschlagen: Desk lamp "
        "(light.b is unavailable). Ausgeführt: light.a."
    ),
}


@pytest.mark.parametrize(
    ("language", "words"),
    [("en", "en"), ("de", "de"), ("de-CH", "de"), ("de-DE", "de"), ("uk", "en")],
)
async def test_the_verdict_is_worded_in_the_conversation_language(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
    language: str,
    words: str,
) -> None:
    """Regional codes use their base language; an unknown one gets English."""
    done = await _confirmed_write(
        hass,
        mock_config_entry,
        aioclient_mock,
        status=200,
        body={"text": "ok", "actions": [_OK_A]},
        language=language,
    )
    assert _spoken(done) == _DONE[words]


@pytest.mark.parametrize(
    ("language", "words"), [("en", "en"), ("de", "de"), ("de-CH", "de"), ("uk", "en")]
)
async def test_a_failure_is_worded_in_the_conversation_language(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
    language: str,
    words: str,
) -> None:
    """The words of a refusal are translated; the verdict itself is not."""
    result = await _confirmed_write(
        hass,
        mock_config_entry,
        aioclient_mock,
        status=502,
        body={"error": "x", "code": "action_failed", **_PARTIAL},
        language=language,
    )
    assert _spoken(result) == _NOT_DONE[words]


def test_every_language_translates_all_four_words() -> None:
    """A language that offers the verdict offers all of it, with its placeholders."""
    for path in sorted((_PACKAGE / "translations").glob("*.json")):
        exceptions = json.loads(path.read_text())["exceptions"]
        for key in _KEYS:
            assert key in exceptions, path.name
    for strings in ("strings.json", "translations/en.json"):
        exceptions = json.loads((_PACKAGE / strings).read_text())["exceptions"]
        assert "{summary}" in exceptions["write_done"]["message"]
        assert "{failed}" in exceptions["write_not_done"]["message"]
        assert "{went_through}" in exceptions["write_went_through"]["message"]


@pytest.mark.parametrize(
    ("language", "speech"),
    [("en", "Done."), ("de", "Erledigt."), ("de-CH", "Erledigt."), ("uk", "Done.")],
)
async def test_a_memory_change_with_nothing_to_say_is_just_done(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    language: str,
    speech: str,
) -> None:
    """The bare confirmation is worded in the language too."""
    await setup_integration(hass, mock_config_entry)
    agent_id = _agent_id(hass, mock_config_entry)
    agent = async_get_agent(hass, agent_id)
    assert isinstance(agent, ClaudeConversationEntity)
    user_input = conversation.ConversationInput(
        text="yes",
        context=Context(),
        conversation_id=None,
        device_id=None,
        satellite_id=None,
        language=language,
        agent_id=agent_id,
    )
    with (
        chat_session.async_get_chat_session(hass, None) as session,
        conversation.async_get_chat_log(hass, session, user_input) as chat_log,
    ):
        result = await agent._async_apply_memory(
            user_input, chat_log, MemoryChange((), ()), None
        )

    assert result.response.speech["plain"]["speech"] == speech

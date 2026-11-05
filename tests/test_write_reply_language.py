"""The words of a write's verdict follow the conversation's language."""

from __future__ import annotations

import json
from pathlib import Path
import string
from unittest.mock import patch

from aiohttp import ClientError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.claude_ha.api import MemoryChange, exception_text
from custom_components.claude_ha.conversation import ClaudeConversationEntity, _decision
from custom_components.claude_ha.replies import ENGLISH, Words, async_words
from homeassistant.components import conversation
from homeassistant.components.conversation.agent_manager import async_get_agent
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import chat_session

from .conftest import TEST_BASE_URL, setup_integration
from .test_conversation import (
    _FAILED_B,
    _OK_A,
    _WRITE_PROPOSAL,
    _agent_id,
    _confirmed_write,
    _spoken,
)

_PACKAGE = Path(__file__).parents[1] / "custom_components" / "claude_ha"
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


def _messages(path: Path) -> dict[str, str]:
    exceptions = json.loads(path.read_text())["exceptions"]
    return {key: exceptions[key]["message"] for key in ENGLISH if key in exceptions}


def _placeholders(text: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(text) if field}


def test_the_english_words_are_the_english_translation() -> None:
    """The fallback in code says what strings.json and en.json say."""
    for strings in ("strings.json", "translations/en.json"):
        assert _messages(_PACKAGE / strings) == ENGLISH, strings


def test_every_language_translates_every_word_with_its_placeholders() -> None:
    """A language that offers the reply words offers all of them, fully filled in."""
    for path in sorted((_PACKAGE / "translations").glob("*.json")):
        messages = _messages(path)
        assert messages.keys() == ENGLISH.keys(), path.name
        for key, text in messages.items():
            assert _placeholders(text) == _placeholders(ENGLISH[key]), (path.name, key)


async def _turns(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    language: str,
    answers: list[str],
) -> list[str]:
    """Propose a write, then send each answer as its own turn; return the speech."""

    async def _side_effect(
        method: str, url: str, data: dict
    ) -> AiohttpClientMockResponse:
        if data.get("mode") == "write":
            return AiohttpClientMockResponse(
                method, url, json={"text": "ok", "actions": [_OK_A]}
            )
        return AiohttpClientMockResponse(method, url, json=_WRITE_PROPOSAL)

    aioclient_mock.post(f"{TEST_BASE_URL}/api/prompt", side_effect=_side_effect)
    await setup_integration(hass, entry)
    agent_id = _agent_id(hass, entry)
    speech = []
    conversation_id = None
    for text in ["turn off the lamps", *answers]:
        result = await conversation.async_converse(
            hass,
            text,
            conversation_id,
            context=Context(),
            agent_id=agent_id,
            language=language,
        )
        conversation_id = result.conversation_id
        speech.append(_spoken(result))
    return speech


async def test_a_german_proposal_is_asked_and_confirmed_in_german(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The proposal, its question and the verdict are German; "ja" confirms."""
    asked, done = await _turns(hass, mock_config_entry, aioclient_mock, "de", ["ja"])
    assert asked == (
        "ok\n\nVorschlag: Turn off the lamps\nBetrifft: light.a, light.b.\n\n"
        "Bestätigen? (ja/nein)"
    )
    assert done == "Erledigt: Turn off the lamps"


async def test_a_german_nein_cancels(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A German "nein" cancels."""
    _, cancelled = await _turns(hass, mock_config_entry, aioclient_mock, "de", ["nein"])
    assert cancelled == "Abgebrochen."


@pytest.mark.parametrize("answer", ["yes", "ok", "OK"])
async def test_the_english_words_still_confirm_in_german(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
    answer: str,
) -> None:
    """The language's words are added to the English ones, never replacing them."""
    _, done = await _turns(hass, mock_config_entry, aioclient_mock, "de", [answer])
    assert done == "Erledigt: Turn off the lamps"


async def test_ja_does_not_confirm_in_english(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """English behaviour is unchanged: "ja" is not a yes there, only a new question."""
    _, answer = await _turns(hass, mock_config_entry, aioclient_mock, "en", ["ja"])
    assert not answer.startswith("Done")
    assert answer != "Cancelled."


def test_only_the_languages_own_words_widen_the_vocabulary() -> None:
    """A translation contributes exactly its listed words, lower-cased and trimmed."""
    german = Words({"component.claude_ha.exceptions.confirm_yes.message": "Ja, JAWOHL"})
    assert _decision("ja", german) is True
    assert _decision(" Jawohl ", german) is True
    assert _decision("yes", german) is True
    assert _decision("jein", german) is None
    assert _decision("ja", Words({})) is None
    assert _decision("nein", Words({})) is None


async def test_a_german_turn_with_an_error_that_has_no_german_text_is_english(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An existing error key missing from de.json gives the English text, no raw key."""
    assert (
        "cannot_connect"
        not in json.loads((_PACKAGE / "translations" / "de.json").read_text())[
            "exceptions"
        ]
    )
    aioclient_mock.post(f"{TEST_BASE_URL}/api/prompt", exc=ClientError("down"))
    await setup_integration(hass, mock_config_entry)

    result = await conversation.async_converse(
        hass,
        "hello",
        None,
        context=Context(),
        agent_id=_agent_id(hass, mock_config_entry),
        language="de",
    )

    assert _spoken(result) == "The app is not reachable."


async def test_unloadable_translations_still_give_the_english_reply(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A failing translation load never leaves a ``{placeholder}`` in a reply."""
    with patch(
        "custom_components.claude_ha.api.translation.async_get_translations",
        side_effect=OSError("disk gone"),
    ):
        done = await _confirmed_write(
            hass,
            mock_config_entry,
            aioclient_mock,
            status=200,
            body={"text": "ok", "actions": [_OK_A]},
            language="de",
        )
    assert _spoken(done) == "Done: Turn off the lamps"


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
            user_input,
            chat_log,
            MemoryChange((), ()),
            None,
            await async_words(hass, language),
        )

    assert result.response.speech["plain"]["speech"] == speech


def test_a_template_that_cannot_be_rendered_falls_back_to_the_default() -> None:
    """A broken translation gives the default; a broken default is returned as is."""
    key = "component.claude_ha.exceptions.write_done.message"
    assert exception_text({key: "{nope}"}, "write_done", "Done: {s}", s="x") == (
        "Done: x"
    )
    assert exception_text({}, "write_done", "{nope}", s="x") == "{nope}"


def test_every_reply_word_is_used_by_the_agent() -> None:
    """No translated word is dead, and a new one must be wired in to be listed."""
    source = (_PACKAGE / "conversation.py").read_text()
    assert [key for key in ENGLISH if f'"{key}"' not in source] == []

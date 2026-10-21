"""Tests that a user is shown an error's translation, never its raw text."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.api import (
    GENERIC_ERROR_MESSAGE,
    ClaudeError,
    async_error_message,
)
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import intent

from .conftest import TEST_BASE_URL, setup_integration
from .test_conversation import _DRAFT_BODY, _agent_id, _draft_then

_PACKAGE = Path(__file__).parents[1] / "custom_components" / "claude_ha"
_PROMPT_URL = f"{TEST_BASE_URL}/api/prompt"


async def _speech(
    hass: HomeAssistant, entry: MockConfigEntry, language: str = "en"
) -> conversation.ConversationResult:
    return await conversation.async_converse(
        hass,
        "hello",
        None,
        context=Context(),
        agent_id=_agent_id(hass, entry),
        language=language,
    )


@pytest.mark.parametrize(
    ("mock", "speech"),
    [
        ({"status": 503}, "The add-on is busy or rate-limited. Try again shortly."),
        (
            {"status": 401},
            "The add-on rejected the request. The shared token may be out of date.",
        ),
        ({"status": 413}, "The request is too large for the add-on."),
        ({"status": 400}, "The add-on rejected the request as invalid."),
        ({"status": 500}, "Unexpected error talking to the add-on."),
        (
            {"exc": ClientError("Cannot connect to host abcd:8126")},
            "The add-on is not reachable.",
        ),
    ],
)
async def test_client_errors_are_spoken_as_their_translation(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
    mock: dict,
    speech: str,
) -> None:
    """Chat shows the translated message, not the client's own text."""
    aioclient_mock.post(_PROMPT_URL, **mock)
    await setup_integration(hass, mock_config_entry)

    result = await _speech(hass, mock_config_entry)

    assert result.response.response_type is intent.IntentResponseType.ERROR
    assert result.response.speech["plain"]["speech"] == speech


async def test_a_language_without_translations_gets_english(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Home Assistant's English fallback applies; the raw text never appears."""
    aioclient_mock.post(_PROMPT_URL, exc=ClientError("Cannot connect to host abcd"))
    await setup_integration(hass, mock_config_entry)

    result = await _speech(hass, mock_config_entry, language="uk")

    assert result.response.speech["plain"]["speech"] == "The add-on is not reachable."


async def test_a_rejected_automation_says_why_with_its_details(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The real commit policy's refusal reaches chat as its translation."""
    body = {
        **_DRAFT_BODY,
        "automation": {
            **_DRAFT_BODY["automation"],
            "actions": [{"action": "homeassistant.restart"}],
        },
    }
    aioclient_mock.post(_PROMPT_URL, json=body)
    await setup_integration(hass, mock_config_entry)

    result = await _draft_then(hass, mock_config_entry, "yes")

    assert result.response.response_type is intent.IntentResponseType.ERROR
    assert result.response.speech["plain"]["speech"] == (
        "The drafted automation calls homeassistant.restart, which isn't allowed "
        "for an automation created from chat; not created."
    )


@pytest.mark.parametrize(
    ("err", "message"),
    [
        (
            ClaudeError(
                "raw",
                translation_key="automation_entity_not_allowed",
                translation_placeholders={"entity": "lock.front"},
            ),
            "The drafted automation targets lock.front, which isn't a specific "
            "entity in an allowed domain; not created.",
        ),
        # A key with no translation, or a placeholder missing, never shows a
        # raw key or template.
        (
            ClaudeError("raw", translation_key="no_such_key"),
            "Unexpected error talking to the add-on.",
        ),
        (
            ClaudeError("raw", translation_key="automation_entity_not_allowed"),
            "Unexpected error talking to the add-on.",
        ),
        (ClaudeError(), "Unexpected error talking to the add-on."),
    ],
)
async def test_error_message_rendering(
    hass: HomeAssistant, err: ClaudeError, message: str
) -> None:
    """What a user reads is the translation, filled, or the generic message."""
    assert await async_error_message(hass, "en", err) == message


def test_str_keeps_the_log_detail() -> None:
    """The raw detail stays available to logs and setup reasons."""
    err = ClaudeError("Cannot connect to host", translation_key="cannot_connect")
    assert str(err) == "Cannot connect to host"


def _error_keys() -> set[str]:
    """Every exception translation key the integration's code names literally."""
    keys: set[str] = set()
    for path in _PACKAGE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else getattr(func, "id", "")
                )
                if not name.endswith(("Error", "NotReady")):
                    continue
                keys.update(
                    kw.value.value
                    for kw in node.keywords
                    if kw.arg == "translation_key"
                    and isinstance(kw.value, ast.Constant)
                )
            elif isinstance(node, ast.ClassDef) and node.name.endswith("Error"):
                keys.update(
                    stmt.value.value
                    for stmt in node.body
                    if isinstance(stmt, ast.Assign)
                    and any(
                        getattr(t, "id", None) == "translation_key"
                        for t in stmt.targets
                    )
                    and isinstance(stmt.value, ast.Constant)
                )
    return keys


def test_every_error_key_has_a_translation() -> None:
    """A key with no text would fall back to the generic message silently."""
    keys = _error_keys()
    assert "automation_service_not_allowed" in keys  # the scan finds raise sites
    assert "cannot_connect" in keys  # ...and class defaults
    for strings in ("strings.json", "translations/en.json"):
        exceptions = json.loads((_PACKAGE / strings).read_text())["exceptions"]
        assert keys - exceptions.keys() == set(), strings


async def test_unloadable_translations_give_the_generic_text(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing translation load still answers, with a fixed text, and logs it."""
    aioclient_mock.post(_PROMPT_URL, status=503)
    await setup_integration(hass, mock_config_entry)

    with patch(
        "custom_components.claude_ha.api.translation.async_get_translations",
        side_effect=OSError("disk gone"),
    ):
        result = await _speech(hass, mock_config_entry)

    assert result.response.response_type is intent.IntentResponseType.ERROR
    assert result.response.speech["plain"]["speech"] == GENERIC_ERROR_MESSAGE
    assert "Could not load the error messages" in caplog.text


async def test_missing_generic_translation_gives_the_generic_text(
    hass: HomeAssistant,
) -> None:
    """Even without the generic key the fixed text is returned, not a KeyError."""
    with patch(
        "custom_components.claude_ha.api.translation.async_get_translations",
        return_value={},
    ):
        message = await async_error_message(hass, "en", ClaudeError("raw"))

    assert message == GENERIC_ERROR_MESSAGE


def test_generic_text_matches_its_translation() -> None:
    """The fixed fallback says what the ``unknown`` translation says."""
    for strings in ("strings.json", "translations/en.json"):
        exceptions = json.loads((_PACKAGE / strings).read_text())["exceptions"]
        assert exceptions["unknown"]["message"] == GENERIC_ERROR_MESSAGE, strings

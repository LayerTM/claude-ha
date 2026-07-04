"""Tests for hybrid auto/confirm chat actions."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from aiohttp import ClientError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.claude_ha.const import CONF_AUTO_EXECUTE, DOMAIN
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er, intent

from .conftest import TEST_BASE_URL, setup_integration

_URL = f"{TEST_BASE_URL}/api/prompt"


def _proposal(summary: str, intents: list[dict], text: str = "ok") -> dict[str, Any]:
    return {
        "text": text,
        "proposal": {"summary": summary, "intents": intents},
        "tools_used": [],
        "truncated": False,
    }


LOW_BENIGN = _proposal(
    "Turn on the kitchen light",
    [{"intent": "HassTurnOn", "targets": ["light.kitchen"], "risk": "low"}],
)
SENSITIVE = _proposal(
    "Turn off the heater",
    [{"intent": "HassTurnOff", "targets": ["switch.heater"]}],  # no risk hint
)
LOW_CRITICAL = _proposal(
    "Unlock the front door",
    [{"intent": "HassTurnOff", "targets": ["lock.front"], "risk": "low"}],
)


def _agent(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "conversation", DOMAIN, entry.entry_id
    )
    assert entity_id is not None
    return entity_id


def _posts(mock: AiohttpClientMocker) -> list:
    return [c for c in mock.mock_calls if c[0] == "POST"]


def _speech(result: conversation.ConversationResult) -> str:
    return result.response.speech["plain"]["speech"]


async def _say(
    hass: HomeAssistant, entry: MockConfigEntry, text: str, conv_id: str | None = None
) -> conversation.ConversationResult:
    return await conversation.async_converse(
        hass, text, conv_id, context=Context(user_id="u"), agent_id=_agent(hass, entry)
    )


async def test_auto_executes_benign(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A low-risk benign action runs immediately."""
    aioclient_mock.post(_URL, json=LOW_BENIGN)
    await setup_integration(hass, mock_config_entry)

    result = await _say(hass, mock_config_entry, "turn on the kitchen light")
    posts = _posts(aioclient_mock)
    assert len(posts) == 2  # read + auto write
    assert posts[-1][2]["mode"] == "write"
    assert posts[-1][2]["confirmation"] == "auto"
    assert "Done" in _speech(result)


async def test_sensitive_confirm_then_yes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A non-low-risk action is confirmed; yes runs the stored intents."""
    aioclient_mock.post(_URL, json=SENSITIVE)
    await setup_integration(hass, mock_config_entry)

    first = await _say(hass, mock_config_entry, "turn off the heater")
    assert len(_posts(aioclient_mock)) == 1  # read only
    assert "Confirm? (yes/no)" in _speech(first)

    second = await _say(hass, mock_config_entry, "yes", conv_id=first.conversation_id)
    posts = _posts(aioclient_mock)
    assert len(posts) == 2  # write now
    assert posts[-1][2]["confirmation"] == "confirmed"
    assert posts[-1][2]["intents"] == SENSITIVE["proposal"]["intents"]
    assert "Done" in _speech(second)


async def test_confirm_then_no_cancels(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """No cancels the pending action without a write."""
    aioclient_mock.post(_URL, json=SENSITIVE)
    await setup_integration(hass, mock_config_entry)

    first = await _say(hass, mock_config_entry, "turn off the heater")
    second = await _say(hass, mock_config_entry, "no", conv_id=first.conversation_id)
    assert "Cancelled" in _speech(second)
    assert len(_posts(aioclient_mock)) == 1  # no write


async def test_critical_target_requires_confirm(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A low-risk action on a critical target is still confirmed."""
    aioclient_mock.post(_URL, json=LOW_CRITICAL)
    await setup_integration(hass, mock_config_entry)

    result = await _say(hass, mock_config_entry, "unlock the front door")
    assert len(_posts(aioclient_mock)) == 1  # no auto write
    assert "Confirm? (yes/no)" in _speech(result)


async def test_auto_execute_disabled_always_confirms(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """With auto-execute off, even benign actions are confirmed."""
    aioclient_mock.post(_URL, json=LOW_BENIGN)
    await setup_integration(hass, mock_config_entry)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_AUTO_EXECUTE: False}
    )

    result = await _say(hass, mock_config_entry, "turn on the kitchen light")
    assert len(_posts(aioclient_mock)) == 1  # no auto write
    assert "Confirm? (yes/no)" in _speech(result)


async def test_confirm_unrecognised_reply_is_fresh(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A non yes/no reply drops the pending and is a fresh request."""
    aioclient_mock.post(_URL, json=SENSITIVE)
    await setup_integration(hass, mock_config_entry)

    first = await _say(hass, mock_config_entry, "turn off the heater")
    second = await _say(
        hass, mock_config_entry, "what's the weather?", conv_id=first.conversation_id
    )
    posts = _posts(aioclient_mock)
    assert all(p[2]["mode"] == "read" for p in posts)  # never wrote
    assert len(posts) == 2  # two reads
    assert "Confirm? (yes/no)" in _speech(second)


async def test_confirm_expired_is_fresh(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
    freezer,
) -> None:
    """A yes after the chat TTL does not execute the stale pending."""
    aioclient_mock.post(_URL, json=SENSITIVE)
    await setup_integration(hass, mock_config_entry)

    first = await _say(hass, mock_config_entry, "turn off the heater")
    freezer.tick(timedelta(minutes=3))
    second = await _say(hass, mock_config_entry, "yes", conv_id=first.conversation_id)
    posts = _posts(aioclient_mock)
    assert all(p[2]["mode"] == "read" for p in posts)  # no write
    assert "Confirm? (yes/no)" in _speech(second)


async def test_auto_403_falls_back_to_confirm(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """If the add-on refuses an auto write (403), confirm instead."""

    async def _side_effect(
        method: str, url: str, data: dict
    ) -> AiohttpClientMockResponse:
        if data.get("mode") == "write":
            return AiohttpClientMockResponse(
                method, url, status=403, json={"error": "auto refused"}
            )
        return AiohttpClientMockResponse(method, url, status=200, json=LOW_BENIGN)

    aioclient_mock.post(_URL, side_effect=_side_effect)
    await setup_integration(hass, mock_config_entry)

    result = await _say(hass, mock_config_entry, "turn on the kitchen light")
    posts = _posts(aioclient_mock)
    assert len(posts) == 2  # auto write attempted, refused
    assert "Confirm? (yes/no)" in _speech(result)


async def test_confirmed_write_failure_is_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A failing confirmed write returns an error response, not a crash."""

    async def _side_effect(method: str, url: str, data: dict):
        if data.get("mode") == "write":
            raise ClientError("boom")
        return AiohttpClientMockResponse(method, url, status=200, json=SENSITIVE)

    aioclient_mock.post(_URL, side_effect=_side_effect)
    await setup_integration(hass, mock_config_entry)

    first = await _say(hass, mock_config_entry, "turn off the heater")
    second = await _say(hass, mock_config_entry, "yes", conv_id=first.conversation_id)
    assert second.response.response_type is intent.IntentResponseType.ERROR

"""Tests for the claude_ha.ask service."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.const import (
    ATTR_CONFIG_ENTRY,
    ATTR_INTENTS,
    ATTR_MODE,
    ATTR_PROMPT,
    DOMAIN,
    MODE_WRITE,
    PROMPT_MAX_BYTES,
    SERVICE_ASK,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .conftest import TEST_BASE_URL, setup_integration


async def test_ask_returns_response(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    mock_prompt: None,
) -> None:
    """The ask service returns Claude's structured response."""
    await setup_integration(hass, mock_config_entry)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_ASK,
        {ATTR_PROMPT: "What's the temperature?"},
        blocking=True,
        return_response=True,
    )
    assert response["text"] == "The living room is 21 °C."
    assert response["proposal"] is None
    assert response["tools_used"] == ["mcp__ha__GetLiveContext"]
    assert response["truncated"] is False


_INTENTS = [{"intent": "HassTurnOff", "targets": ["switch.heater"], "data": {}}]


async def test_ask_write_mode(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Write mode forwards the confirmed intents to the add-on (contract §2)."""
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={"text": "Done.", "proposal": None, "tools_used": [], "truncated": False},
    )
    await setup_integration(hass, mock_config_entry)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_ASK,
        {
            ATTR_PROMPT: "turn off the heater",
            ATTR_MODE: MODE_WRITE,
            ATTR_INTENTS: _INTENTS,
        },
        blocking=True,
        return_response=True,
    )
    body = aioclient_mock.mock_calls[-1][2]
    assert body["mode"] == MODE_WRITE
    assert body["intents"] == _INTENTS


async def test_ask_write_requires_intents(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """Write mode without intents is rejected before any add-on call."""
    await setup_integration(hass, mock_config_entry)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ASK,
            {ATTR_PROMPT: "turn off the heater", ATTR_MODE: MODE_WRITE},
            blocking=True,
            return_response=True,
        )


async def test_ask_too_many_intents(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """More than the allowed number of intents is rejected."""
    await setup_integration(hass, mock_config_entry)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ASK,
            {ATTR_PROMPT: "x", ATTR_MODE: MODE_WRITE, ATTR_INTENTS: _INTENTS * 6},
            blocking=True,
            return_response=True,
        )


async def test_ask_intents_require_write(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """Intents sent with read mode are rejected."""
    await setup_integration(hass, mock_config_entry)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ASK,
            {ATTR_PROMPT: "x", ATTR_INTENTS: _INTENTS},
            blocking=True,
            return_response=True,
        )


async def test_ask_returns_proposal(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A read-mode proposal is serialized into the service response."""
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={
            "text": "I can do that.",
            "proposal": {"summary": "Turn off the heater", "intents": _INTENTS},
            "tools_used": [],
            "truncated": False,
        },
    )
    await setup_integration(hass, mock_config_entry)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_ASK,
        {ATTR_PROMPT: "turn off the heater"},
        blocking=True,
        return_response=True,
    )
    assert response["proposal"] == {
        "summary": "Turn off the heater",
        "intents": _INTENTS,
    }


async def test_ask_prompt_too_large(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """An oversized prompt is rejected before contacting the add-on."""
    await setup_integration(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ASK,
            {ATTR_PROMPT: "x" * (PROMPT_MAX_BYTES + 1)},
            blocking=True,
            return_response=True,
        )


async def test_ask_invalid_config_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """An unknown config entry id is rejected."""
    await setup_integration(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ASK,
            {ATTR_PROMPT: "hi", ATTR_CONFIG_ENTRY: "does-not-exist"},
            blocking=True,
            return_response=True,
        )


async def test_ask_no_entry_configured(hass: HomeAssistant) -> None:
    """With no entry set up, the service reports there is nothing to target."""
    from custom_components.claude_ha.services import async_setup_services

    async_setup_services(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ASK,
            {ATTR_PROMPT: "hi"},
            blocking=True,
            return_response=True,
        )


async def test_ask_entry_not_loaded(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """An explicitly targeted but unloaded entry is rejected."""
    await setup_integration(hass, mock_config_entry)
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ASK,
            {ATTR_PROMPT: "hi", ATTR_CONFIG_ENTRY: mock_config_entry.entry_id},
            blocking=True,
            return_response=True,
        )


async def test_ask_add_on_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An add-on failure surfaces as a HomeAssistantError."""
    aioclient_mock.post(f"{TEST_BASE_URL}/api/prompt", status=401)
    await setup_integration(hass, mock_config_entry)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ASK,
            {ATTR_PROMPT: "hi"},
            blocking=True,
            return_response=True,
        )

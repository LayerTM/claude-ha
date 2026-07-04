"""Tests for camera-vision resolution (Design 8)."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha import vision
from custom_components.claude_ha.api import ClaudeClient
from custom_components.claude_ha.const import CONF_CAMERA_VISION, DOMAIN
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .conftest import TEST_BASE_URL, TEST_TOKEN, setup_integration

_URL = f"{TEST_BASE_URL}/api/prompt"


@pytest.fixture
def expose_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat every camera as exposed to Assist."""
    monkeypatch.setattr(vision, "async_should_expose", lambda *_a: True)


def test_not_visual_returns_none(hass: HomeAssistant, expose_all: None) -> None:
    """A non-visual message never attaches a camera."""
    hass.states.async_set("camera.front", "idle")
    assert vision.resolve_camera(hass, "what's the temperature?") is None


def test_no_exposed_camera(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A visual message with no exposed camera attaches nothing."""
    monkeypatch.setattr(vision, "async_should_expose", lambda *_a: False)
    hass.states.async_set("camera.front", "idle")
    assert vision.resolve_camera(hass, "look at the camera") is None


def test_single_exposed_camera(hass: HomeAssistant, expose_all: None) -> None:
    """A visual message with exactly one exposed camera picks it."""
    hass.states.async_set("camera.front", "idle")
    assert vision.resolve_camera(hass, "who's at the door?") == "camera.front"


def test_name_match_among_many(hass: HomeAssistant, expose_all: None) -> None:
    """With several cameras, the one named in the message wins."""
    hass.states.async_set("camera.a", "idle", {"friendly_name": "Front Door"})
    hass.states.async_set("camera.b", "idle", {"friendly_name": "Garage"})
    assert vision.resolve_camera(hass, "show the front door camera") == "camera.a"


def test_ambiguous_returns_none(hass: HomeAssistant, expose_all: None) -> None:
    """With several cameras and no name match, never guess."""
    hass.states.async_set("camera.a", "idle", {"friendly_name": "Alpha"})
    hass.states.async_set("camera.b", "idle", {"friendly_name": "Beta"})
    assert vision.resolve_camera(hass, "look at the camera") is None


def test_area_and_floor_match(hass: HomeAssistant, expose_all: None) -> None:
    """A camera resolves by its area or floor name."""
    floor = fr.async_get(hass).async_create("Ground Floor")
    area = ar.async_get(hass).async_create("Backyard")
    ar.async_get(hass).async_update(area.id, floor_id=floor.floor_id)
    entry = er.async_get(hass).async_get_or_create("camera", "test", "back")
    er.async_get(hass).async_update_entity(entry.entity_id, area_id=area.id)
    hass.states.async_set(entry.entity_id, "idle")
    hass.states.async_set("camera.other", "idle")

    assert vision.resolve_camera(hass, "look at the ground floor") == entry.entity_id


async def test_device_area_fallback(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, expose_all: None
) -> None:
    """A camera with no direct area resolves via its device's area."""
    mock_config_entry.add_to_hass(hass)
    area = ar.async_get(hass).async_create("Hallway")
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, "cam-device")},
    )
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    entry = er.async_get(hass).async_get_or_create(
        "camera", "test", "hall", device_id=device.id
    )
    hass.states.async_set(entry.entity_id, "idle")
    hass.states.async_set("camera.other", "idle")

    assert vision.resolve_camera(hass, "look in the hallway") == entry.entity_id


async def test_async_prompt_sends_image_entity(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The client attaches image_entity on a read request."""
    aioclient_mock.post(
        _URL, json={"text": "x", "proposal": None, "tools_used": [], "truncated": False}
    )
    client = ClaudeClient(async_get_clientsession(hass), TEST_BASE_URL, TEST_TOKEN)
    await client.async_prompt("who's there?", image_entity="camera.front")
    assert aioclient_mock.mock_calls[0][2]["image_entity"] == "camera.front"


async def test_conversation_attaches_camera_when_enabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With vision enabled, a visual message sends the exposed camera."""
    monkeypatch.setattr(vision, "async_should_expose", lambda *_a: True)
    hass.states.async_set("camera.front", "idle")
    aioclient_mock.post(
        _URL,
        json={
            "text": "A person.",
            "proposal": None,
            "tools_used": [],
            "truncated": False,
        },
    )
    await setup_integration(hass, mock_config_entry)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_CAMERA_VISION: True}
    )

    entity_id = er.async_get(hass).async_get_entity_id(
        "conversation", DOMAIN, mock_config_entry.entry_id
    )
    assert entity_id is not None
    await conversation.async_converse(
        hass, "who's at the door?", None, Context(user_id="u"), agent_id=entity_id
    )

    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert posts[-1][2]["image_entity"] == "camera.front"


async def test_conversation_no_camera_when_disabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With vision off (default), no snapshot is ever sent."""
    monkeypatch.setattr(vision, "async_should_expose", lambda *_a: True)
    hass.states.async_set("camera.front", "idle")
    aioclient_mock.post(
        _URL,
        json={"text": "hi", "proposal": None, "tools_used": [], "truncated": False},
    )
    await setup_integration(hass, mock_config_entry)

    entity_id = er.async_get(hass).async_get_entity_id(
        "conversation", DOMAIN, mock_config_entry.entry_id
    )
    assert entity_id is not None
    await conversation.async_converse(
        hass, "who's at the door?", None, Context(user_id="u"), agent_id=entity_id
    )

    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert "image_entity" not in posts[-1][2]

"""Tests for one-click voice setup (Whisper + Piper + Assist pipeline)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.claude_ha import voice
from custom_components.claude_ha.const import DOMAIN
from custom_components.claude_ha.voice import (
    ASSIST_PIPELINE_DATA,
    PIPER_ADDON_SLUG,
    WHISPER_ADDON_SLUG,
    VoiceSetupResult,
)
from homeassistant.components.hassio import AddonError, AddonState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .conftest import make_addon_info, setup_integration


def _register_wyoming(hass: HomeAssistant, domain: str, obj: str) -> None:
    """Register a Wyoming engine entity as if discovery had created it."""
    er.async_get(hass).async_get_or_create(
        domain, "wyoming", obj, suggested_object_id=obj
    )


def _install_pipeline_store(hass: HomeAssistant, captured: dict[str, Any]) -> None:
    """Install a fake assist_pipeline store that records created pipelines."""

    class _Store:
        async def async_create_item(self, data: dict[str, Any]) -> SimpleNamespace:
            captured.update(data)
            return SimpleNamespace(id="pipeline-1")

    hass.data[ASSIST_PIPELINE_DATA] = SimpleNamespace(pipeline_store=_Store())


def test_default_voice() -> None:
    """Known languages map to a voice; unknown ones do not."""
    assert voice.default_voice("uk") == "uk_UA-ukrainian_tts-medium"
    assert voice.default_voice("zz") is None


async def test_ensure_addon_installs_configures_starts(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing add-on is installed, configured and started."""
    manager = MagicMock()
    manager.async_get_addon_info = AsyncMock(
        side_effect=[
            make_addon_info(AddonState.NOT_INSTALLED),
            make_addon_info(AddonState.NOT_RUNNING),
        ]
    )
    manager.async_install_addon = AsyncMock()
    manager.async_set_addon_options = AsyncMock()
    manager.async_start_addon = AsyncMock()
    monkeypatch.setattr(voice, "get_addon_manager", lambda _h, _s: manager)

    await voice._async_ensure_addon(hass, WHISPER_ADDON_SLUG, {"model": "small-int8"})

    manager.async_install_addon.assert_awaited_once()
    manager.async_set_addon_options.assert_awaited_once_with({"model": "small-int8"})
    manager.async_start_addon.assert_awaited_once()


async def test_ensure_addon_already_running(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installed, running add-on is only reconfigured, not reinstalled."""
    manager = MagicMock()
    manager.async_get_addon_info = AsyncMock(
        return_value=make_addon_info(AddonState.RUNNING)
    )
    manager.async_install_addon = AsyncMock()
    manager.async_set_addon_options = AsyncMock()
    manager.async_start_addon = AsyncMock()
    monkeypatch.setattr(voice, "get_addon_manager", lambda _h, _s: manager)

    await voice._async_ensure_addon(hass, PIPER_ADDON_SLUG, {"voice": "x"})

    manager.async_install_addon.assert_not_awaited()
    manager.async_start_addon.assert_not_awaited()


async def test_ensure_addon_wraps_error(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Supervisor error is surfaced as a translated HomeAssistantError."""
    manager = MagicMock()
    manager.async_get_addon_info = AsyncMock(side_effect=AddonError("boom"))
    monkeypatch.setattr(voice, "get_addon_manager", lambda _h, _s: manager)

    with pytest.raises(HomeAssistantError):
        await voice._async_ensure_addon(hass, WHISPER_ADDON_SLUG, {})


async def test_setup_pipeline_full(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Engines discovered → an Assist pipeline wired to Claude is created."""
    captured: dict[str, Any] = {}
    _install_pipeline_store(hass, captured)

    async def _fake_ensure(
        _hass: HomeAssistant, slug: str, _options: dict[str, Any]
    ) -> None:
        if slug == WHISPER_ADDON_SLUG:
            _register_wyoming(hass, "stt", "faster_whisper")
        else:
            _register_wyoming(hass, "tts", "piper")

    monkeypatch.setattr(voice, "_async_ensure_addon", _fake_ensure)

    result = await voice.async_setup_voice_pipeline(
        hass,
        "conversation.claude",
        language="uk",
        whisper_model="small-int8",
        piper_voice="uk_UA-x",
        pipeline_name="Claude (uk)",
    )

    assert result.pipeline_id == "pipeline-1"
    assert result.stt_engine == "stt.faster_whisper"
    assert result.tts_engine == "tts.piper"
    assert captured["conversation_engine"] == "conversation.claude"
    assert captured["tts_voice"] == "uk_UA-x"
    assert captured["language"] == "uk"


async def test_setup_pipeline_engine_timeout(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the engines never appear, no pipeline is created."""
    monkeypatch.setattr(voice, "ENGINE_DISCOVERY_TIMEOUT", 0.01)

    async def _noop(_hass: HomeAssistant, _slug: str, _options: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(voice, "_async_ensure_addon", _noop)

    result = await voice.async_setup_voice_pipeline(
        hass,
        "conversation.claude",
        language="pl",
        whisper_model="small-int8",
        piper_voice="pl_PL-x",
        pipeline_name="Claude (pl)",
    )

    assert result == VoiceSetupResult(None, None, None)


async def test_setup_pipeline_without_assist_pipeline(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Engines found but assist_pipeline absent → engines returned, no pipeline."""
    hass.data.pop(ASSIST_PIPELINE_DATA, None)

    async def _fake_ensure(
        _hass: HomeAssistant, slug: str, _options: dict[str, Any]
    ) -> None:
        domain = "stt" if slug == WHISPER_ADDON_SLUG else "tts"
        _register_wyoming(hass, domain, "engine")

    monkeypatch.setattr(voice, "_async_ensure_addon", _fake_ensure)

    result = await voice.async_setup_voice_pipeline(
        hass,
        "conversation.claude",
        language="en",
        whisper_model="small-int8",
        piper_voice="en_US-x",
        pipeline_name="Claude (en)",
    )

    assert result.stt_engine is not None
    assert result.pipeline_id is None


async def test_service_setup_voice_success(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service resolves the Claude agent and returns the setup result."""
    await setup_integration(hass, mock_config_entry)

    async def _fake_pipeline(
        _hass: HomeAssistant, conv_id: str, **_: Any
    ) -> VoiceSetupResult:
        assert conv_id.startswith("conversation.")
        return VoiceSetupResult("stt.whisper", "tts.piper", "pipeline-1")

    monkeypatch.setattr(
        "custom_components.claude_ha.services.async_setup_voice_pipeline",
        _fake_pipeline,
    )

    response = await hass.services.async_call(
        DOMAIN,
        "setup_voice",
        {"language": "uk"},
        blocking=True,
        return_response=True,
    )
    assert response["created_pipeline"] is True
    assert response["pipeline_id"] == "pipeline-1"
    assert response["tts_voice"] == "uk_UA-ukrainian_tts-medium"


async def test_service_setup_voice_unknown_language(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """An unknown language with no voice override is rejected."""
    await setup_integration(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "setup_voice",
            {"language": "zz"},
            blocking=True,
            return_response=True,
        )


async def test_service_setup_voice_no_conversation_entity(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the Claude conversation entity can't be resolved, the call is rejected."""
    await setup_integration(hass, mock_config_entry)
    monkeypatch.setattr(
        er.async_get(hass), "async_get_entity_id", lambda *_a, **_k: None
    )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "setup_voice",
            {"language": "uk"},
            blocking=True,
            return_response=True,
        )

"""One-click local voice: install Whisper + Piper and wire a Claude pipeline.

A polished, non-console version of the add-on's ``/ha-voice`` skill. For a chosen
language it installs and starts the official Whisper (STT) and Piper (TTS)
add-ons, waits for the Wyoming integration to discover them, and creates an Assist
pipeline whose conversation agent is this Claude agent.

The values that depend on the live Supervisor / Piper catalogue are isolated as
constants below so they are trivial to correct: the official add-on slugs, their
option keys, and a best-effort default Piper voice per language (override with the
service's ``tts_voice``). The Wyoming engine ids and the tts voice are otherwise
discovered from the running system rather than guessed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from homeassistant.components.hassio import AddonError, AddonState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .addon import get_addon_manager
from .const import LOGGER

# assist_pipeline stores its data under ``hass.data[HassKey("assist_pipeline")]``.
# We use the plain domain string rather than importing the module so this
# integration does not hard-depend on assist_pipeline (an optional
# after-dependency that pulls in tts/mutagen); it is only touched at runtime.
ASSIST_PIPELINE_DATA = "assist_pipeline"

# Official Supervisor add-on slugs and the integration that discovers them.
WHISPER_ADDON_SLUG = "core_whisper"
PIPER_ADDON_SLUG = "core_piper"
WYOMING_PLATFORM = "wyoming"

# A multilingual Whisper model that runs on CPU; overridable per call.
DEFAULT_WHISPER_MODEL = "small-int8"

# Best-effort default Piper voice per language — VERIFY against the live Piper
# catalogue, a wrong id stops Piper from starting. Override with ``tts_voice``.
LANGUAGE_VOICES: dict[str, str] = {
    "en": "en_US-lessac-medium",
    "de": "de_DE-thorsten-medium",
    "fr": "fr_FR-siwis-medium",
    "es": "es_ES-davefx-medium",
    "it": "it_IT-riccardo-x_low",
    "nl": "nl_NL-mls_5809-low",
    "pl": "pl_PL-gosia-medium",
    "pt": "pt_BR-faber-medium",
    "ru": "ru_RU-dmitri-medium",
    "uk": "uk_UA-ukrainian_tts-medium",
}

# How long to wait for Wyoming to discover a freshly-started add-on.
ENGINE_DISCOVERY_TIMEOUT = 90


@dataclass(slots=True)
class VoiceSetupResult:
    """What the voice setup achieved."""

    stt_engine: str | None
    tts_engine: str | None
    pipeline_id: str | None


@callback
def default_voice(language: str) -> str | None:
    """Return the best-effort default Piper voice for a language, if known."""
    return LANGUAGE_VOICES.get(language)


async def _async_ensure_addon(
    hass: HomeAssistant, slug: str, options: dict[str, object]
) -> None:
    """Install (if needed), configure and start one Supervisor add-on."""
    manager = get_addon_manager(hass, slug)
    try:
        info = await manager.async_get_addon_info()
        if info.state is AddonState.NOT_INSTALLED:
            await manager.async_install_addon()
        await manager.async_set_addon_options(options)
        info = await manager.async_get_addon_info()
        if info.state is not AddonState.RUNNING:
            await manager.async_start_addon()
    except AddonError as err:
        raise HomeAssistantError(
            translation_domain="claude_ha",
            translation_key="voice_addon_failed",
            translation_placeholders={"addon": slug, "error": str(err)},
        ) from err


@callback
def _wyoming_engine_ids(hass: HomeAssistant, domain: str) -> set[str]:
    """Return the entity ids of Wyoming engines in a domain (stt/tts)."""
    return {
        entry.entity_id
        for entry in er.async_get(hass).entities.values()
        if entry.domain == domain and entry.platform == WYOMING_PLATFORM
    }


async def _async_wait_for_new_engine(
    hass: HomeAssistant, domain: str, before: set[str]
) -> str | None:
    """Wait (bounded) for a newly-discovered Wyoming engine, or None on timeout.

    Driven by entity-registry updates (Wyoming registers the engine once the
    Supervisor discovery for the started add-on lands), so there is no polling.
    """
    appeared = asyncio.Event()

    @callback
    def _check(_event: object = None) -> None:
        if _wyoming_engine_ids(hass, domain) - before:
            appeared.set()

    cancel = hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _check)
    try:
        _check()  # it may already be present
        async with asyncio.timeout(ENGINE_DISCOVERY_TIMEOUT):
            await appeared.wait()
    except TimeoutError:
        LOGGER.warning("Timed out waiting for the Wyoming %s engine", domain)
        return None
    finally:
        cancel()
    new = _wyoming_engine_ids(hass, domain) - before
    return sorted(new)[0] if new else None


async def _async_create_pipeline(
    hass: HomeAssistant,
    *,
    name: str,
    language: str,
    conversation_entity_id: str,
    stt_engine: str,
    tts_engine: str,
    tts_voice: str,
) -> str | None:
    """Create an Assist pipeline wiring Claude + the discovered engines."""
    pipeline_data: Any = hass.data.get(ASSIST_PIPELINE_DATA)
    if pipeline_data is None:
        LOGGER.warning("assist_pipeline is not set up; skipping pipeline creation")
        return None
    pipeline = await pipeline_data.pipeline_store.async_create_item(
        {
            "name": name,
            "conversation_engine": conversation_entity_id,
            "conversation_language": language,
            "language": language,
            "stt_engine": stt_engine,
            "stt_language": language,
            "tts_engine": tts_engine,
            "tts_language": language,
            "tts_voice": tts_voice,
            "wake_word_entity": None,
            "wake_word_id": None,
        }
    )
    return str(pipeline.id)


async def async_setup_voice_pipeline(
    hass: HomeAssistant,
    conversation_entity_id: str,
    *,
    language: str,
    whisper_model: str,
    piper_voice: str,
    pipeline_name: str,
) -> VoiceSetupResult:
    """Install Whisper + Piper, discover their engines and build the pipeline."""
    before_stt = _wyoming_engine_ids(hass, "stt")
    before_tts = _wyoming_engine_ids(hass, "tts")

    await _async_ensure_addon(
        hass, WHISPER_ADDON_SLUG, {"model": whisper_model, "language": language}
    )
    await _async_ensure_addon(hass, PIPER_ADDON_SLUG, {"voice": piper_voice})

    stt_engine = await _async_wait_for_new_engine(hass, "stt", before_stt)
    tts_engine = await _async_wait_for_new_engine(hass, "tts", before_tts)

    pipeline_id: str | None = None
    if stt_engine is not None and tts_engine is not None:
        pipeline_id = await _async_create_pipeline(
            hass,
            name=pipeline_name,
            language=language,
            conversation_entity_id=conversation_entity_id,
            stt_engine=stt_engine,
            tts_engine=tts_engine,
            tts_voice=piper_voice,
        )
    return VoiceSetupResult(stt_engine, tts_engine, pipeline_id)

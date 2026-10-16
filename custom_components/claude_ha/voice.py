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
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import UnknownFlow
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
WHISPER_ADDON_NAME = "Whisper"
PIPER_ADDON_NAME = "Piper"
WYOMING_PLATFORM = "wyoming"

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


def _discovery_url(slug: str) -> str:
    """Return the ``configuration_url`` Wyoming's flow carries for an add-on."""
    return f"homeassistant://hassio/addon/{slug}/info"


# Wyoming's Supervisor-discovery flows for exactly the two add-ons set up here,
# mapped to the add-on each one belongs to.
_OWN_DISCOVERY_SLUGS = {
    _discovery_url(slug): slug for slug in (WHISPER_ADDON_SLUG, PIPER_ADDON_SLUG)
}


def _addon_host(slug: str) -> str:
    """Return the hostname Supervisor gives an add-on on the internal network."""
    return slug.replace("_", "-")


@callback
def default_voice(language: str) -> str | None:
    """Return the best-effort default Piper voice for a language, if known."""
    return LANGUAGE_VOICES.get(language)


async def _async_ensure_addon(
    hass: HomeAssistant, slug: str, name: str, options: dict[str, object]
) -> None:
    """Install (if needed), configure and start one Supervisor add-on."""
    manager = get_addon_manager(hass, slug, name)
    try:
        info = await manager.async_get_addon_info()
        if info.state is AddonState.NOT_INSTALLED:
            await manager.async_install_addon()
            info = await manager.async_get_addon_info()
        # Supervisor validates the whole option set, and add-ons grow required
        # options over time, so change only ours on top of what is stored.
        await manager.async_set_addon_options({**info.options, **options})
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
def _wyoming_hosts(hass: HomeAssistant) -> set[str]:
    """Return the hosts of every Wyoming config entry."""
    return {
        str(entry.data.get(CONF_HOST))
        for entry in hass.config_entries.async_entries(WYOMING_PLATFORM)
    }


async def _async_confirm_discovery(
    hass: HomeAssistant, flow_id: str, claimed: set[str]
) -> None:
    """Confirm one Wyoming discovery flow if it is for Whisper or Piper.

    The action is the user's explicit request to wire these two add-ons, so the
    prompt Wyoming would otherwise show for them is answered here. Any other
    Wyoming flow, or any other add-on's, is left to the user. An add-on that
    already has an entry, or whose discovery is already confirmed in this run
    (a stale one is left behind when an add-on is reinstalled), is never wired
    a second time.
    """
    for flow in hass.config_entries.flow.async_progress_by_handler(
        WYOMING_PLATFORM, match_context={"source": "hassio"}
    ):
        slug = _OWN_DISCOVERY_SLUGS.get(flow["context"].get("configuration_url", ""))
        if flow["flow_id"] != flow_id or slug is None:
            continue
        if slug in claimed or _addon_host(slug) in _wyoming_hosts(hass):
            return
        claimed.add(slug)
        try:
            await hass.config_entries.flow.async_configure(flow_id, {})
        except (UnknownFlow, HomeAssistantError) as err:
            claimed.discard(slug)
            LOGGER.warning("Could not confirm the Wyoming discovery: %s", err)
        return


@callback
def _wyoming_engine_ids(hass: HomeAssistant, domain: str, host: str) -> set[str]:
    """Return the entity ids of a Wyoming server's engines in a domain (stt/tts)."""
    entry_ids = {
        entry.entry_id
        for entry in hass.config_entries.async_entries(WYOMING_PLATFORM)
        if entry.data.get(CONF_HOST) == host
    }
    return {
        entry.entity_id
        for entry in er.async_get(hass).entities.values()
        if entry.domain == domain
        and entry.platform == WYOMING_PLATFORM
        and entry.config_entry_id in entry_ids
    }


async def _async_wait_for_engine(
    hass: HomeAssistant, domain: str, host: str
) -> str | None:
    """Return an add-on's Wyoming engine, waiting (bounded) for its discovery.

    An engine that is already registered is returned at once. Otherwise this is
    driven by entity-registry updates (Wyoming registers the engine once the
    Supervisor discovery for the started add-on lands), so there is no polling.
    """
    appeared = asyncio.Event()

    @callback
    def _check(_event: object = None) -> None:
        if _wyoming_engine_ids(hass, domain, host):
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
    return sorted(_wyoming_engine_ids(hass, domain, host))[0]


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
    whisper_model: str | None,
    piper_voice: str,
    pipeline_name: str,
) -> VoiceSetupResult:
    """Install Whisper + Piper, discover their engines and build the pipeline."""
    # Answer the discovery prompts as they appear; keep a stored model unless
    # the caller named one.
    confirmations: set[asyncio.Task[None]] = set()
    seen: set[str] = set()
    claimed: set[str] = set()

    @callback
    def _on_flow(event: str, flow_id: str) -> None:
        if event == "added" and flow_id not in seen:
            seen.add(flow_id)
            task = hass.async_create_task(
                _async_confirm_discovery(hass, flow_id, claimed)
            )
            confirmations.add(task)
            task.add_done_callback(confirmations.discard)

    unsubscribe = hass.config_entries.flow.async_subscribe_flow(_on_flow)
    try:
        whisper_options: dict[str, object] = {"language": language}
        if whisper_model:
            whisper_options["model"] = whisper_model
        await _async_ensure_addon(
            hass, WHISPER_ADDON_SLUG, WHISPER_ADDON_NAME, whisper_options
        )
        await _async_ensure_addon(
            hass, PIPER_ADDON_SLUG, PIPER_ADDON_NAME, {"voice": piper_voice}
        )
        # A prompt left over from an earlier attempt is already pending.
        for flow in hass.config_entries.flow.async_progress_by_handler(
            WYOMING_PLATFORM, match_context={"source": "hassio"}
        ):
            _on_flow("added", flow["flow_id"])

        stt_engine = await _async_wait_for_engine(
            hass, "stt", _addon_host(WHISPER_ADDON_SLUG)
        )
        tts_engine = await _async_wait_for_engine(
            hass, "tts", _addon_host(PIPER_ADDON_SLUG)
        )
    finally:
        unsubscribe()
        if confirmations:
            await asyncio.gather(*confirmations)

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

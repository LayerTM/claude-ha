"""The ``claude_ha.ask`` service."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    entity_registry as er,
    selector,
)

from .confirm import ConfirmationRequest, async_send_proposal_notification
from .const import (
    ATTR_CONFIG_ENTRY,
    ATTR_INTENTS,
    ATTR_LANGUAGE,
    ATTR_MODE,
    ATTR_NOTIFY,
    ATTR_PIPELINE_NAME,
    ATTR_PROMPT,
    ATTR_STT_MODEL,
    ATTR_TTS_VOICE,
    DOMAIN,
    MAX_WRITE_INTENTS,
    MODE_READ,
    MODE_WRITE,
    MODES,
    PROMPT_MAX_BYTES,
    RESP_PROPOSAL,
    RESP_TEXT,
    RESP_TOOLS_USED,
    RESP_TRUNCATED,
    SERVICE_ASK,
    SERVICE_SETUP_VOICE,
)
from .coordinator import ClaudeConfigEntry
from .voice import DEFAULT_WHISPER_MODEL, async_setup_voice_pipeline, default_voice

ASK_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY): selector.ConfigEntrySelector(
            {"integration": DOMAIN}
        ),
        vol.Required(ATTR_PROMPT): cv.string,
        vol.Optional(ATTR_MODE, default=MODE_READ): vol.In(MODES),
        vol.Optional(ATTR_INTENTS): vol.All(cv.ensure_list, [dict]),
        vol.Optional(ATTR_NOTIFY): cv.string,
    }
)

SETUP_VOICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY): selector.ConfigEntrySelector(
            {"integration": DOMAIN}
        ),
        vol.Required(ATTR_LANGUAGE): cv.string,
        vol.Optional(ATTR_STT_MODEL): cv.string,
        vol.Optional(ATTR_TTS_VOICE): cv.string,
        vol.Optional(ATTR_PIPELINE_NAME): cv.string,
    }
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register integration services (available regardless of entry state)."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_ASK,
        _async_handle_ask,
        schema=ASK_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SETUP_VOICE,
        _async_handle_setup_voice,
        schema=SETUP_VOICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


def _async_get_entry(hass: HomeAssistant, call: ServiceCall) -> ClaudeConfigEntry:
    """Resolve the target config entry, defaulting to the single loaded entry."""
    entry_id = call.data.get(ATTR_CONFIG_ENTRY)
    if entry_id is not None:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_config_entry",
                translation_placeholders={"config_entry": entry_id},
            )
    else:
        entries = hass.config_entries.async_entries(DOMAIN)
        if len(entries) != 1:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_config_entry",
            )
        entry = entries[0]

    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
        )
    return entry


async def _async_handle_ask(call: ServiceCall) -> ServiceResponse:
    """Handle ``claude_ha.ask``: send a prompt to Claude and return the result."""
    entry = _async_get_entry(call.hass, call)
    prompt: str = call.data[ATTR_PROMPT]
    mode: str = call.data[ATTR_MODE]
    intents: list[dict[str, Any]] | None = call.data.get(ATTR_INTENTS)

    if len(prompt.encode("utf-8")) > PROMPT_MAX_BYTES:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="prompt_too_large",
            translation_placeholders={"max_bytes": str(PROMPT_MAX_BYTES)},
        )

    # Contract §2: write requires the user-confirmed proposal intents (max 5);
    # read forbids them. This keeps writes scoped to a prior read-mode proposal
    # rather than free-text.
    if mode == MODE_WRITE:
        if not intents:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="write_requires_intents"
            )
        if len(intents) > MAX_WRITE_INTENTS:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="too_many_intents",
                translation_placeholders={"max": str(MAX_WRITE_INTENTS)},
            )
    elif intents:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="intents_require_write"
        )

    # ClaudeError is a translated HomeAssistantError and surfaces as-is.
    result = await entry.runtime_data.client.async_prompt(
        prompt,
        mode=mode,
        caller=call.context.user_id,
        intents=intents,
    )

    # If a read produced a proposal and a notify target was given, ask the user
    # to confirm it (the write then runs on Approve).
    notify_service = call.data.get(ATTR_NOTIFY)
    if notify_service and mode == MODE_READ and result.proposal is not None:
        await async_send_proposal_notification(
            call.hass,
            entry,
            notify_service,
            ConfirmationRequest(prompt, result.proposal, call.context.user_id),
        )

    response: dict[str, Any] = {
        RESP_TEXT: result.text,
        RESP_PROPOSAL: (
            None
            if result.proposal is None
            else {
                "summary": result.proposal.summary,
                "intents": result.proposal.intents,
            }
        ),
        RESP_TOOLS_USED: result.tools_used,
        RESP_TRUNCATED: result.truncated,
    }
    return response


async def _async_handle_setup_voice(call: ServiceCall) -> ServiceResponse:
    """Handle ``claude_ha.setup_voice``: install Whisper/Piper and wire a pipeline."""
    hass = call.hass
    entry = _async_get_entry(hass, call)
    language = call.data[ATTR_LANGUAGE].lower()

    conversation_entity_id = er.async_get(hass).async_get_entity_id(
        "conversation", DOMAIN, entry.entry_id
    )
    if conversation_entity_id is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="entry_not_loaded"
        )

    voice: str | None = call.data.get(ATTR_TTS_VOICE) or default_voice(language)
    if not voice:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="voice_unknown_language",
            translation_placeholders={"language": language},
        )

    result = await async_setup_voice_pipeline(
        hass,
        conversation_entity_id,
        language=language,
        whisper_model=call.data.get(ATTR_STT_MODEL, DEFAULT_WHISPER_MODEL),
        piper_voice=voice,
        pipeline_name=call.data.get(ATTR_PIPELINE_NAME) or f"Claude ({language})",
    )
    return {
        "stt_engine": result.stt_engine,
        "tts_engine": result.tts_engine,
        "tts_voice": voice,
        "pipeline_id": result.pipeline_id,
        "created_pipeline": result.pipeline_id is not None,
    }

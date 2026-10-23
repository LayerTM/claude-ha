"""The integration's actions: ask, set up voice, and the remembered facts."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import (
    config_validation as cv,
    entity_registry as er,
    selector,
)

from .api import ClaudeError
from .confirm import ConfirmationRequest, async_send_proposal_notification
from .const import (
    ATTR_ALL,
    ATTR_CONFIG_ENTRY,
    ATTR_FACT_IDS,
    ATTR_HOUSEHOLD,
    ATTR_INTENTS,
    ATTR_LANGUAGE,
    ATTR_MODE,
    ATTR_NOTIFY,
    ATTR_PIPELINE_NAME,
    ATTR_PROMPT,
    ATTR_STT_MODEL,
    ATTR_TEXT,
    ATTR_TTS_VOICE,
    ATTR_WEB,
    CONF_MEMORY,
    CONF_VOICE_PIPELINE_ID,
    DOMAIN,
    MAX_WRITE_INTENTS,
    MODE_READ,
    MODE_WRITE,
    MODES,
    RESP_PROPOSAL,
    RESP_TEXT,
    RESP_TOOLS_USED,
    RESP_TRUNCATED,
    SERVICE_ASK,
    SERVICE_FORGET,
    SERVICE_LIST_MEMORY,
    SERVICE_REMEMBER,
    SERVICE_SETUP_VOICE,
)
from .coordinator import ClaudeConfigEntry
from .engines import engine_for_entry
from .memory import Fact, async_may_edit_household
from .schema import vol
from .voice import async_setup_voice_pipeline, default_voice
from .web import async_note_web_outcome

ASK_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY): selector.ConfigEntrySelector(
            {"integration": DOMAIN}
        ),
        vol.Required(ATTR_PROMPT): cv.string,
        vol.Optional(ATTR_MODE, default=MODE_READ): vol.In(MODES),
        vol.Optional(ATTR_INTENTS): vol.All(cv.ensure_list, [dict]),
        vol.Optional(ATTR_NOTIFY): cv.string,
        vol.Optional(ATTR_WEB, default=False): cv.boolean,
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

_ENTRY_FIELD = {
    vol.Optional(ATTR_CONFIG_ENTRY): selector.ConfigEntrySelector(
        {"integration": DOMAIN}
    )
}
REMEMBER_SCHEMA = vol.Schema(
    {
        **_ENTRY_FIELD,
        vol.Required(ATTR_TEXT): cv.string,
        vol.Optional(ATTR_HOUSEHOLD, default=False): cv.boolean,
    }
)
FORGET_SCHEMA = vol.Schema(
    {
        **_ENTRY_FIELD,
        vol.Optional(ATTR_FACT_IDS, default=list): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_ALL, default=False): cv.boolean,
    }
)
LIST_MEMORY_SCHEMA = vol.Schema(_ENTRY_FIELD)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register integration services (available regardless of entry state)."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_ASK,
        _async_handle_ask,
        schema=ASK_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SETUP_VOICE,
        _async_handle_setup_voice,
        schema=SETUP_VOICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REMEMBER,
        _async_handle_remember,
        schema=REMEMBER_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FORGET,
        _async_handle_forget,
        schema=FORGET_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_MEMORY,
        _async_handle_list_memory,
        schema=LIST_MEMORY_SCHEMA,
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

    # A web answer is text from the web alone: no home, no action, nothing to confirm.
    if call.data[ATTR_WEB]:
        return await _async_ask_web(call, entry, prompt, mode, intents)

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


async def _async_ask_web(
    call: ServiceCall,
    entry: ClaudeConfigEntry,
    prompt: str,
    mode: str,
    intents: list[dict[str, Any]] | None,
) -> ServiceResponse:
    """Answer ``claude_ha.ask`` with ``web: true``: text only, never a proposal."""
    if mode != MODE_READ or intents or call.data.get(ATTR_NOTIFY):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="web_requires_plain_read"
        )
    engine = engine_for_entry(entry)
    assert engine is not None
    try:
        result = await entry.runtime_data.client.async_prompt_web(
            prompt, caller=call.context.user_id
        )
    except ClaudeError as err:
        async_note_web_outcome(call.hass, entry.entry_id, engine, err)
        raise
    async_note_web_outcome(call.hass, entry.entry_id, engine, None)
    return {
        RESP_TEXT: result.text,
        RESP_PROPOSAL: None,
        RESP_TOOLS_USED: [],
        RESP_TRUNCATED: result.truncated,
    }


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

    engine = engine_for_entry(entry)
    assert engine is not None
    result = await async_setup_voice_pipeline(
        hass,
        conversation_entity_id,
        language=language,
        whisper_model=call.data.get(ATTR_STT_MODEL),
        piper_voice=voice,
        pipeline_name=call.data.get(ATTR_PIPELINE_NAME)
        or f"{engine.name} ({language})",
        pipeline_id=entry.data.get(CONF_VOICE_PIPELINE_ID),
    )
    created = result.pipeline_id is not None and result.pipeline_id != entry.data.get(
        CONF_VOICE_PIPELINE_ID
    )
    if created:
        # Remember the pipeline made here, so the next run re-wires this one and
        # never touches a pipeline of the user's own.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_VOICE_PIPELINE_ID: result.pipeline_id}
        )
    return {
        "stt_engine": result.stt_engine,
        "tts_engine": result.tts_engine,
        "tts_voice": voice,
        "pipeline_id": result.pipeline_id,
        "created_pipeline": created,
    }


def _fact_response(fact: Fact) -> dict[str, Any]:
    """Return a fact as an action response item."""
    return {
        "id": fact.id,
        "text": fact.text,
        "household": fact.user_id is None,
        "created": fact.created,
    }


async def _async_handle_remember(call: ServiceCall) -> ServiceResponse:
    """Handle ``claude_ha.remember``: keep a fact for the caller or household."""
    entry = _async_get_entry(call.hass, call)
    user_id = call.context.user_id
    household: bool = call.data[ATTR_HOUSEHOLD]
    if household and not await async_may_edit_household(call.hass, user_id):
        raise Unauthorized(context=call.context)
    fact = await entry.runtime_data.memory.async_remember(
        call.data[ATTR_TEXT], user_id, household=household
    )
    return _fact_response(fact)


async def _async_handle_forget(call: ServiceCall) -> ServiceResponse:
    """Handle ``claude_ha.forget``: remove facts the caller may remove."""
    entry = _async_get_entry(call.hass, call)
    memory = entry.runtime_data.memory
    user_id = call.context.user_id
    household = await async_may_edit_household(call.hass, user_id)
    if call.data[ATTR_ALL]:
        removed = await memory.async_forget_all(user_id, household=household)
    elif call.data[ATTR_FACT_IDS]:
        forgotten = await memory.async_forget(
            call.data[ATTR_FACT_IDS], user_id, household=household
        )
        removed = len(forgotten)
    else:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="forget_nothing_selected"
        )
    return {"removed": removed}


async def _async_handle_list_memory(call: ServiceCall) -> ServiceResponse:
    """Handle ``claude_ha.list_memory``: the facts the caller can see."""
    entry = _async_get_entry(call.hass, call)
    data = entry.runtime_data
    facts = data.memory.visible(call.context.user_id)
    enabled = bool(entry.options.get(CONF_MEMORY, True))
    return {
        "facts": [_fact_response(fact) for fact in facts],
        "enabled": enabled,
        # Whether chat turns carry the facts now: on, and an add-on that takes them.
        "used_in_chat": enabled and data.client.supports_memory,
    }

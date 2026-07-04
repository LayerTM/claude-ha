"""Conversation agent for Claude, backed by the Claude Code add-on.

Chat can ACT on Home Assistant: benign, model-low-risk actions run immediately;
anything the deterministic classifier (:mod:`.risk`) flags — or that the model
marks non-low, or that the user pinned as critical — is held and confirmed with a
plain yes/no. The confirmed write replays the exact validated intents, so it does
not depend on any model memory of what is being confirmed. The add-on's coarse
per-domain 403 is a backstop; Assist entity exposure is the outer ceiling.
"""

from __future__ import annotations

from typing import Any, Literal

from homeassistant.components import conversation
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import ClaudeError, Proposal
from .confirm import CHAT_PENDING_TTL, DATA_PENDING_CHAT, PendingProposal
from .const import (
    CONF_AUTO_EXECUTE,
    CONF_CRITICAL_ENTITIES,
    CONFIRMATION_AUTO,
    CONFIRMATION_CONFIRMED,
    MODE_READ,
    MODE_WRITE,
)
from .coordinator import ClaudeConfigEntry, ClaudeStatusCoordinator
from .entity import build_device_info
from .risk import is_auto_ok

# Declared for the parallel-updates quality rule. Conversation turns are not
# entity state updates, so this does not gate them; the add-on owns concurrency
# control (it returns 503 when its own cap is reached).
PARALLEL_UPDATES = 0

# Conservative yes/no vocabulary (case-insensitive, trimmed). Unknown => neither.
_AFFIRM = frozenset(
    {
        "yes",
        "yeah",
        "yep",
        "ok",
        "okay",
        "sure",
        "confirm",
        "do it",
        "go ahead",
        "так",
        "добре",
        "давай",
        "підтверджую",
    }
)
_DENY = frozenset({"no", "nope", "cancel", "stop", "don't", "ні", "скасуй", "відміна"})


def _decision(text: str) -> bool | None:
    """Return True (affirm), False (deny) or None (neither) for a reply."""
    normalized = text.strip().lower()
    if normalized in _AFFIRM:
        return True
    if normalized in _DENY:
        return False
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClaudeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the conversation entity from a config entry."""
    async_add_entities([ClaudeConversationEntity(entry.runtime_data.status)])


class ClaudeConversationEntity(conversation.ConversationEntity):
    """Forwards HA Assist messages to Claude and acts on its proposals."""

    _attr_has_entity_name = True
    _attr_name = None
    # The add-on returns a full JSON body, not a token stream.
    _attr_supports_streaming = False

    def __init__(self, coordinator: ClaudeStatusCoordinator) -> None:
        """Init from the runtime coordinator (which owns the API client)."""
        self.coordinator = coordinator
        entry = coordinator.config_entry
        self._attr_unique_id = entry.entry_id
        status = coordinator.data
        self._attr_device_info = build_device_info(
            entry,
            claude_version=status.claude_version if status else None,
            model=status.model if status else None,
        )

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Claude handles any language, so match all."""
        return MATCH_ALL

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Answer, and act on the request — automatically or after confirmation."""
        hass = self.coordinator.hass
        entry = self.coordinator.config_entry
        conv_id = chat_log.conversation_id
        caller = user_input.context.user_id
        text = user_input.text
        pending_store: dict[str, PendingProposal] = hass.data.setdefault(
            DATA_PENDING_CHAT, {}
        )

        # A. Resolve a confirmation that is pending for this conversation.
        pending = pending_store.pop(conv_id, None)
        if pending is not None:
            decision = _decision(text)
            if decision is True and pending.expires_at >= dt_util.utcnow():
                return await self._async_write(
                    user_input,
                    chat_log,
                    pending.prompt,
                    pending.intents,
                    pending.summary,
                )
            if decision is False:
                return self._reply(user_input, chat_log, "Cancelled.")
            # Neither/expired: fall through and treat as a fresh request.

        # B. Fresh read.
        try:
            result = await self.coordinator.client.async_prompt(
                text, mode=MODE_READ, conversation_id=conv_id, caller=caller
            )
        except ClaudeError as err:
            return self._error(user_input, chat_log, err)

        proposal = result.proposal
        if proposal is None or not proposal.intents:
            return self._reply(
                user_input, chat_log, _render_proposal(result.text, proposal)
            )

        summary = proposal.summary.strip() or "the requested change"

        # C/D. Auto-execute benign, model-low-risk actions.
        auto_execute = entry.options.get(CONF_AUTO_EXECUTE, True)
        critical = entry.options.get(CONF_CRITICAL_ENTITIES, [])
        if auto_execute and is_auto_ok(hass, proposal.intents, critical):
            try:
                await self.coordinator.client.async_prompt(
                    text,
                    mode=MODE_WRITE,
                    intents=proposal.intents,
                    confirmation=CONFIRMATION_AUTO,
                    conversation_id=conv_id,
                    caller=caller,
                )
            except ClaudeError:
                pass  # e.g. the add-on's 403 backstop -> fall through to confirm
            else:
                return self._reply(user_input, chat_log, f"Done: {summary}")

        # E. Hold the exact validated intents and ask for confirmation.
        pending_store[conv_id] = PendingProposal(
            entry_id=entry.entry_id,
            prompt=text,
            intents=proposal.intents,
            caller=caller,
            summary=summary,
            expires_at=dt_util.utcnow() + CHAT_PENDING_TTL,
        )
        message = f"{_render_proposal(result.text, proposal)}\n\nConfirm? (yes/no)"
        return self._reply(user_input, chat_log, message.strip())

    async def _async_write(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
        prompt: str,
        intents: list[dict[str, Any]],
        summary: str,
    ) -> conversation.ConversationResult:
        """Run a confirmed write with the stored, validated intents."""
        try:
            await self.coordinator.client.async_prompt(
                prompt,
                mode=MODE_WRITE,
                intents=intents,
                confirmation=CONFIRMATION_CONFIRMED,
                conversation_id=chat_log.conversation_id,
                caller=user_input.context.user_id,
            )
        except ClaudeError as err:
            return self._error(user_input, chat_log, err)
        return self._reply(user_input, chat_log, f"Done: {summary}")

    def _reply(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
        text: str,
    ) -> conversation.ConversationResult:
        """Record an assistant reply and build the result."""
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=user_input.agent_id, content=text)
        )
        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    def _error(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
        err: Exception,
    ) -> conversation.ConversationResult:
        """Build an error response without raising."""
        response = intent.IntentResponse(language=user_input.language)
        response.async_set_error(
            intent.IntentResponseErrorCode.UNKNOWN,
            str(err) or "Error talking to Claude.",
        )
        return conversation.ConversationResult(
            response=response, conversation_id=chat_log.conversation_id
        )


def _render_proposal(text: str, proposal: Proposal | None) -> str:
    """Combine Claude's answer with a described proposal (summary + targets)."""
    if proposal is None:
        return text
    lines = [text] if text else []
    summary = proposal.summary.strip()
    if summary:
        lines.append(f"\nProposed: {summary}")
    targets = sorted({t for item in proposal.intents for t in item.get("targets", [])})
    if targets:
        lines.append(f"Affects: {', '.join(targets)}.")
    return "\n".join(line for line in lines if line).strip()

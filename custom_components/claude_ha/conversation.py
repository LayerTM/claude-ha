"""Conversation agent for Claude, backed by the Claude Code add-on."""

from __future__ import annotations

from typing import Literal

from homeassistant.components import conversation
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import ClaudeError, Proposal
from .const import MODE_READ
from .coordinator import ClaudeConfigEntry, ClaudeCoordinator
from .entity import build_device_info

# Declared for the parallel-updates quality rule. Conversation turns are not
# entity state updates, so this does not gate them; the add-on owns concurrency
# control (it returns 503 when its own cap is reached).
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClaudeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the conversation entity from a config entry."""
    async_add_entities([ClaudeConversationEntity(entry.runtime_data)])


class ClaudeConversationEntity(conversation.ConversationEntity):
    """Forwards HA Assist messages to Claude and returns its reply.

    Runs every chat turn in read-only mode (contract §2): the add-on denies
    state-changing tools and, when Claude wants to change something, returns a
    ``proposal`` instead of acting. The proposal is surfaced in the reply so the
    user can act on it deliberately (via automations / the ``claude_ha.ask``
    action), never silently from untrusted chat text.
    """

    _attr_has_entity_name = True
    _attr_name = None
    # The add-on returns a full JSON body, not a token stream.
    _attr_supports_streaming = False

    def __init__(self, coordinator: ClaudeCoordinator) -> None:
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
        """Send the user's message to Claude and record the reply."""
        try:
            result = await self.coordinator.client.async_prompt(
                user_input.text,
                mode=MODE_READ,
                conversation_id=chat_log.conversation_id,
                caller=user_input.context.user_id,
            )
        except ClaudeError as err:
            response = intent.IntentResponse(language=user_input.language)
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                str(err) or "Error talking to Claude.",
            )
            return conversation.ConversationResult(
                response=response,
                conversation_id=chat_log.conversation_id,
            )

        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(
                agent_id=user_input.agent_id,
                content=_render_reply(result.text, result.proposal),
            )
        )
        return conversation.async_get_result_from_chat_log(user_input, chat_log)


def _render_reply(text: str, proposal: Proposal | None) -> str:
    """Combine Claude's answer with any proposed-but-unexecuted actions."""
    if proposal is None:
        return text
    lines = [text] if text else []
    summary = proposal.summary.strip()
    if summary:
        lines.append(f"\nProposed action: {summary}")
    if proposal.intents:
        targets = sorted(
            {target for item in proposal.intents for target in item.get("targets", [])}
        )
        if targets:
            lines.append(f"Affects: {', '.join(targets)}.")
    lines.append(
        "I did not make any changes. Ask again to confirm, or run it from an "
        "automation with the Claude: Ask action in write mode."
    )
    return "\n".join(line for line in lines if line).strip()

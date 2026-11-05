"""The fixed words of the conversation agent's own replies, in the user's language.

Every sentence the agent writes itself (the verdict of a write, the confirmation
question, "Cancelled.") is an ``exceptions`` translation of this integration, so
Home Assistant's own language fallback applies and a language is added by adding
a translation file. :data:`ENGLISH` is the text used when the translations cannot
be loaded; a test keeps it equal to ``strings.json``.
"""

from __future__ import annotations

from typing import Final

from homeassistant.core import HomeAssistant

from .api import async_exception_texts, exception_text

ENGLISH: Final = {
    "write_done": "Done: {summary}",
    "write_done_bare": "Done.",
    "write_not_done": "Not done: {summary}. Failed: {failed}",
    "write_went_through": ". Went through: {went_through}",
    "target_fallback": "the target",
    "requested_change": "the requested change",
    "write_unconfirmed": (
        "I couldn't confirm what happened with: {summary}. Check first."
    ),
    "proposed": "Proposed: {summary}",
    "affects": "Affects: {targets}.",
    "confirm_ask": "Confirm? (yes/no)",
    "confirm_spoken": "{summary}. Say yes to confirm.",
    "confirm_yes": "yes",
    "confirm_no": "no",
    "cancelled": "Cancelled.",
    "remember_line": "Remember: {fact}",
    "forget_line": "Forget: {fact}",
    "remembered": "Remembered: {fact}",
    "forgot": "Forgot: {fact}",
    "memory_dropped": "Not saved to memory — ask on its own to keep it.",
    "memory_needs_user": (
        "Not saved to memory: facts are kept from a signed-in chat or the Remember"
        " a fact action."
    ),
    "automation_fallback": "automation",
    "automation_draft": "Here's a draft automation — **{alias}**:",
    "automation_create_spoken": "Create automation {alias}",
    "automation_update_spoken": "Update automation {alias}",
    "automation_create_ask": "Create it? (yes/no)",
    "automation_update_ask": "Update it? (yes/no)",
    "automation_created": "Created automation: {summary}",
    "automation_updated": "Updated automation: {summary}",
    "automation_deleted": "Deleted automation: {summary}",
    "automation_delete_spoken": "Delete automation {name}",
    "automation_delete_ask": "Delete the automation {name}? (yes/no)",
    "automation_not_found": "I couldn't find an automation matching that.",
    "automation_ambiguous": "I found more than one — which? {names}",
    "automation_unreadable": "I couldn't read that automation's configuration.",
}


class Words:
    """The reply words of one conversation turn, in that turn's language."""

    def __init__(self, texts: dict[str, str]) -> None:
        """Keep the loaded ``exceptions`` translations."""
        self._texts = texts

    def __call__(self, key: str, **placeholders: str) -> str:
        """Return the sentence ``key`` with its placeholders filled in."""
        return exception_text(self._texts, key, ENGLISH[key], **placeholders)

    def vocabulary(self, key: str) -> frozenset[str]:
        """Return the comma-separated, lower-cased words of a list translation."""
        return frozenset(
            word.strip().lower() for word in self(key).split(",") if word.strip()
        )


async def async_words(hass: HomeAssistant, language: str) -> Words:
    """Load the reply words for ``language`` (English where there is no text)."""
    return Words(await async_exception_texts(hass, language))

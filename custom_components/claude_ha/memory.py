"""Remembered facts, kept across conversations.

Only short statements the user explicitly asked to keep ("my room is the
office") are stored — never states, answers or anything the model inferred. A
fact is saved only after the user's yes (chat) or an explicit action call, so
nothing reaches the store silently. Each config entry has its own store, with a
household set (turns without a Home Assistant user: voice satellites,
automations) and one set per user; a turn sees the household set and its own
user's facts, never another user's. Since the household set reaches everyone's
turns, only an administrator — or a turn without a user, which an administrator
set up — may change it.

The fact text is personal data: it is sent to the add-on as data for the model
and returned by ``claude_ha.list_memory``, and it never appears in logs or
diagnostics (counts only).
"""

from __future__ import annotations

from dataclasses import dataclass
import secrets
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import ClaudeError, MemoryItem, normalize_fact
from .const import DOMAIN, MAX_FACT_CHARS

STORAGE_VERSION: Final = 1
# The most facts one set (the household's, or one user's) holds.
MAX_FACTS: Final = 50
# The most UTF-8 bytes of fact text sent with one turn; the newest facts win.
MAX_SENT_BYTES: Final = 16 * 1024


@dataclass(slots=True, frozen=True)
class Fact:
    """One remembered fact. ``user_id`` None means the household set."""

    id: str
    text: str
    user_id: str | None
    created: str


class MemoryFullError(ClaudeError):
    """A set already holds :data:`MAX_FACTS` facts."""

    translation_key = "memory_full"

    def __init__(self) -> None:
        """Build the translated error."""
        super().__init__(translation_placeholders={"max": str(MAX_FACTS)})


class FactInvalidError(ClaudeError):
    """The text is empty, too long or holds control characters."""

    translation_key = "memory_fact_invalid"

    def __init__(self) -> None:
        """Build the translated error."""
        super().__init__(translation_placeholders={"max": str(MAX_FACT_CHARS)})


def _storage_key(entry_id: str) -> str:
    """Return the store key of one config entry's memory."""
    return f"{DOMAIN}.memory.{entry_id}"


class Memory:
    """One config entry's remembered facts, persisted in a Home Assistant Store."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Bind to the entry's store; call :meth:`async_load` before use."""
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, _storage_key(entry_id)
        )
        self._facts: list[Fact] = []

    async def async_load(self) -> None:
        """Read the stored facts; a missing or malformed store is empty."""
        data = await self._store.async_load()
        facts = data.get("facts") if isinstance(data, dict) else None
        self._facts = [
            Fact(
                id=item["id"],
                text=item["text"],
                user_id=item.get("user_id"),
                created=item.get("created", ""),
            )
            for item in (facts if isinstance(facts, list) else [])
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and normalize_fact(item.get("text")) == item.get("text")
        ]

    async def _async_save(self) -> None:
        """Persist the current facts."""
        await self._store.async_save(
            {
                "facts": [
                    {
                        "id": fact.id,
                        "text": fact.text,
                        "user_id": fact.user_id,
                        "created": fact.created,
                    }
                    for fact in self._facts
                ]
            }
        )

    def visible(self, user_id: str | None) -> list[Fact]:
        """Return the facts a turn by ``user_id`` sees: household + its own."""
        return [
            fact
            for fact in self._facts
            if fact.user_id is None or fact.user_id == user_id
        ]

    def for_prompt(self, user_id: str | None) -> list[MemoryItem]:
        """Return the facts to send with a turn, newest first within the budget."""
        sent: list[MemoryItem] = []
        used = 0
        for fact in reversed(self.visible(user_id)):
            size = len(fact.text.encode("utf-8"))
            if used + size > MAX_SENT_BYTES:
                break
            used += size
            sent.append({"id": fact.id, "text": fact.text})
        sent.reverse()
        return sent

    async def async_remember(
        self, text: str, user_id: str | None, *, household: bool = False
    ) -> Fact:
        """Store a fact in the caller's set (or the household's); return it.

        The same fact already in that set is returned as is, not stored twice.
        """
        fact_text = normalize_fact(text)
        if fact_text is None:
            raise FactInvalidError
        owner = None if household else user_id
        same_set = [fact for fact in self._facts if fact.user_id == owner]
        for fact in same_set:
            if fact.text.casefold() == fact_text.casefold():
                return fact
        if len(same_set) >= MAX_FACTS:
            raise MemoryFullError
        fact = Fact(
            id=secrets.token_hex(4),
            text=fact_text,
            user_id=owner,
            created=dt_util.utcnow().isoformat(),
        )
        self._facts.append(fact)
        await self._async_save()
        return fact

    def editable(self, user_id: str | None, *, household: bool) -> list[Fact]:
        """Return the facts a caller may remove.

        That is its own, and the household's when ``household`` (see
        :func:`async_may_edit_household`).
        """
        return [
            fact
            for fact in self._facts
            if (fact.user_id is None and household)
            or (fact.user_id is not None and fact.user_id == user_id)
        ]

    async def async_forget(
        self,
        ids: list[str] | tuple[str, ...],
        user_id: str | None,
        *,
        household: bool,
    ) -> list[Fact]:
        """Remove the given facts the caller may remove; return the removed ones."""
        wanted = set(ids)
        removed = [
            fact
            for fact in self.editable(user_id, household=household)
            if fact.id in wanted
        ]
        if removed:
            self._facts = [fact for fact in self._facts if fact not in removed]
            await self._async_save()
        return removed

    async def async_forget_all(self, user_id: str | None, *, household: bool) -> int:
        """Remove every fact the caller may remove; return how many."""
        ids = [fact.id for fact in self.editable(user_id, household=household)]
        return len(await self.async_forget(ids, user_id, household=household))

    def counts(self) -> dict[str, int]:
        """Return how many facts are stored — the only view diagnostics get."""
        return {
            "household": sum(1 for fact in self._facts if fact.user_id is None),
            "users": len({fact.user_id for fact in self._facts if fact.user_id}),
            "total": len(self._facts),
        }


async def async_may_edit_household(hass: HomeAssistant, user_id: str | None) -> bool:
    """Whether a caller may change the household set.

    A call without a user comes from something an administrator set up (an
    automation, a voice satellite); a user must be an administrator.
    """
    if user_id is None:
        return True
    user = await hass.auth.async_get_user(user_id)
    return user is not None and user.is_admin


async def async_remove_memory(hass: HomeAssistant, entry_id: str) -> None:
    """Delete a removed config entry's stored facts."""
    await Store[dict[str, Any]](
        hass, STORAGE_VERSION, _storage_key(entry_id)
    ).async_remove()

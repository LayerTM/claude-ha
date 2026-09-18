"""Repair issues owned by one config entry.

Several entries can be loaded at once (one per companion add-on), and each one
raises and clears its own issues on its own schedule. An issue id is therefore
the issue kind plus the entry id, while the translation key stays the kind, so
the text a user reads is the same whichever entry raised it.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

# Keys every issue's ``data`` carries, so a fix flow can tell the kind and the
# owning entry apart without parsing the issue id.
ISSUE_DATA_KIND = "issue"
ISSUE_DATA_ENTRY_ID = "entry_id"


def entry_issue_id(issue: str, entry_id: str) -> str:
    """Return the registry id of issue kind ``issue`` for entry ``entry_id``."""
    return f"{issue}_{entry_id}"


@callback
def async_raise_issue(
    hass: HomeAssistant,
    entry_id: str,
    issue: str,
    *,
    severity: ir.IssueSeverity,
    fixable: bool = False,
    persistent: bool = False,
    learn_more_url: str | None = None,
    placeholders: dict[str, str] | None = None,
    data: dict[str, str | int | float | None] | None = None,
) -> None:
    """Raise issue kind ``issue`` for one entry.

    ``persistent`` survives a real Home Assistant restart (its ``data`` and
    the rest of its fields are written to storage); the default does not — a
    restart restores it with ``data=None``, fine for a level check re-derived
    from live state every poll, wrong for an issue whose own ``data`` is the
    only memory of something that already happened.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        entry_issue_id(issue, entry_id),
        is_fixable=fixable,
        is_persistent=persistent,
        severity=severity,
        translation_key=issue,
        translation_placeholders=placeholders,
        learn_more_url=learn_more_url,
        data={ISSUE_DATA_KIND: issue, ISSUE_DATA_ENTRY_ID: entry_id, **(data or {})},
    )


@callback
def async_clear_issues(hass: HomeAssistant, entry_id: str, *issues: str) -> None:
    """Remove the given issue kinds of one entry, leaving other entries' alone."""
    for issue in issues:
        ir.async_delete_issue(hass, DOMAIN, entry_issue_id(issue, entry_id))

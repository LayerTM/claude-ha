"""Web answers: the repair hint when the add-on's web option is off."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .api import ClaudeError, ClaudeWebDisabledError
from .const import ISSUE_WEB_DISABLED
from .engines import Engine
from .issues import async_clear_issues, async_raise_issue


@callback
def async_note_web_outcome(
    hass: HomeAssistant, entry_id: str, engine: Engine, err: ClaudeError | None
) -> None:
    """Raise the repair hint when web answers are refused, clear it when one lands.

    Only the add-on's own refusal says its web option is off; any other outcome
    leaves the hint as it was.
    """
    if err is None:
        async_clear_issues(hass, entry_id, ISSUE_WEB_DISABLED)
    elif isinstance(err, ClaudeWebDisabledError):
        async_raise_issue(
            hass,
            entry_id,
            ISSUE_WEB_DISABLED,
            severity=ir.IssueSeverity.WARNING,
            placeholders=engine.placeholders,
        )

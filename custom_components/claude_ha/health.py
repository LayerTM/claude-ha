"""Proactive health check for the 'chat can't see the home' failure.

The most common dead-on-arrival gap is a chat that reaches the add-on but cannot
read or act on Home Assistant — the add-on isn't logged in, has no HA token, the
Model Context Protocol Server integration is missing, or nothing is exposed to
Assist. This module turns those states into actionable repair issues.

Evaluation is a cheap, side-effect-free callback run off the status poll (no
Claude call, so no cost); only the manual button fires a tiny "ping" read to
populate ``ha_mcp_connected`` for the deep reachability check.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .api import ClaudeClient, ClaudeError, StatusResult
from .const import (
    ASSIST_ASSISTANT,
    DOMAIN,
    HEALTH_PROBE_PROMPT,
    ISSUE_CAMERA_VISION_NO_CAMERAS,
    ISSUE_MCP_UNREACHABLE,
    ISSUE_NO_EXPOSED_ENTITIES,
    ISSUE_NO_HA_TOKEN,
    ISSUE_NOT_LOGGED_IN,
    LOGGER,
    MCP_SERVER_DOMAIN,
    MODE_READ,
)

# All health issues, each with its severity and a doc link for the fix.
_ISSUES: dict[str, tuple[ir.IssueSeverity, str]] = {
    ISSUE_NOT_LOGGED_IN: (
        ir.IssueSeverity.ERROR,
        "https://github.com/LayerTM/claude-ha#the-conversation-agent",
    ),
    ISSUE_NO_HA_TOKEN: (
        ir.IssueSeverity.ERROR,
        "https://github.com/LayerTM/claude-ha#the-conversation-agent",
    ),
    ISSUE_MCP_UNREACHABLE: (
        ir.IssueSeverity.ERROR,
        "https://www.home-assistant.io/integrations/mcp_server/",
    ),
    ISSUE_NO_EXPOSED_ENTITIES: (
        ir.IssueSeverity.WARNING,
        "https://www.home-assistant.io/voice_control/voice_remote_expose_devices/",
    ),
}


@dataclass(slots=True)
class HealthReport:
    """The outcome of one health evaluation."""

    problem: str | None
    exposed_to_assist: int | None
    mcp_server_loaded: bool
    ha_mcp_connected: bool | None
    camera_vision_inert: bool = False


@callback
def _assist_exposed_count(hass: HomeAssistant) -> int | None:
    """Count entities exposed to Assist, or None if exposure data isn't ready."""
    try:
        return sum(
            async_should_expose(hass, ASSIST_ASSISTANT, entity_id)
            for entity_id in hass.states.async_entity_ids()
        )
    except KeyError:
        return None


@callback
def _exposed_camera_count(hass: HomeAssistant) -> int | None:
    """Count cameras exposed to Assist, or None if exposure data isn't ready."""
    try:
        return sum(
            async_should_expose(hass, ASSIST_ASSISTANT, entity_id)
            for entity_id in hass.states.async_entity_ids("camera")
        )
    except KeyError:
        return None


@callback
def evaluate(
    hass: HomeAssistant,
    status: StatusResult | None,
    camera_vision: bool = False,
) -> HealthReport:
    """Classify the single most fundamental 'Claude can't see the home' problem.

    Pure and cheap: reads only status and HA state, fires no request. Unknown
    signals (``None``) never raise a problem — only positively-bad states do. A
    missing ``mcp_server`` is trusted only once HA is fully running, so a startup
    transient (the integration still loading after a restart) doesn't flash a
    scary repair that then clears itself.
    """
    exposed = _assist_exposed_count(hass)
    mcp_loaded = MCP_SERVER_DOMAIN in hass.config.components
    connected = status.ha_mcp_connected if status is not None else None
    running = hass.state is CoreState.running

    problem: str | None = None
    if status is None:
        problem = None  # transport failures are handled by the setup path
    elif not status.ready:
        problem = ISSUE_NOT_LOGGED_IN
    elif status.ha_mcp is False:
        problem = ISSUE_NO_HA_TOKEN
    elif connected is False or (not mcp_loaded and running):
        problem = ISSUE_MCP_UNREACHABLE
    elif exposed == 0:
        problem = ISSUE_NO_EXPOSED_ENTITIES

    # Independent advisory: vision is on but no camera is exposed, so it can never
    # fire. Only surfaced when the fundamentals are otherwise fine, and only once HA
    # is fully running — at startup the camera entities aren't loaded yet, so a bare
    # count of 0 is a transient, not a real "no cameras exposed".
    camera_vision_inert = (
        camera_vision
        and problem is None
        and running
        and _exposed_camera_count(hass) == 0
    )

    return HealthReport(
        problem=problem,
        exposed_to_assist=exposed,
        mcp_server_loaded=mcp_loaded,
        ha_mcp_connected=connected,
        camera_vision_inert=camera_vision_inert,
    )


@callback
def async_apply(hass: HomeAssistant, report: HealthReport) -> None:
    """Raise the active health issue and clear the others."""
    for issue_id, (severity, learn_more_url) in _ISSUES.items():
        if issue_id == report.problem:
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=severity,
                translation_key=issue_id,
                learn_more_url=learn_more_url,
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id)

    # Independent of the single-problem set above: the camera-vision advisory can
    # coexist with an otherwise-healthy home, so raise/clear it on its own.
    if report.camera_vision_inert:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_CAMERA_VISION_NO_CAMERAS,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_CAMERA_VISION_NO_CAMERAS,
            learn_more_url="https://www.home-assistant.io/voice_control/voice_remote_expose_devices/",
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_CAMERA_VISION_NO_CAMERAS)


async def async_probe(hass: HomeAssistant, client: ClaudeClient) -> None:
    """Fire a tiny read so the add-on populates ``ha_mcp_connected``.

    A failed probe is swallowed: it just leaves connectivity unknown, and the
    next status poll re-evaluates whatever the add-on now reports.
    """
    try:
        await client.async_prompt(HEALTH_PROBE_PROMPT, mode=MODE_READ)
    except ClaudeError as err:
        LOGGER.debug("Health probe read failed: %s", err)

"""Tests for the health-check evaluator, repairs and probe."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from custom_components.claude_ha import health
from custom_components.claude_ha.api import ClaudeConnectionError, StatusResult
from custom_components.claude_ha.const import (
    DOMAIN,
    ISSUE_MCP_UNREACHABLE,
    ISSUE_NO_EXPOSED_ENTITIES,
    ISSUE_NO_HA_TOKEN,
    ISSUE_NOT_LOGGED_IN,
    MCP_SERVER_DOMAIN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir


def _status(**over: object) -> StatusResult:
    base: dict[str, object] = {
        "ready": True,
        "version": "1.14.0",
        "claude_version": "2.0.1",
        "model": "m",
        "ha_mcp": True,
        "ha_mcp_connected": True,
    }
    base.update(over)
    return StatusResult(**base)  # type: ignore[arg-type]


@pytest.fixture
def expose(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    """Return a helper to force async_should_expose to a fixed verdict."""

    def _set(exposed: bool) -> None:
        monkeypatch.setattr(health, "async_should_expose", lambda *_a, **_k: exposed)

    return _set


def _healthy(hass: HomeAssistant) -> None:
    """Put the environment in the fully-healthy shape."""
    hass.config.components.add(MCP_SERVER_DOMAIN)
    hass.states.async_set("light.kitchen", "on")


async def test_evaluate_ok(hass: HomeAssistant, expose: Callable[[bool], None]) -> None:
    """A logged-in, MCP-connected, exposed home has no problem."""
    _healthy(hass)
    expose(True)
    assert health.evaluate(hass, _status()).problem is None


async def test_evaluate_status_none(hass: HomeAssistant) -> None:
    """No status (transport failure) never raises a health problem."""
    assert health.evaluate(hass, None).problem is None


async def test_evaluate_not_logged_in(
    hass: HomeAssistant, expose: Callable[[bool], None]
) -> None:
    """A not-ready add-on surfaces the not-logged-in problem."""
    _healthy(hass)
    expose(True)
    assert health.evaluate(hass, _status(ready=False)).problem == ISSUE_NOT_LOGGED_IN


async def test_evaluate_no_ha_token(
    hass: HomeAssistant, expose: Callable[[bool], None]
) -> None:
    """A false ha_mcp surfaces the no-HA-token problem."""
    _healthy(hass)
    expose(True)
    assert health.evaluate(hass, _status(ha_mcp=False)).problem == ISSUE_NO_HA_TOKEN


async def test_evaluate_mcp_not_loaded(
    hass: HomeAssistant, expose: Callable[[bool], None]
) -> None:
    """A missing mcp_server integration surfaces the unreachable problem."""
    expose(True)
    hass.states.async_set("light.kitchen", "on")
    assert health.evaluate(hass, _status()).problem == ISSUE_MCP_UNREACHABLE


async def test_evaluate_mcp_reported_disconnected(
    hass: HomeAssistant, expose: Callable[[bool], None]
) -> None:
    """A false ha_mcp_connected surfaces the unreachable problem."""
    _healthy(hass)
    expose(True)
    report = health.evaluate(hass, _status(ha_mcp_connected=False))
    assert report.problem == ISSUE_MCP_UNREACHABLE


async def test_evaluate_null_connectivity_is_not_a_problem(
    hass: HomeAssistant, expose: Callable[[bool], None]
) -> None:
    """A never-probed (null) connectivity does not raise on its own."""
    _healthy(hass)
    expose(True)
    assert health.evaluate(hass, _status(ha_mcp_connected=None)).problem is None


async def test_evaluate_no_exposed_entities(
    hass: HomeAssistant, expose: Callable[[bool], None]
) -> None:
    """Zero exposed entities surfaces the nothing-exposed problem."""
    _healthy(hass)
    expose(False)
    report = health.evaluate(hass, _status())
    assert report.problem == ISSUE_NO_EXPOSED_ENTITIES
    assert report.exposed_to_assist == 0


async def test_exposed_count_handles_missing_store(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the exposure store isn't ready, the count is None (not a crash)."""
    _healthy(hass)

    def _raise(*_a: object, **_k: object) -> bool:
        raise KeyError

    monkeypatch.setattr(health, "async_should_expose", _raise)
    report = health.evaluate(hass, _status())
    assert report.exposed_to_assist is None
    # None is not zero, so the nothing-exposed problem does not fire.
    assert report.problem is None


async def test_apply_raises_active_and_clears_others(hass: HomeAssistant) -> None:
    """async_apply raises exactly the active issue and clears the rest."""
    registry = ir.async_get(hass)
    health.async_apply(hass, health.HealthReport(ISSUE_NO_HA_TOKEN, 1, True, True))
    assert registry.async_get_issue(DOMAIN, ISSUE_NO_HA_TOKEN) is not None
    assert registry.async_get_issue(DOMAIN, ISSUE_MCP_UNREACHABLE) is None

    # A later clean report clears the previously-raised issue.
    health.async_apply(hass, health.HealthReport(None, 1, True, True))
    assert registry.async_get_issue(DOMAIN, ISSUE_NO_HA_TOKEN) is None


async def test_probe_sends_ping(hass: HomeAssistant) -> None:
    """The probe fires a read against the client."""
    calls: list[tuple[str, str]] = []

    class _Client:
        async def async_prompt(self, prompt: str, *, mode: str, **_: object) -> None:
            calls.append((prompt, mode))

    await health.async_probe(hass, _Client())  # type: ignore[arg-type]
    assert calls == [("ping", "read")]


async def test_probe_swallows_errors(hass: HomeAssistant) -> None:
    """A failed probe is swallowed rather than raised."""

    class _Client:
        async def async_prompt(self, *_: object, **__: object) -> None:
            raise ClaudeConnectionError("down")

    await health.async_probe(hass, _Client())  # type: ignore[arg-type]

"""Tests for the account-wide limit sensors."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any
from unittest.mock import MagicMock

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.api import (
    AccountLimit,
    AccountLimitsResult,
    ClaudeClient,
    _parse_limits,
)
from custom_components.claude_ha.const import DOMAIN, USAGE_SCAN_INTERVAL
from custom_components.claude_ha.engines import CLAUDE
from custom_components.claude_ha.sensor import ClaudeAccountLimitSensor
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .conftest import (
    ACCOUNT_LIMITS_PAYLOAD,
    STATUS_PAYLOAD,
    TEST_BASE_URL,
    TEST_TOKEN,
    USAGE_PAYLOAD,
    setup_integration,
)

LIMITS_URL = f"{TEST_BASE_URL}/api/account_limits"
PACKAGE_LOGGER = "custom_components.claude_ha"

SESSION_LIMIT = {
    "kind": "session",
    "percent": 14,
    "severity": "normal",
    "resets_at": "2026-09-14T15:30:00+00:00",
    "model": None,
}
SCOPED_LIMIT = {
    "kind": "weekly_scoped",
    "percent": 88,
    "severity": "warning",
    "resets_at": "2026-09-16T13:00:00+00:00",
    "model": "Fable",
}


def _serve(
    aioclient_mock: AiohttpClientMocker,
    payload: dict[str, Any] | None = None,
    *,
    status: int = 200,
) -> None:
    """Answer the add-on endpoints, with the given account-limits response."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", json=STATUS_PAYLOAD)
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    if status == 200:
        aioclient_mock.get(LIMITS_URL, json=payload)
    else:
        aioclient_mock.get(
            LIMITS_URL, status=status, json={"error": "account limits unavailable"}
        )


def _limits(*entries: dict[str, Any], mode: str = "subscription") -> dict[str, Any]:
    """Build an account-limits response carrying the given limits."""
    return {
        "fetched_at": "2026-09-14T14:02:11Z",
        "mode": mode,
        "limits": list(entries),
    }


def _entity_id(hass: HomeAssistant, entry: MockConfigEntry, suffix: str) -> str | None:
    """Return the entity id registered for one limit sensor, if it exists."""
    return er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{suffix}"
    )


def _loud(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return this integration's records at WARNING or above."""
    return [
        r
        for r in caplog.records
        if r.name.startswith(PACKAGE_LOGGER) and r.levelno >= logging.WARNING
    ]


async def _poll_again(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Move past the next account-limits poll and let it settle.

    The extra second covers the coordinator scheduling its poll at the current
    whole second plus a random fraction plus the interval.
    """
    freezer.tick(timedelta(seconds=USAGE_SCAN_INTERVAL + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_limit_sensors_read_the_contract_response(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """The three limits of the contract response each get a sensor."""
    await setup_integration(hass, mock_config_entry)

    session = hass.states.get(_entity_id(hass, mock_config_entry, "session_limit"))
    assert session is not None
    assert session.state == "14"
    assert session.attributes["unit_of_measurement"] == "%"
    assert session.attributes["state_class"] == "measurement"
    assert session.attributes["kind"] == "session"
    assert session.attributes["severity"] == "normal"
    assert session.attributes["resets_at"].isoformat() == "2026-09-14T15:30:00+00:00"
    assert "model" not in session.attributes

    weekly = hass.states.get(_entity_id(hass, mock_config_entry, "weekly_limit"))
    assert weekly is not None
    assert weekly.state == "82"
    assert weekly.attributes["severity"] == "warning"

    scoped = hass.states.get(_entity_id(hass, mock_config_entry, "weekly_limit_fable"))
    assert scoped is not None
    assert scoped.state == "88"
    assert scoped.attributes["model"] == "Fable"
    assert scoped.attributes["kind"] == "weekly_scoped"
    # The scoped sensor is named after its model, so it is readable on a dashboard.
    assert scoped.attributes["friendly_name"] == "Claude Code Weekly limit (Fable)"


async def test_api_key_account_creates_no_limit_entities(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An API-key account has no limits: no entities, nothing unavailable, no noise.

    Such an account genuinely has no rate-limit buckets upstream, so the add-on
    answers 200 with an empty list. Nothing about that is a failure, and the
    usage sensors must keep working exactly as before.
    """
    _serve(aioclient_mock, _limits(mode="api_key"))
    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    for suffix in ("session_limit", "weekly_limit", "weekly_limit_fable"):
        assert _entity_id(hass, mock_config_entry, suffix) is None
    # Nothing was added and then hidden: the entity registry holds no limit sensor.
    entities = er.async_get(hass).entities
    assert not [e for e in entities.values() if "_limit" in (e.unique_id or "")]

    usage = hass.states.get(_entity_id(hass, mock_config_entry, "usage"))
    assert usage is not None
    assert usage.state == "1500"
    assert _loud(caplog) == []


@pytest.mark.parametrize("status", [404, 503])
async def test_endpoint_without_an_answer_is_quiet(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    """An add-on without the endpoint (404) or without upstream (503) is quiet."""
    _serve(aioclient_mock, status=status)
    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert _entity_id(hass, mock_config_entry, "session_limit") is None
    assert _loud(caplog) == []
    errors = [
        r
        for r in caplog.records
        if r.name.startswith(PACKAGE_LOGGER) and r.levelno >= logging.ERROR
    ]
    assert errors == []
    # The usage sensors are untouched by the limits endpoint failing.
    usage = hass.states.get(_entity_id(hass, mock_config_entry, "usage"))
    assert usage is not None
    assert usage.state == "1500"


async def test_a_new_model_gets_a_sensor_and_keeps_it(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A scoped model that appears is added; one that goes silent goes unavailable."""
    _serve(aioclient_mock, _limits(SESSION_LIMIT))
    await setup_integration(hass, mock_config_entry)
    assert _entity_id(hass, mock_config_entry, "weekly_limit_fable") is None

    _serve(aioclient_mock, _limits(SESSION_LIMIT, SCOPED_LIMIT))
    await _poll_again(hass, freezer)

    scoped_id = _entity_id(hass, mock_config_entry, "weekly_limit_fable")
    assert scoped_id is not None
    assert hass.states.get(scoped_id).state == "88"

    # The account stops reporting that model: the sensor stays, without a value.
    _serve(aioclient_mock, _limits(SESSION_LIMIT))
    await _poll_again(hass, freezer)

    assert hass.states.get(scoped_id).state == STATE_UNAVAILABLE
    assert _entity_id(hass, mock_config_entry, "weekly_limit_fable") == scoped_id
    assert (
        hass.states.get(_entity_id(hass, mock_config_entry, "session_limit")).state
        == "14"
    )


async def test_an_unknown_kind_still_gets_a_sensor(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A kind the contract does not name yet is shown, not silently dropped."""
    _serve(
        aioclient_mock,
        _limits(
            {
                "kind": "monthly_all",
                "percent": 5,
                "severity": "normal",
                "resets_at": "2026-10-01T00:00:00+00:00",
                "model": None,
            },
            {
                "kind": "monthly_scoped",
                "percent": 7,
                "severity": "normal",
                "resets_at": "2026-10-01T00:00:00+00:00",
                "model": "Opus",
            },
        ),
    )
    await setup_integration(hass, mock_config_entry)

    plain = hass.states.get(_entity_id(hass, mock_config_entry, "limit_monthly_all"))
    assert plain is not None
    assert plain.state == "5"
    assert plain.attributes["friendly_name"] == "Claude Code Limit (monthly_all)"

    scoped = hass.states.get(
        _entity_id(hass, mock_config_entry, "limit_monthly_scoped_opus")
    )
    assert scoped is not None
    assert scoped.state == "7"
    assert scoped.attributes["model"] == "Opus"


async def test_an_unreadable_limit_is_dropped_not_defaulted(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A limit with no usable percentage gets no sensor; its siblings still do.

    Defaulting it would state how much of the allowance is gone, which is
    exactly the claim nobody can stand behind here.
    """
    _serve(
        aioclient_mock,
        _limits(
            SESSION_LIMIT,
            {**SCOPED_LIMIT, "percent": None},
            {
                "kind": "weekly_all",
                "percent": 150,
                "severity": "warning",
                "resets_at": None,
                "model": None,
            },
        ),
    )
    await setup_integration(hass, mock_config_entry)

    assert (
        hass.states.get(_entity_id(hass, mock_config_entry, "session_limit")).state
        == "14"
    )
    assert _entity_id(hass, mock_config_entry, "weekly_limit_fable") is None
    assert _entity_id(hass, mock_config_entry, "weekly_limit") is None


async def test_limits_reach_diagnostics(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """The last account-limits report is in the entry's diagnostics."""
    from custom_components.claude_ha.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    await setup_integration(hass, mock_config_entry)
    report = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert report["account_limits"] == ACCOUNT_LIMITS_PAYLOAD


@pytest.mark.parametrize(
    "limits",
    [
        pytest.param("nope", id="not-a-list"),
        pytest.param([SESSION_LIMIT, "nope"], id="entry-not-an-object"),
        pytest.param(
            [SESSION_LIMIT, {**SCOPED_LIMIT, "percent": True}], id="percent-a-boolean"
        ),
        pytest.param(
            [SESSION_LIMIT, {**SCOPED_LIMIT, "percent": float("inf")}],
            id="percent-infinite",
        ),
        pytest.param([SESSION_LIMIT, {**SCOPED_LIMIT, "kind": ""}], id="kind-empty"),
    ],
)
async def test_malformed_shapes_never_become_a_sensor(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    limits: Any,
) -> None:
    """A shape the contract does not allow is dropped, and never raises.

    A payload that raised out of the poll would take every other sensor of the
    entry down with it, which is how the status poll used to fail.
    """
    _serve(
        aioclient_mock,
        {
            "fetched_at": "2026-09-14T14:02:11Z",
            "mode": "subscription",
            "limits": limits,
        },
    )
    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert _entity_id(hass, mock_config_entry, "weekly_limit_fable") is None
    assert _loud(caplog) == []


async def test_client_drops_percentages_it_cannot_trust(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Only a plain 0-100 number is a percentage; everything else is dropped.

    Read through the client rather than through entities, because these shapes
    must never reach an entity in the first place: an infinite or out-of-range
    figure passed on as a reading would state how much of the account's
    allowance is gone, which is precisely what nobody can stand behind.
    """
    aioclient_mock.get(
        LIMITS_URL,
        json=_limits(
            SESSION_LIMIT,
            {**SCOPED_LIMIT, "kind": "inf", "percent": float("inf")},
            {**SCOPED_LIMIT, "kind": "bool", "percent": True},
            {**SCOPED_LIMIT, "kind": "text", "percent": "88"},
            {**SCOPED_LIMIT, "kind": "over", "percent": 150},
        ),
    )
    client = ClaudeClient(
        async_get_clientsession(hass),
        base_url=TEST_BASE_URL,
        token=TEST_TOKEN,
        engine=CLAUDE,
    )

    result = await client.async_get_account_limits()

    assert [limit.kind for limit in result.limits] == ["session"]
    assert result.mode == "subscription"
    assert result.fetched_at is not None


def test_a_vanished_limit_states_nothing(
    mock_config_entry: MockConfigEntry,
) -> None:
    """A sensor whose limit is gone reports no value and no attributes.

    Home Assistant never reads an unavailable entity's value or attributes, so
    the guards are exercised directly, in the manner of the chat-health guards.
    """
    limit = AccountLimit(
        kind="weekly_scoped",
        percent=88,
        severity="warning",
        resets_at=None,
        model="Fable",
    )
    coordinator = MagicMock()
    coordinator.config_entry = mock_config_entry
    coordinator.data = AccountLimitsResult(
        mode="subscription", fetched_at=None, limits=(limit,), report={}
    )
    sensor = ClaudeAccountLimitSensor(coordinator, limit)
    assert sensor.native_value == 88

    coordinator.data = AccountLimitsResult(
        mode="subscription", fetched_at=None, limits=(), report={}
    )
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


@pytest.mark.parametrize(
    "percent",
    [
        pytest.param(float("inf"), id="infinite"),
        pytest.param(float("nan"), id="not-a-number"),
        pytest.param(True, id="boolean"),
        pytest.param("88", id="text"),
        pytest.param(None, id="null"),
        pytest.param(150, id="above-the-scale"),
        pytest.param(-1, id="below-the-scale"),
    ],
)
def test_parser_drops_every_untrustworthy_percentage(percent: Any) -> None:
    """Only a plain number inside the scale is read as a percentage.

    Tested at the parser, where the rule lives: an infinite or out-of-scale
    figure passed on as a reading would state how much of the account's
    allowance is gone, which is the one thing that must never be invented. The
    HTTP layer cannot reach every one of these shapes, and the rule is not the
    HTTP layer's.
    """
    assert _parse_limits([{**SESSION_LIMIT, "percent": percent}]) == ()


def test_parser_reads_a_well_formed_limit() -> None:
    """The control for the case above: a plain number is read, with its fields."""
    (limit,) = _parse_limits([SESSION_LIMIT])

    assert limit.percent == 14
    assert limit.kind == "session"
    assert limit.severity == "normal"
    assert limit.model is None
    assert limit.resets_at is not None


async def test_a_model_respelled_upstream_stays_one_sensor(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A model whose capitalisation changes upstream keeps its one sensor.

    The entity id is built from a slug, so "Fable" and "FABLE" are one id. If the
    sensors were tracked by the raw name instead, the second spelling would add a
    second entity claiming an id that is already taken: Home Assistant refuses it
    with a duplicate-unique-id error and the first sensor, still looking for a
    name the account no longer sends, would be unavailable for good.
    """
    _serve(aioclient_mock, _limits(SCOPED_LIMIT))
    await setup_integration(hass, mock_config_entry)
    entity_id = _entity_id(hass, mock_config_entry, "weekly_limit_fable")
    assert entity_id is not None
    caplog.clear()

    _serve(aioclient_mock, _limits({**SCOPED_LIMIT, "model": "FABLE", "percent": 91}))
    await _poll_again(hass, freezer)

    registered = [
        e
        for e in er.async_get(hass).entities.values()
        if (e.unique_id or "").endswith("_weekly_limit_fable")
    ]
    assert len(registered) == 1
    assert registered[0].entity_id == entity_id
    state = hass.states.get(entity_id)
    assert state.state == "91"
    assert state.attributes["model"] == "FABLE"
    assert _loud(caplog) == []

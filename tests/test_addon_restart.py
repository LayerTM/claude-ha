"""An add-on restart is a normal event: quiet unavailability, not an error."""

from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta
import logging
from unittest.mock import MagicMock, patch

from aiohttp import ClientConnectionError
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.const import (
    ADDON_OUTAGE_GRACE,
    ADDON_RESTART_RETRY,
    DOMAIN,
    ISSUE_ADDON_NOT_RUNNING,
    SCAN_INTERVAL,
    USAGE_SCAN_INTERVAL,
)
from homeassistant.components.hassio import AddonError, AddonState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from .conftest import (
    STATUS_PAYLOAD,
    TEST_BASE_URL,
    USAGE_PAYLOAD,
    make_addon_info,
    setup_integration,
)

STATUS_URL = f"{TEST_BASE_URL}/api/status"
USAGE_URL = f"{TEST_BASE_URL}/api/usage"
PACKAGE_LOGGER = "custom_components.claude_ha"


@pytest.fixture(autouse=True)
def on_supervisor() -> Generator[None]:
    """Run every test here on a Supervisor install."""
    with patch("custom_components.claude_ha.is_hassio", return_value=True):
        yield


def _serve(aioclient_mock: AiohttpClientMocker, *, up: bool) -> None:
    """Answer the add-on endpoints, or refuse them as a restarting add-on does."""
    aioclient_mock.clear_requests()
    if up:
        aioclient_mock.get(STATUS_URL, json=STATUS_PAYLOAD)
        aioclient_mock.get(USAGE_URL, json=USAGE_PAYLOAD)
    else:
        aioclient_mock.get(STATUS_URL, exc=ClientConnectionError("Connection refused"))
        aioclient_mock.get(USAGE_URL, exc=ClientConnectionError("Connection refused"))


async def _advance(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    """Move past a poll due in ``seconds``.

    A coordinator schedules its next poll at the current whole second plus a
    random fraction plus the interval, so it can land up to a second after
    ``now + seconds``; the extra second makes the poll certain to have run.
    """
    freezer.tick(timedelta(seconds=seconds + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _status_state(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_status"
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


def _records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    """Return this integration's log records at exactly ``level``."""
    return [
        r
        for r in caplog.records
        if r.name.startswith(PACKAGE_LOGGER) and r.levelno == level
    ]


def _loud(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return this integration's records at WARNING or above."""
    return [
        r
        for r in caplog.records
        if r.name.startswith(PACKAGE_LOGGER) and r.levelno >= logging.WARNING
    ]


@pytest.mark.parametrize(
    "supervisor_state", [AddonState.NOT_RUNNING, AddonState.RUNNING]
)
async def test_restart_window_is_quiet(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    mock_addon_manager: MagicMock,
    caplog: pytest.LogCaptureFixture,
    supervisor_state: AddonState,
) -> None:
    """Refused polls while the add-on restarts log one INFO and no error.

    The Supervisor reports ``started`` while the add-on is still shutting down
    and ``startup``/``stopped`` while it comes back; both are a restart.
    """
    _serve(aioclient_mock, up=True)
    await setup_integration(hass, mock_config_entry)
    assert _status_state(hass, mock_config_entry) != STATE_UNAVAILABLE
    caplog.clear()
    caplog.set_level(logging.INFO, logger=PACKAGE_LOGGER)

    mock_addon_manager.async_get_addon_info.return_value = make_addon_info(
        supervisor_state
    )
    _serve(aioclient_mock, up=False)
    await _advance(hass, freezer, SCAN_INTERVAL)
    assert _status_state(hass, mock_config_entry) == STATE_UNAVAILABLE
    # A second refused poll inside the window stays quiet too.
    await _advance(hass, freezer, ADDON_RESTART_RETRY)
    assert _status_state(hass, mock_config_entry) == STATE_UNAVAILABLE

    mock_addon_manager.async_get_addon_info.return_value = make_addon_info()
    _serve(aioclient_mock, up=True)
    # While restarting the add-on is re-polled every ADDON_RESTART_RETRY seconds,
    # so the entities come back well inside one SCAN_INTERVAL.
    await _advance(hass, freezer, ADDON_RESTART_RETRY)
    assert _status_state(hass, mock_config_entry) != STATE_UNAVAILABLE

    assert _loud(caplog) == []
    infos = [r for r in _records(caplog, logging.INFO) if "add-on" in r.getMessage()]
    assert len(infos) == 1
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ADDON_NOT_RUNNING) is None


async def test_usage_poll_in_restart_window_is_quiet(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    mock_addon_manager: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The slow usage poll landing in the window is quiet as well."""
    _serve(aioclient_mock, up=True)
    await setup_integration(hass, mock_config_entry)
    await _advance(hass, freezer, USAGE_SCAN_INTERVAL - 5)
    caplog.clear()

    mock_addon_manager.async_get_addon_info.return_value = make_addon_info(
        AddonState.NOT_RUNNING
    )
    _serve(aioclient_mock, up=False)
    await _advance(hass, freezer, 5)

    assert _loud(caplog) == []
    usage = mock_config_entry.runtime_data.usage
    assert usage.last_update_success is False


async def test_outage_warns_once_and_raises_repair(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    mock_addon_manager: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stopped add-on past the grace period: one WARNING and a repair."""
    _serve(aioclient_mock, up=True)
    await setup_integration(hass, mock_config_entry)
    caplog.clear()

    mock_addon_manager.async_get_addon_info.return_value = make_addon_info(
        AddonState.NOT_RUNNING
    )
    _serve(aioclient_mock, up=False)
    await _advance(hass, freezer, SCAN_INTERVAL)
    await _advance(hass, freezer, ADDON_OUTAGE_GRACE.total_seconds())
    # Polling goes on at the normal interval; it must not repeat the warning.
    for _ in range(3):
        await _advance(hass, freezer, SCAN_INTERVAL)

    warnings = _loud(caplog)
    assert [r.levelno for r in warnings] == [logging.WARNING]
    assert "not been running" in warnings[0].getMessage()
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, ISSUE_ADDON_NOT_RUNNING) is not None

    mock_addon_manager.async_get_addon_info.return_value = make_addon_info()
    _serve(aioclient_mock, up=True)
    await _advance(hass, freezer, SCAN_INTERVAL)
    assert _status_state(hass, mock_config_entry) != STATE_UNAVAILABLE
    assert registry.async_get_issue(DOMAIN, ISSUE_ADDON_NOT_RUNNING) is None


async def test_running_but_unreachable_outage_warns_without_repair(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    mock_addon_manager: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Running per the Supervisor yet silent: a WARNING, no 'start it' repair."""
    _serve(aioclient_mock, up=True)
    await setup_integration(hass, mock_config_entry)
    caplog.clear()

    _serve(aioclient_mock, up=False)
    await _advance(hass, freezer, SCAN_INTERVAL)
    await _advance(hass, freezer, ADDON_OUTAGE_GRACE.total_seconds())

    warnings = _loud(caplog)
    assert [r.levelno for r in warnings] == [logging.WARNING]
    assert "running but its API" in warnings[0].getMessage()
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ADDON_NOT_RUNNING) is None


@pytest.mark.parametrize(
    "supervisor",
    [
        pytest.param({"side_effect": AddonError("boom")}, id="supervisor-error"),
        pytest.param(
            {"return_value": make_addon_info(AddonState.NOT_INSTALLED)},
            id="not-installed",
        ),
    ],
)
async def test_unvouched_failure_stays_an_error(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    mock_addon_manager: MagicMock,
    caplog: pytest.LogCaptureFixture,
    supervisor: dict[str, object],
) -> None:
    """If the Supervisor cannot vouch for the add-on, the failure is an error."""
    _serve(aioclient_mock, up=True)
    await setup_integration(hass, mock_config_entry)
    caplog.clear()

    mock_addon_manager.async_get_addon_info.reset_mock(return_value=True)
    for attr, value in supervisor.items():
        setattr(mock_addon_manager.async_get_addon_info, attr, value)
    _serve(aioclient_mock, up=False)
    await _advance(hass, freezer, SCAN_INTERVAL)

    errors = _records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert "Error fetching claude_ha_status data" in errors[0].getMessage()


async def test_without_supervisor_failure_stays_an_error(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With no Supervisor to ask, a refused poll is reported as before."""
    _serve(aioclient_mock, up=True)
    with patch("custom_components.claude_ha.is_hassio", return_value=False):
        await setup_integration(hass, mock_config_entry)
    caplog.clear()

    _serve(aioclient_mock, up=False)
    await _advance(hass, freezer, SCAN_INTERVAL)

    assert len(_records(caplog, logging.ERROR)) == 1


async def test_http_error_is_not_a_restart(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    mock_addon_manager: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An add-on that answers with an error is failing, not restarting."""
    _serve(aioclient_mock, up=True)
    await setup_integration(hass, mock_config_entry)
    caplog.clear()

    aioclient_mock.clear_requests()
    aioclient_mock.get(STATUS_URL, status=500)
    await _advance(hass, freezer, SCAN_INTERVAL)

    assert len(_records(caplog, logging.ERROR)) == 1
    mock_addon_manager.async_get_addon_info.assert_awaited_once()  # setup only

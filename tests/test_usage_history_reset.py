"""Tests for the usage-history-reset repair notice."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry, flush_store
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.const import DOMAIN, ISSUE_USAGE_HISTORY_RESET
from custom_components.claude_ha.issues import entry_issue_id
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .conftest import (
    ACCOUNT_LIMITS_PAYLOAD,
    STATUS_PAYLOAD,
    TEST_BASE_URL,
    USAGE_PAYLOAD,
    setup_integration,
)


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, entry_issue_id(ISSUE_USAGE_HISTORY_RESET, entry.entry_id)
    )


async def _poll_usage(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    payload: dict,
) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=payload)
    await entry.runtime_data.usage.async_refresh()
    await hass.async_block_till_done()


async def _simulate_ha_restart(hass: HomeAssistant) -> None:
    """Round-trip the issue registry through storage, like a real HA restart.

    A plain entry reload never touches the registry's store, so it cannot
    tell a persistent issue's stored ``data`` apart from a non-persistent
    one's ``data=None`` on reload — only a real save/load does.
    """
    registry = ir.async_get(hass)
    await flush_store(registry._store)
    ir.async_get.cache_clear()
    del hass.data[ir.DATA_REGISTRY]
    await ir.async_load(hass)


async def test_no_notification_while_history_reset_is_false(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """A normal usage report (no reset) raises no repair."""
    await setup_integration(hass, mock_config_entry)

    assert _issue(hass, mock_config_entry) is None


async def test_no_notification_when_reset_but_no_day_is_known_yet(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    mock_status: None,
) -> None:
    """A reset with no ``history_since`` yet (nothing counted at all) stays quiet."""
    await setup_integration(hass, mock_config_entry)
    payload = {**USAGE_PAYLOAD, "history_reset": True, "history_since": None}
    await _poll_usage(hass, mock_config_entry, aioclient_mock, payload)

    assert _issue(hass, mock_config_entry) is None


async def test_reset_notifies_once_and_stays_quiet_on_repeat_polls(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    mock_status: None,
) -> None:
    """A reset raises exactly one repair; two more polls of the same reset add none."""
    await setup_integration(hass, mock_config_entry)
    reset_payload = {
        **USAGE_PAYLOAD,
        "history_reset": True,
        "history_since": "2026-09-01",
    }
    await _poll_usage(hass, mock_config_entry, aioclient_mock, reset_payload)

    issue = _issue(hass, mock_config_entry)
    assert issue is not None
    assert issue.translation_placeholders == {"history_since": "2026-09-01"}
    created = issue.created

    for _ in range(2):
        await _poll_usage(hass, mock_config_entry, aioclient_mock, reset_payload)

    issue = _issue(hass, mock_config_entry)
    assert issue is not None
    assert issue.created == created  # never deleted and recreated


async def test_a_new_history_since_notifies_again(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    mock_status: None,
) -> None:
    """A later, different reset re-surfaces the repair even if it was dismissed."""
    await setup_integration(hass, mock_config_entry)
    first_payload = {
        **USAGE_PAYLOAD,
        "history_reset": True,
        "history_since": "2026-09-01",
    }
    await _poll_usage(hass, mock_config_entry, aioclient_mock, first_payload)

    registry = ir.async_get(hass)
    issue_id = entry_issue_id(ISSUE_USAGE_HISTORY_RESET, mock_config_entry.entry_id)
    registry.async_ignore(DOMAIN, issue_id, True)
    assert registry.async_get_issue(DOMAIN, issue_id).dismissed_version is not None

    second_payload = {
        **USAGE_PAYLOAD,
        "history_reset": True,
        "history_since": "2026-09-10",
    }
    await _poll_usage(hass, mock_config_entry, aioclient_mock, second_payload)

    issue = _issue(hass, mock_config_entry)
    assert issue is not None
    assert issue.dismissed_version is None  # recreated, so visible again
    assert issue.translation_placeholders == {"history_since": "2026-09-10"}


async def test_reset_notice_survives_an_entry_reload(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    mock_status: None,
) -> None:
    """Unloading and reloading the entry keeps the already-raised repair.

    The issue registry itself stays live in memory across an entry reload
    (it never round-trips through storage), so this does not exercise the
    persistence path — see ``test_reset_notice_survives_a_real_restart``.
    """
    await setup_integration(hass, mock_config_entry)
    reset_payload = {
        **USAGE_PAYLOAD,
        "history_reset": True,
        "history_since": "2026-09-01",
    }
    await _poll_usage(hass, mock_config_entry, aioclient_mock, reset_payload)
    assert _issue(hass, mock_config_entry) is not None

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", json=STATUS_PAYLOAD)
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=reset_payload)
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/account_limits", json=ACCOUNT_LIMITS_PAYLOAD
    )
    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED

    assert _issue(hass, mock_config_entry) is not None


async def test_reset_notice_survives_a_real_restart(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    mock_status: None,
) -> None:
    """A real restart (registry reloaded from storage) does not re-notify.

    ``history_reset`` stays true forever, so if the dedup key vanished on
    restart the very next poll would treat an ordinary restart as a brand
    new loss and recreate the issue. It must not.
    """
    await setup_integration(hass, mock_config_entry)
    reset_payload = {
        **USAGE_PAYLOAD,
        "history_reset": True,
        "history_since": "2026-09-01",
    }
    await _poll_usage(hass, mock_config_entry, aioclient_mock, reset_payload)
    issue = _issue(hass, mock_config_entry)
    assert issue is not None
    created = issue.created

    await _simulate_ha_restart(hass)
    restored = _issue(hass, mock_config_entry)
    assert restored is not None
    assert restored.data == {
        "issue": ISSUE_USAGE_HISTORY_RESET,
        "entry_id": mock_config_entry.entry_id,
        "history_since": "2026-09-01",
    }

    await _poll_usage(hass, mock_config_entry, aioclient_mock, reset_payload)

    issue = _issue(hass, mock_config_entry)
    assert issue is not None
    assert issue.created == created  # not recreated by the same, old reset

"""Tests for the engine of an entry: storage, migration, checks, request fields."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.api import (
    ClaudeClient,
    ClaudeEngineMismatchError,
    StatusResult,
)
from custom_components.claude_ha.const import (
    CONF_ADDON_SLUG,
    CONF_ENGINE,
    CONF_HOST,
    CONF_PORT,
    CONF_TOKEN,
    DOMAIN,
    ISSUE_ENGINE_MISMATCH,
)
from custom_components.claude_ha.engines import CLAUDE, engine_for_slug
from custom_components.claude_ha.entity import build_device_info
from custom_components.claude_ha.issues import entry_issue_id
from homeassistant.config_entries import SOURCE_HASSIO, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .conftest import (
    ACCOUNT_LIMITS_PAYLOAD,
    LEGACY_STATUS_PAYLOAD,
    PROMPT_PAYLOAD,
    STATUS_PAYLOAD,
    TEST_BASE_URL,
    TEST_HOST,
    TEST_PORT,
    TEST_SLUG,
    TEST_TOKEN,
    USAGE_PAYLOAD,
    setup_integration,
)

_GOLDEN = Path(__file__).parent / "fixtures" / "registry_1_10_0.json"
_STATUS_URL = f"{TEST_BASE_URL}/api/status"
_PROMPT_URL = f"{TEST_BASE_URL}/api/prompt"
_LEGACY_DATA = {
    CONF_HOST: TEST_HOST,
    CONF_PORT: TEST_PORT,
    CONF_TOKEN: TEST_TOKEN,
    CONF_ADDON_SLUG: TEST_SLUG,
}


def _mock_addon(aioclient_mock: AiohttpClientMocker, status: dict[str, Any]) -> None:
    aioclient_mock.get(_STATUS_URL, json=status)
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/account_limits", json=ACCOUNT_LIMITS_PAYLOAD
    )


def _registry(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    """Dump what an entry left in the registries, in the golden's shape."""
    eid = entry.entry_id
    entities = sorted(
        [e.unique_id.removeprefix(eid), e.entity_id, e.original_name, e.translation_key]
        for e in er.async_entries_for_config_entry(er.async_get(hass), eid)
    )
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), eid)
    return {
        "entities": entities,
        "device": [device.name, device.manufacturer, device.model, device.sw_version],
        "title": entry.title,
    }


def _mismatch_issue(
    hass: HomeAssistant, entry: MockConfigEntry
) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, entry_issue_id(ISSUE_ENGINE_MISMATCH, entry.entry_id)
    )


def _client(hass: HomeAssistant) -> ClaudeClient:
    return ClaudeClient(
        async_get_clientsession(hass), TEST_BASE_URL, TEST_TOKEN, engine=CLAUDE
    )


@pytest.mark.parametrize("status", [LEGACY_STATUS_PAYLOAD, STATUS_PAYLOAD])
async def test_entry_from_1_10_is_migrated_and_unchanged(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: dict[str, Any],
) -> None:
    """An entry made before the engine field loads as the same entities/device."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Claude Code",
        unique_id=TEST_SLUG,
        version=1,
        minor_version=1,
        data=dict(_LEGACY_DATA),
    )
    _mock_addon(aioclient_mock, {**LEGACY_STATUS_PAYLOAD, **status})

    await setup_integration(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.minor_version == 2
    assert dict(entry.data) == {**_LEGACY_DATA, CONF_ENGINE: "claude"}
    golden = json.loads(_GOLDEN.read_text())
    assert _registry(hass, entry) == {
        key: golden[key] for key in ("entities", "device", "title")
    }
    assert _mismatch_issue(hass, entry) is None


async def test_newer_minor_version_is_left_alone(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """A minor version newer than this code's loads without being rewritten."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_SLUG,
        version=1,
        minor_version=3,
        data=dict(mock_config_entry.data),
    )

    await setup_integration(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.minor_version == 3


async def test_unknown_engine_is_refused(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """An entry for an engine this version does not know fails to set up."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_SLUG,
        version=1,
        minor_version=2,
        data={**mock_config_entry.data, CONF_ENGINE: "future"},
    )

    await setup_integration(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == "unsupported_engine"


async def test_engine_mismatch_raises_then_clears_repair(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A different reported engine fails the poll and raises a repair until fixed."""
    _mock_addon(aioclient_mock, {**STATUS_PAYLOAD, "engine": "other"})

    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    issue = _mismatch_issue(hass, mock_config_entry)
    assert issue is not None
    assert issue.translation_key == ISSUE_ENGINE_MISMATCH
    assert issue.translation_placeholders == {"engine": "other"}
    assert issue.severity is ir.IssueSeverity.ERROR

    aioclient_mock.clear_requests()
    _mock_addon(aioclient_mock, STATUS_PAYLOAD)
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert _mismatch_issue(hass, mock_config_entry) is None


async def test_engine_mismatch_on_a_loaded_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A loaded entry whose add-on changes engine goes unavailable with a repair."""
    _mock_addon(aioclient_mock, STATUS_PAYLOAD)
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data.status

    aioclient_mock.clear_requests()
    _mock_addon(aioclient_mock, {**STATUS_PAYLOAD, "engine": "other"})
    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert _mismatch_issue(hass, mock_config_entry) is not None
    assert hass.states.get("sensor.claude_code_status").state == "unavailable"

    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert _mismatch_issue(hass, mock_config_entry) is None


async def test_removing_an_entry_removes_its_mismatch_repair(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A mismatch raised during a setup retry goes away with the entry."""
    _mock_addon(aioclient_mock, {**STATUS_PAYLOAD, "engine": "other"})
    await setup_integration(hass, mock_config_entry)
    assert _mismatch_issue(hass, mock_config_entry) is not None

    await hass.config_entries.async_remove(mock_config_entry.entry_id)

    assert _mismatch_issue(hass, mock_config_entry) is None


async def test_discovery_of_an_addon_with_another_engine_aborts(
    hass: HomeAssistant,
    mock_addon_manager: MagicMock,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Setup never creates an entry whose add-on reports a different engine."""
    _mock_addon(aioclient_mock, {**STATUS_PAYLOAD, "engine": "other"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=HassioServiceInfo(
            config={CONF_HOST: TEST_HOST, CONF_PORT: TEST_PORT, CONF_TOKEN: TEST_TOKEN},
            name="Claude Code",
            slug=TEST_SLUG,
            uuid="1234",
        ),
    )
    assert result["description_placeholders"] == {"addon": "Claude Code"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "engine_mismatch"
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_status_sensor_shows_the_engine(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The status sensor shows the engine and its version."""
    _mock_addon(aioclient_mock, {**STATUS_PAYLOAD, "engine_version": "9.9.9"})
    await setup_integration(hass, mock_config_entry)

    attributes = hass.states.get("sensor.claude_code_status").attributes
    assert attributes["engine"] == "claude"
    assert attributes["engine_version"] == "9.9.9"


@pytest.mark.parametrize(
    ("engine_version", "claude_version", "sw_version"),
    [("9.9.9", "2.0.1", "9.9.9"), (None, "2.0.1", "2.0.1"), (None, None, None)],
)
def test_device_version_prefers_the_engine_version(
    mock_config_entry: MockConfigEntry,
    engine_version: str | None,
    claude_version: str | None,
    sw_version: str | None,
) -> None:
    """The device shows the engine's version, else the Claude version of old add-ons."""
    status = StatusResult(
        ready=True,
        version="1.0.0",
        claude_version=claude_version,
        model=None,
        ha_mcp=None,
        ha_mcp_connected=None,
        engine_version=engine_version,
    )

    info = build_device_info(mock_config_entry, status)

    assert info["sw_version"] == sw_version
    assert info["name"] == "Claude Code"
    assert info["manufacturer"] == "Anthropic"
    assert info["model"] == "Claude Code add-on"


async def test_client_rejects_another_engine(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The client never returns the status of an add-on with another engine."""
    aioclient_mock.get(_STATUS_URL, json={**STATUS_PAYLOAD, "engine": "other"})

    with pytest.raises(ClaudeEngineMismatchError) as err:
        await _client(hass).async_get_status()

    assert err.value.reported == "other"
    assert err.value.translation_key == "engine_mismatch"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (LEGACY_STATUS_PAYLOAD, None),
        ({**LEGACY_STATUS_PAYLOAD, "request_fields": []}, frozenset()),
        (
            {**LEGACY_STATUS_PAYLOAD, "request_fields": ["prompt", "surface"]},
            frozenset({"prompt", "surface"}),
        ),
        ({**LEGACY_STATUS_PAYLOAD, "request_fields": "surface"}, frozenset()),
        ({**LEGACY_STATUS_PAYLOAD, "request_fields": ["surface", 3]}, frozenset()),
        ({**LEGACY_STATUS_PAYLOAD, "request_fields": None}, frozenset()),
    ],
)
async def test_request_fields_parsing(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: dict[str, Any],
    expected: frozenset[str] | None,
) -> None:
    """Absent means "predates the list"; anything unreadable accepts nothing."""
    aioclient_mock.get(_STATUS_URL, json=status)

    result = await _client(hass).async_get_status()

    assert result.request_fields == expected
    assert result.engine == "claude"


_OPTIONAL = {
    "conversation_id": "c1",
    "language": "uk",
    "surface": "voice",
    "image_entity": "camera.door",
}


@pytest.mark.parametrize(
    ("status", "sent"),
    [
        # A new add-on: exactly the fields it lists, whatever its version.
        (
            {**STATUS_PAYLOAD, "version": "0.0.1"},
            {"conversation_id", "language", "surface", "image_entity"},
        ),
        (
            {**STATUS_PAYLOAD, "request_fields": ["prompt", "mode", "surface"]},
            {"surface"},
        ),
        ({**STATUS_PAYLOAD, "request_fields": []}, set()),
        # An add-on that predates the list: today's version rule.
        (
            {**LEGACY_STATUS_PAYLOAD, "version": "1.27.9"},
            {"conversation_id", "language", "image_entity"},
        ),
        (
            {**LEGACY_STATUS_PAYLOAD, "version": "1.28.0"},
            {"conversation_id", "language", "surface", "image_entity"},
        ),
    ],
)
async def test_read_sends_only_accepted_fields(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: dict[str, Any],
    sent: set[str],
) -> None:
    """A read carries each optional field only when the add-on accepts it."""
    aioclient_mock.get(_STATUS_URL, json=status)
    aioclient_mock.post(_PROMPT_URL, json=PROMPT_PAYLOAD)
    client = _client(hass)
    client.note_status(await client.async_get_status())

    await client.async_prompt("hi", **_OPTIONAL)

    body = aioclient_mock.mock_calls[-1][2]
    assert set(body) == {"prompt", "mode"} | sent


@pytest.mark.parametrize(
    ("request_fields", "sent"),
    [
        (None, {"intents", "confirmation"}),
        (["prompt", "mode", "intents", "confirmation"], {"intents", "confirmation"}),
        (["prompt", "mode"], set()),
    ],
)
async def test_write_sends_only_accepted_fields(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    request_fields: list[str] | None,
    sent: set[str],
) -> None:
    """A write's intents and confirmation follow the same rule."""
    status = dict(LEGACY_STATUS_PAYLOAD)
    if request_fields is not None:
        status["request_fields"] = request_fields
    aioclient_mock.get(_STATUS_URL, json=status)
    aioclient_mock.post(_PROMPT_URL, json=PROMPT_PAYLOAD)
    client = _client(hass)
    client.note_status(await client.async_get_status())

    await client.async_prompt(
        "do it", mode="write", intents=[{"a": 1}], confirmation="confirmed"
    )

    body = aioclient_mock.mock_calls[-1][2]
    assert set(body) == {"prompt", "mode"} | sent


@pytest.mark.parametrize(
    ("slug", "engine"),
    [
        (TEST_SLUG, CLAUDE),
        ("local_claude-code", CLAUDE),
        ("abcd1234_other-agent", None),
        ("claude-code", None),
    ],
)
def test_engine_for_slug(slug: str, engine: object) -> None:
    """A slug belongs to the engine whose suffix it ends with."""
    assert engine_for_slug(slug) is engine

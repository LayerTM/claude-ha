"""Tests for the health-check button."""

from __future__ import annotations

import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.const import CONF_ADDON_SLUG, CONF_ENGINE, DOMAIN
from custom_components.claude_ha.engines import CLAUDE, CODEX, Engine
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import (
    ACCOUNT_LIMITS_PAYLOAD,
    STATUS_PAYLOAD,
    TEST_BASE_URL,
    USAGE_PAYLOAD,
    setup_integration,
)


def _button(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_check_health"
    )
    assert entity_id is not None
    return entity_id


async def test_press_probes_and_refreshes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    mock_prompt: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Pressing fires a probe read and re-polls status."""
    await setup_integration(hass, mock_config_entry)
    aioclient_mock.mock_calls.clear()

    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: _button(hass, mock_config_entry)},
        blocking=True,
    )

    methods = [(c[0], c[1].path) for c in aioclient_mock.mock_calls]
    assert ("POST", "/api/prompt") in methods  # the probe read
    assert ("GET", "/api/status") in methods  # the re-poll


@pytest.mark.parametrize("existing_entity_id", [None, "button.renamed_by_user"])
@pytest.mark.parametrize("engine", [CLAUDE, CODEX], ids=lambda e: e.key)
async def test_name_follows_the_entry_engine(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    engine: Engine,
    existing_entity_id: str | None,
) -> None:
    """The button names its engine, and an already registered entity id is kept."""
    slug = f"local{engine.slug_suffix}"
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=engine.addon_name,
        unique_id=slug,
        version=1,
        minor_version=2,
        data={**mock_config_entry.data, CONF_ADDON_SLUG: slug, CONF_ENGINE: engine.key},
    )
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status", json={**STATUS_PAYLOAD, "engine": engine.key}
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/account_limits", json=ACCOUNT_LIMITS_PAYLOAD
    )
    if existing_entity_id is not None:
        entry.add_to_hass(hass)
        er.async_get(hass).async_get_or_create(
            "button",
            DOMAIN,
            f"{entry.entry_id}_check_health",
            config_entry=entry,
            object_id_base=existing_entity_id.partition(".")[2],
            original_name="Check Claude health",
        )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    else:
        await setup_integration(hass, entry)

    entity_id = _button(hass, entry)
    new_ids = {
        "claude": "button.claude_code_check_claude_health",
        "codex": "button.codex_check_codex_health",
    }
    assert entity_id == (existing_entity_id or new_ids[engine.key])
    registered = er.async_get(hass).async_get(entity_id)
    assert registered is not None
    assert registered.original_name == f"Check {engine.name} health"
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.name == f"{engine.addon_name} Check {engine.name} health"

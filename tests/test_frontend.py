"""Tests for serving the bundled Lovelace card and loading it on dashboards."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.claude_ha.frontend import (
    CARD_URL,
    async_ensure_card_resource,
    async_register_card,
)
from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from homeassistant.setup import async_setup_component

from .conftest import setup_integration


async def _version(hass: HomeAssistant) -> str:
    integration = await async_get_integration(hass, "claude_ha")
    return str(integration.version)


def _storage(hass: HomeAssistant) -> ResourceStorageCollection:
    resources = hass.data[LOVELACE_DATA].resources
    assert isinstance(resources, ResourceStorageCollection)
    return resources


def _card_resources(hass: HomeAssistant) -> list[dict[str, Any]]:
    return [
        item
        for item in hass.data[LOVELACE_DATA].resources.async_items()
        if item["url"].split("?")[0] == CARD_URL
    ]


def _extra_modules(hass: HomeAssistant) -> set[str]:
    return set(hass.data[DATA_EXTRA_MODULE_URL].urls)


async def test_card_is_served(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """The chat card is served from the integration over HTTP."""
    await setup_integration(hass, mock_config_entry)

    client = await hass_client()
    resp = await client.get(CARD_URL)
    assert resp.status == 200
    body = await resp.text()
    assert "claude-chat-card" in body


async def test_card_is_a_lovelace_resource(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """In storage mode the card is one module resource, never an extra module."""
    await setup_integration(hass, mock_config_entry)

    assert _card_resources(hass) == [
        {
            "id": _card_resources(hass)[0]["id"],
            "type": "module",
            "url": f"{CARD_URL}?v={await _version(hass)}",
        }
    ]
    assert CARD_URL not in _extra_modules(hass)


async def test_card_resource_is_updated_and_deduplicated(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """An old or hand-added card resource is updated in place; copies go away."""
    assert await async_setup_component(hass, "lovelace", {})
    resources = _storage(hass)
    await resources.async_get_info()
    await resources.async_create_item({"res_type": "js", "url": f"{CARD_URL}?v=0.1"})
    await resources.async_create_item({"res_type": "module", "url": CARD_URL})
    other = await resources.async_create_item(
        {"res_type": "module", "url": "/local/other-card.js"}
    )

    await setup_integration(hass, mock_config_entry)

    cards = _card_resources(hass)
    assert len(cards) == 1
    assert cards[0]["type"] == "module"
    assert cards[0]["url"] == f"{CARD_URL}?v={await _version(hass)}"
    assert other in resources.async_items()


async def test_current_card_resource_is_left_alone(hass: HomeAssistant) -> None:
    """A resource that is already right is not rewritten."""
    assert await async_setup_component(hass, "lovelace", {})
    resources = _storage(hass)
    await resources.async_get_info()
    item = await resources.async_create_item(
        {"res_type": "module", "url": f"{CARD_URL}?v={await _version(hass)}"}
    )
    listener_calls: list[Any] = []
    resources.async_add_listener(lambda *args: listener_calls.append(args))

    await async_ensure_card_resource(hass)

    assert _card_resources(hass) == [item]
    assert listener_calls == []


async def test_removing_the_last_entry_removes_the_resource(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """The card resource stays while an entry exists and goes with the last one."""
    other = MockConfigEntry(domain="claude_ha", unique_id="other_claude-code")
    other.add_to_hass(hass)
    await setup_integration(hass, mock_config_entry)
    assert len(_card_resources(hass)) == 1

    assert await hass.config_entries.async_remove(other.entry_id)
    await hass.async_block_till_done()
    assert len(_card_resources(hass)) == 1

    assert await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert _card_resources(hass) == []


@pytest.mark.parametrize(
    ("yaml_resources", "loaded_as_extra_module"),
    [
        ([], True),
        ([{"type": "module", "url": f"{CARD_URL}?v=1"}], False),
    ],
)
async def test_yaml_resources(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    yaml_resources: list[dict[str, str]],
    loaded_as_extra_module: bool,
) -> None:
    """With YAML resources nothing is written; the card loads either way."""
    assert await async_setup_component(
        hass,
        "lovelace",
        {"lovelace": {"resource_mode": "yaml", "resources": yaml_resources}},
    )

    await setup_integration(hass, mock_config_entry)
    await async_ensure_card_resource(hass)

    assert hass.data[LOVELACE_DATA].resources.async_items() == yaml_resources
    assert (CARD_URL in _extra_modules(hass)) is loaded_as_extra_module

    assert await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.data[LOVELACE_DATA].resources.async_items() == yaml_resources


async def test_card_registration_is_idempotent(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """Registering the card again is a safe no-op."""
    await setup_integration(hass, mock_config_entry)
    # A second call must not re-register the static path (which would raise).
    await async_register_card(hass)

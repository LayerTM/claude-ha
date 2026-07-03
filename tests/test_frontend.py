"""Tests for the bundled Lovelace card registration."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.claude_ha.frontend import CARD_URL, async_register_card
from homeassistant.core import HomeAssistant

from .conftest import setup_integration


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


async def test_card_registration_is_idempotent(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """Registering the card again is a safe no-op."""
    await setup_integration(hass, mock_config_entry)
    # A second call must not re-register the static path (which would raise).
    await async_register_card(hass)

"""Register the bundled Lovelace chat card as a frontend resource.

The card ships with the integration and is served from it directly (rather than
as a separate HACS "plugin"), so no second HACS category is needed.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

CARD_FILENAME = "claude-chat-card.js"
CARD_URL = f"/{DOMAIN}/{CARD_FILENAME}"
_REGISTERED = f"{DOMAIN}_frontend_registered"


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the chat card and load it in the frontend (once)."""
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True

    card_path = Path(__file__).parent / "www" / CARD_FILENAME
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(card_path), cache_headers=False)]
    )
    frontend.add_extra_js_url(hass, CARD_URL)

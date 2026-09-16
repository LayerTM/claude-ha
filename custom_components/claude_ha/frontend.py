"""Serve the bundled Lovelace chat card and make dashboards load it.

The card ships with the integration and is served from it directly (rather than
as a separate HACS "plugin"), so no second HACS category is needed.

It is loaded as a Lovelace resource. The frontend loads resources after its own
app bundle, and that bundle installs the custom-element registry every card must
be defined in; a module loaded outside that order (an "extra module") can run
first, define the card in a registry the frontend no longer reads, and leave
every dashboard with "Custom element doesn't exist". Resources live in Lovelace's
storage, which the integration can only write in storage mode; with YAML-managed
resources the user lists the card there, and until they do it is still loaded
as an extra module so the card keeps working.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import LOVELACE_DATA, MODE_STORAGE
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

CARD_FILENAME = "claude-chat-card.js"
CARD_URL = f"/{DOMAIN}/{CARD_FILENAME}"
RESOURCE_TYPE = "module"
_REGISTERED = f"{DOMAIN}_frontend_registered"


def _is_card(url: str) -> bool:
    """Whether a resource URL points at the bundled card, whatever its query."""
    return urlsplit(url).path == CARD_URL


def _storage_resources(hass: HomeAssistant) -> ResourceStorageCollection | None:
    """Return Lovelace's resource storage, or ``None`` when YAML manages it."""
    data = hass.data[LOVELACE_DATA]
    if data.resource_mode != MODE_STORAGE:
        return None
    assert isinstance(data.resources, ResourceStorageCollection)
    return data.resources


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the chat card (once); with YAML resources, load it as a fallback."""
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True

    card_path = Path(__file__).parent / "www" / CARD_FILENAME
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(card_path), cache_headers=False)]
    )
    if _storage_resources(hass) is not None:
        return
    listed = hass.data[LOVELACE_DATA].resources.async_items()
    if not any(_is_card(item["url"]) for item in listed):
        frontend.add_extra_js_url(hass, CARD_URL)


async def async_ensure_card_resource(hass: HomeAssistant) -> None:
    """Keep exactly one Lovelace resource for the card, at this version.

    The version in the URL makes browsers fetch the card again after an update.
    """
    if (resources := _storage_resources(hass)) is None:
        return
    integration = await async_get_integration(hass, DOMAIN)
    url = f"{CARD_URL}?v={integration.version}"
    await resources.async_get_info()  # loads the collection
    cards = [item for item in resources.async_items() if _is_card(item["url"])]
    if not cards:
        await resources.async_create_item({"res_type": RESOURCE_TYPE, "url": url})
        return
    first, *duplicates = cards
    if first["url"] != url or first["type"] != RESOURCE_TYPE:
        await resources.async_update_item(
            first["id"], {"res_type": RESOURCE_TYPE, "url": url}
        )
    for item in duplicates:
        await resources.async_delete_item(item["id"])


async def async_remove_card_resource(hass: HomeAssistant) -> None:
    """Remove the card's Lovelace resources (the integration is going away)."""
    if (resources := _storage_resources(hass)) is None:
        return
    await resources.async_get_info()  # loads the collection
    for item in list(resources.async_items()):
        if _is_card(item["url"]):
            await resources.async_delete_item(item["id"])

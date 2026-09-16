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

import asyncio
from contextlib import suppress
from pathlib import Path
import re
from typing import Any

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import LOVELACE_DATA, MODE_STORAGE
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.core import HomeAssistant
from homeassistant.helpers.collection import ItemNotFound
from homeassistant.loader import async_get_integration

from .const import DOMAIN

CARD_FILENAME = "claude-chat-card.js"
CARD_URL = f"/{DOMAIN}/{CARD_FILENAME}"
RESOURCE_TYPE = "module"
# How often a vanished resource is re-read before the error is let through.
_RECONCILE_ATTEMPTS = 3
_REGISTERED = f"{DOMAIN}_frontend_registered"
_RESOURCE_LOCK = f"{DOMAIN}_resource_lock"
# Only the URLs this integration writes (or documents) are ours: the local
# endpoint, bare or with a version query. Anything else in the user's resources
# (another host, another query, an unparsable string) belongs to someone else.
_OUR_URL = re.compile(rf"{re.escape(CARD_URL)}(\?v=[0-9A-Za-z.+-]+)?")


def _is_card(item: dict[str, Any]) -> bool:
    """Whether a resource item is the bundled card as this integration adds it."""
    url = item.get("url")
    return isinstance(url, str) and _OUR_URL.fullmatch(url) is not None


def _lock(hass: HomeAssistant) -> asyncio.Lock:
    """Return the one lock every read-then-write of the card resource holds."""
    lock: asyncio.Lock = hass.data.setdefault(_RESOURCE_LOCK, asyncio.Lock())
    return lock


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
    if not any(_is_card(item) for item in listed):
        frontend.add_extra_js_url(hass, CARD_URL)


async def async_ensure_card_resource(hass: HomeAssistant) -> None:
    """Keep exactly one Lovelace resource for the card, at this version.

    The version in the URL makes browsers fetch the card again after an update.
    """
    if (resources := _storage_resources(hass)) is None:
        return
    integration = await async_get_integration(hass, DOMAIN)
    url = f"{CARD_URL}?v={integration.version}"
    async with _lock(hass):
        await resources.async_get_info()  # loads the collection
        # The user can still delete an item between our read and our write; a
        # vanished item just means reading the resources again.
        for attempt in range(_RECONCILE_ATTEMPTS):
            try:
                await _async_reconcile(resources, url)
            except ItemNotFound:
                if attempt == _RECONCILE_ATTEMPTS - 1:
                    raise
            else:
                return


async def _async_reconcile(resources: ResourceStorageCollection, url: str) -> None:
    """Create, update or de-duplicate the card resource from one snapshot."""
    cards = [item for item in resources.async_items() if _is_card(item)]
    if not cards:
        await resources.async_create_item({"res_type": RESOURCE_TYPE, "url": url})
        return
    first, *duplicates = cards
    if first["url"] != url or first["type"] != RESOURCE_TYPE:
        await resources.async_update_item(
            first["id"], {"res_type": RESOURCE_TYPE, "url": url}
        )
    for item in duplicates:
        await _async_delete(resources, item["id"])


async def _async_delete(resources: ResourceStorageCollection, item_id: str) -> None:
    """Delete one resource; one that is already gone needs nothing."""
    with suppress(ItemNotFound):
        await resources.async_delete_item(item_id)


async def async_remove_card_resource(
    hass: HomeAssistant, removed_entry_id: str
) -> None:
    """Remove the card's resources when the last entry is being removed."""
    if (resources := _storage_resources(hass)) is None:
        return
    async with _lock(hass):
        remaining = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.entry_id != removed_entry_id
        ]
        if remaining:
            return
        await resources.async_get_info()  # loads the collection
        for item in [item for item in resources.async_items() if _is_card(item)]:
            await _async_delete(resources, item["id"])

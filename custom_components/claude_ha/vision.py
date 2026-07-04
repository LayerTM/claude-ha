"""Resolve which camera (if any) a chat message wants Claude to look at.

Vision is opt-in and privacy-bounded: a camera is only ever attached when the
message is clearly visual AND exactly one camera can be resolved AND that camera
is exposed to Assist — the same ceiling as everything else Claude can see. The
integration passes only the entity_id; the add-on fetches and downscales the
snapshot itself.
"""

from __future__ import annotations

from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)

from .const import ASSIST_ASSISTANT

# Message must clearly ask to look before a snapshot is ever attached (cost,
# latency, privacy). Kept small and multilingual; matched as lowercase substrings.
_VISUAL_CUES = (
    "camera",
    "see ",
    "look",
    "watch",
    "snapshot",
    "who is at",
    "who's at",
    "whos at",
    "what's at",
    "whats at",
    "at the door",
    "at the front door",
    "камер",
    "подивись",
    "подивися",
    "хто там",
    "хто біля",
    "біля дверей",
    "на порозі",
)


@callback
def _is_visual(text: str) -> bool:
    """Return True if the message clearly asks to look at something."""
    lowered = text.lower()
    return any(cue in lowered for cue in _VISUAL_CUES)


@callback
def _camera_names(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Return lowercase names that identify a camera: its own name, area, floor."""
    names: list[str] = []
    if (state := hass.states.get(entity_id)) is not None:
        names.append(state.name.lower())
    entry = er.async_get(hass).async_get(entity_id)
    area_id = entry.area_id if entry is not None else None
    if entry is not None and area_id is None and entry.device_id is not None:
        # Fall back to the device's area when the entity has no explicit one.
        device = dr.async_get(hass).async_get(entry.device_id)
        area_id = device.area_id if device is not None else None
    if area_id is not None and (area := ar.async_get(hass).async_get_area(area_id)):
        names.append(area.name.lower())
        if area.floor_id is not None and (
            floor := fr.async_get(hass).async_get_floor(area.floor_id)
        ):
            names.append(floor.name.lower())
    return [name for name in names if name]


@callback
def resolve_camera(hass: HomeAssistant, text: str) -> str | None:
    """Return the one Assist-exposed camera the message refers to, else None.

    None means "don't attach an image": the message isn't visual, no camera is
    exposed, or the choice is ambiguous (never guess between cameras).
    """
    if not _is_visual(text):
        return None
    exposed = [
        entity_id
        for entity_id in hass.states.async_entity_ids("camera")
        if async_should_expose(hass, ASSIST_ASSISTANT, entity_id)
    ]
    if not exposed:
        return None

    lowered = text.lower()
    named = [
        entity_id
        for entity_id in exposed
        if any(name in lowered for name in _camera_names(hass, entity_id))
    ]
    if len(named) == 1:
        return named[0]
    if not named and len(exposed) == 1:
        return exposed[0]
    return None

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


# A physical camera (UniFi Protect etc.) exposes several channels, each labelled
# "<location> High/Medium/Low Resolution Channel". Users say the location, so we also
# match the label with this channel suffix stripped ("… High …Channel" → "front yard").
_CHANNEL_SUFFIXES = (
    "high resolution channel",
    "medium resolution channel",
    "low resolution channel",
    "resolution channel",
    "channel",
)


@callback
def _strip_channel_suffix(name: str) -> str:
    """Drop a trailing camera-channel suffix so a location label matches a mention."""
    for suffix in _CHANNEL_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)].strip()
    return name


@callback
def _camera_names(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Return the lowercase labels a user might call a camera by.

    Covers what people actually say: the entity's friendly name and registered
    Assist aliases, its device name, and its area/floor — because a user names a
    camera by its location ("Front yard") far more than by its model ("G4 Instant")
    or entity_id. Assist exposure is enforced by the caller, not here.
    """
    names: list[str] = []
    if (state := hass.states.get(entity_id)) is not None:
        names.append(state.name.lower())

    entry = er.async_get(hass).async_get(entity_id)
    device = None
    if entry is not None:
        # aliases may hold a COMPUTED_NAME sentinel (the friendly name, already
        # captured via state.name); keep only the user's explicit string aliases.
        names.extend(alias.lower() for alias in entry.aliases if isinstance(alias, str))
        if entry.device_id is not None:
            device = dr.async_get(hass).async_get(entry.device_id)
    if device is not None:
        names.append((device.name_by_user or device.name or "").lower())

    # Prefer the entity's own area; fall back to the device's when it has none.
    area_id = entry.area_id if entry is not None else None
    if area_id is None and device is not None:
        area_id = device.area_id
    if area_id is not None and (area := ar.async_get(hass).async_get_area(area_id)):
        names.append(area.name.lower())
        if area.floor_id is not None and (
            floor := fr.async_get(hass).async_get_floor(area.floor_id)
        ):
            names.append(floor.name.lower())

    # Also match each label with a camera-channel suffix stripped, so "Front yard"
    # resolves a "Front Yard High Resolution Channel" entity (I6).
    resolved: list[str] = []
    for name in names:
        if not name:
            continue
        resolved.append(name)
        stripped = _strip_channel_suffix(name)
        if stripped and stripped != name:
            resolved.append(stripped)
    return list(dict.fromkeys(resolved))


@callback
def _prefer_high_res(hass: HomeAssistant, entity_ids: list[str]) -> str:
    """Pick the high-resolution channel among channels of one camera (best source)."""
    for entity_id in sorted(entity_ids):
        state = hass.states.get(entity_id)
        if state is not None and "high" in state.name.lower():
            return entity_id
    return sorted(entity_ids)[0]


@callback
def _dedupe_channels(hass: HomeAssistant, entity_ids: list[str]) -> list[str]:
    """Collapse channels of the SAME physical camera to one (they share a device).

    Two exposed channels of one camera are not a real ambiguity — they show the
    same view — so they should resolve, not trip the never-guess guard.
    """
    registry = er.async_get(hass)
    by_device: dict[str, list[str]] = {}
    result: list[str] = []
    for entity_id in entity_ids:
        entry = registry.async_get(entity_id)
        device_id = entry.device_id if entry is not None else None
        if device_id is None:
            result.append(entity_id)
        else:
            by_device.setdefault(device_id, []).append(entity_id)
    result.extend(_prefer_high_res(hass, group) for group in by_device.values())
    return result


@callback
def resolve_camera(hass: HomeAssistant, text: str) -> str | None:
    """Return the one Assist-exposed camera the message refers to, else None.

    None means "don't attach an image": the message isn't visual, no camera is
    exposed, or the choice is ambiguous (never guess between distinct cameras).
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
    if named:
        # Several matches that are all channels of ONE camera resolve to one; matches
        # across distinct cameras stay ambiguous (never guess).
        collapsed = _dedupe_channels(hass, named)
        return collapsed[0] if len(collapsed) == 1 else None
    if len(exposed) == 1:
        return exposed[0]
    return None

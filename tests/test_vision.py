"""Tests for camera-vision resolution (Design 8)."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha import vision
from custom_components.claude_ha.api import ClaudeClient
from custom_components.claude_ha.const import CONF_CAMERA_VISION, DOMAIN
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .conftest import TEST_BASE_URL, TEST_TOKEN, setup_integration

_URL = f"{TEST_BASE_URL}/api/prompt"


@pytest.fixture
def expose_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat every camera as exposed to Assist."""
    monkeypatch.setattr(vision, "async_should_expose", lambda *_a: True)


def test_not_visual_returns_none(hass: HomeAssistant, expose_all: None) -> None:
    """A non-visual message never attaches a camera."""
    hass.states.async_set("camera.front", "idle")
    assert vision.resolve_camera(hass, "what's the temperature?") is None


def test_no_exposed_camera(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A visual message with no exposed camera attaches nothing."""
    monkeypatch.setattr(vision, "async_should_expose", lambda *_a: False)
    hass.states.async_set("camera.front", "idle")
    assert vision.resolve_camera(hass, "look at the camera") is None


def test_single_exposed_camera(hass: HomeAssistant, expose_all: None) -> None:
    """A visual message with exactly one exposed camera picks it."""
    hass.states.async_set("camera.front", "idle")
    assert vision.resolve_camera(hass, "who's at the door?") == "camera.front"


def test_name_match_among_many(hass: HomeAssistant, expose_all: None) -> None:
    """With several cameras, the one named in the message wins."""
    hass.states.async_set("camera.a", "idle", {"friendly_name": "Front Door"})
    hass.states.async_set("camera.b", "idle", {"friendly_name": "Garage"})
    assert vision.resolve_camera(hass, "show the front door camera") == "camera.a"


def test_ambiguous_returns_none(hass: HomeAssistant, expose_all: None) -> None:
    """With several cameras and no name match, never guess."""
    hass.states.async_set("camera.a", "idle", {"friendly_name": "Alpha"})
    hass.states.async_set("camera.b", "idle", {"friendly_name": "Beta"})
    assert vision.resolve_camera(hass, "look at the camera") is None


def test_area_and_floor_match(hass: HomeAssistant, expose_all: None) -> None:
    """A camera resolves by its area or floor name."""
    floor = fr.async_get(hass).async_create("Ground Floor")
    area = ar.async_get(hass).async_create("Backyard")
    ar.async_get(hass).async_update(area.id, floor_id=floor.floor_id)
    entry = er.async_get(hass).async_get_or_create("camera", "test", "back")
    er.async_get(hass).async_update_entity(entry.entity_id, area_id=area.id)
    hass.states.async_set(entry.entity_id, "idle")
    hass.states.async_set("camera.other", "idle")

    assert vision.resolve_camera(hass, "look at the ground floor") == entry.entity_id


async def test_device_area_fallback(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, expose_all: None
) -> None:
    """A camera with no direct area resolves via its device's area."""
    mock_config_entry.add_to_hass(hass)
    area = ar.async_get(hass).async_create("Hallway")
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, "cam-device")},
    )
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    entry = er.async_get(hass).async_get_or_create(
        "camera", "test", "hall", device_id=device.id
    )
    hass.states.async_set(entry.entity_id, "idle")
    hass.states.async_set("camera.other", "idle")

    assert vision.resolve_camera(hass, "look in the hallway") == entry.entity_id


async def test_async_prompt_sends_image_entity(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The client attaches image_entity on a read request."""
    aioclient_mock.post(
        _URL, json={"text": "x", "proposal": None, "tools_used": [], "truncated": False}
    )
    client = ClaudeClient(async_get_clientsession(hass), TEST_BASE_URL, TEST_TOKEN)
    await client.async_prompt("who's there?", image_entity="camera.front")
    assert aioclient_mock.mock_calls[0][2]["image_entity"] == "camera.front"


async def test_conversation_attaches_camera_when_enabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With vision enabled, a visual message sends the exposed camera."""
    monkeypatch.setattr(vision, "async_should_expose", lambda *_a: True)
    hass.states.async_set("camera.front", "idle")
    aioclient_mock.post(
        _URL,
        json={
            "text": "A person.",
            "proposal": None,
            "tools_used": [],
            "truncated": False,
        },
    )
    await setup_integration(hass, mock_config_entry)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_CAMERA_VISION: True}
    )

    entity_id = er.async_get(hass).async_get_entity_id(
        "conversation", DOMAIN, mock_config_entry.entry_id
    )
    assert entity_id is not None
    await conversation.async_converse(
        hass, "who's at the door?", None, Context(user_id="u"), agent_id=entity_id
    )

    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert posts[-1][2]["image_entity"] == "camera.front"


async def test_conversation_no_camera_when_disabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With vision off (default), no snapshot is ever sent."""
    monkeypatch.setattr(vision, "async_should_expose", lambda *_a: True)
    hass.states.async_set("camera.front", "idle")
    aioclient_mock.post(
        _URL,
        json={"text": "hi", "proposal": None, "tools_used": [], "truncated": False},
    )
    await setup_integration(hass, mock_config_entry)

    entity_id = er.async_get(hass).async_get_entity_id(
        "conversation", DOMAIN, mock_config_entry.entry_id
    )
    assert entity_id is not None
    await conversation.async_converse(
        hass, "who's at the door?", None, Context(user_id="u"), agent_id=entity_id
    )

    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert "image_entity" not in posts[-1][2]


# --- _is_visual cue matrix (D2 triage: exhaustive cue guard + negatives) -------


@pytest.mark.parametrize("cue", list(vision._VISUAL_CUES))
def test_is_visual_true_for_every_declared_cue(cue: str) -> None:
    """Every declared cue substring marks a message visual.

    Tripwire: if a cue is accidentally removed/renamed, its case here breaks.
    """
    assert vision._is_visual(cue) is True


@pytest.mark.parametrize(
    "text",
    [
        "what's the temperature?",
        "turn on the kitchen light",
        "set a timer for ten minutes",
        "яка зараз погода?",
        "увімкни світло на кухні",
    ],
)
def test_is_visual_false_for_non_visual(text: str) -> None:
    """Plain state/act questions are never visual (no snapshot risk)."""
    assert vision._is_visual(text) is False


def test_is_visual_case_insensitive() -> None:
    """Cues match regardless of case (input is lowercased)."""
    assert vision._is_visual("LOOK at the CAMERA") is True
    assert vision._is_visual("Подивись, хто там?") is True


# --- Ukrainian cue set, end-to-end ---------------------------------------------


def test_ukrainian_cue_resolves_single_camera(
    hass: HomeAssistant, expose_all: None
) -> None:
    """A Ukrainian visual message resolves the one exposed camera."""
    hass.states.async_set("camera.hall", "idle", {"friendly_name": "Прихожа"})
    assert vision.resolve_camera(hass, "подивись, хто там?") == "camera.hall"


def test_ukrainian_named_among_live_like_set(
    hass: HomeAssistant, expose_all: None
) -> None:
    """UK cue + a name that matches exactly one of the live-like 3-camera set."""
    hass.states.async_set("camera.front", "idle", {"friendly_name": "Front"})
    hass.states.async_set("camera.back", "idle", {"friendly_name": "Back"})
    hass.states.async_set("camera.gazebo", "idle", {"friendly_name": "gazebo"})
    assert (
        vision.resolve_camera(hass, "подивись на камеру front, що там?")
        == "camera.front"
    )


# --- The D2 zero-`img=` branches (why a snapshot may never be attached) --------


def test_name_without_cue_declines(hass: HomeAssistant, expose_all: None) -> None:
    """A bare camera name with NO visual cue is not visual → declines.

    This is add-on's top hypothesis for the 2nd live question: the message was
    just the camera's name, so `_is_visual` was False and nothing was attached.
    """
    hass.states.async_set("camera.a", "idle", {"friendly_name": "Front Yard"})
    hass.states.async_set("camera.b", "idle", {"friendly_name": "Garage"})
    assert vision._is_visual("front yard") is False
    assert vision.resolve_camera(hass, "front yard") is None


def test_recalled_en_bare_name_declines(hass: HomeAssistant, expose_all: None) -> None:
    """Recalled EN case 'Front yard (G4 Instant)' — a bare name, no cue → None."""
    hass.states.async_set("camera.fy", "idle", {"friendly_name": "Front yard"})
    hass.states.async_set("camera.other", "idle", {"friendly_name": "Back"})
    assert vision._is_visual("Front yard (G4 Instant)") is False
    assert vision.resolve_camera(hass, "Front yard (G4 Instant)") is None


def test_recalled_uk_phrasing_single_camera_resolves(
    hass: HomeAssistant, expose_all: None
) -> None:
    """Recalled UK phrasing is visual via 'камер'; one camera → lenient resolve."""
    hass.states.async_set("camera.entry", "idle", {"friendly_name": "Вхід"})
    text = "камера біля входу, що там видно?"
    assert vision._is_visual(text) is True
    assert vision.resolve_camera(hass, text) == "camera.entry"


def test_recalled_uk_phrasing_ambiguous_with_three_cameras(
    hass: HomeAssistant, expose_all: None
) -> None:
    """Visual cue present, but ≥2 cameras and no name match → None (never guess).

    A concrete path to zero-`img=` even with vision ON and a valid cue: the
    live set had 3 cameras and the message named none of them exactly.
    """
    for eid, name in (
        ("camera.front", "Front"),
        ("camera.back", "Back"),
        ("camera.gazebo", "gazebo"),
    ):
        hass.states.async_set(eid, "idle", {"friendly_name": name})
    text = "камера біля входу, що там видно?"
    assert vision._is_visual(text) is True
    assert vision.resolve_camera(hass, text) is None


def test_two_name_matches_returns_none(hass: HomeAssistant, expose_all: None) -> None:
    """A cue plus names that match TWO cameras → None (never guess between them)."""
    hass.states.async_set("camera.a", "idle", {"friendly_name": "Front"})
    hass.states.async_set("camera.b", "idle", {"friendly_name": "Yard"})
    assert vision.resolve_camera(hass, "show the front yard camera") is None


# --- Cue-precision notes (documented current behavior; NOT a unilateral change) -
# Candidate gaps and over-matches surfaced for add-on-002 §3. We only widen/narrow
# the cue list once the live result shows a genuine cue-miss — until then these
# lock in current behavior so any future change is a conscious, reviewed flip.


@pytest.mark.parametrize(
    "text",
    [
        "покажи що на кухні",  # 'покажи' / 'що видно' are not cues
        "show me the driveway",  # 'show me' is not a cue ('show' alone isn't visual)
        "глянь на подвір'я",  # 'глянь' is not a cue
    ],
)
def test_known_cue_gaps_currently_not_visual(text: str) -> None:
    """Real phrasings a user might say that are NOT yet cues → currently False."""
    assert vision._is_visual(text) is False


def test_cue_substring_overmatch_is_known(
    hass: HomeAssistant, expose_all: None
) -> None:
    """'look' matches as a substring, so 'look up …' is currently visual.

    With a single exposed camera the lenient path WOULD attach a snapshot to a
    non-visual question — a known false-positive tradeoff of substring matching.
    """
    hass.states.async_set("camera.hall", "idle")
    assert vision._is_visual("look up tomorrow's forecast") is True
    assert vision.resolve_camera(hass, "look up tomorrow's forecast") == "camera.hall"


# --- D2 live fix: match the user's LOCATION word (device name + aliases) --------
# Live ground truth (shared/D2-live-result.md): «Подивись на камеру Front yard…» with
# G4 exposed as location "Front yard" among 3 cameras still returned None, because the
# resolver read friendly-name + area + floor but NOT the device name or entity aliases
# — the two labels a user most often speaks. These reproduce that, then must pass.


def _make_unifi_camera(
    hass: HomeAssistant,
    entry_id: str,
    unique: str,
    *,
    device_name: str | None = None,
    friendly: str,
    aliases: set[str] | None = None,
    area_name: str | None = None,
) -> str:
    """Register a camera the way UniFi Protect does and return its entity_id."""
    ent = er.async_get(hass)
    device_id = None
    if device_name is not None:
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry_id,
            identifiers={(DOMAIN, unique)},
            name=device_name,
        )
        device_id = device.id
    reg = ent.async_get_or_create("camera", "unifi", unique, device_id=device_id)
    updates: dict[str, object] = {}
    if aliases is not None:
        updates["aliases"] = aliases
    if area_name is not None:
        area = ar.async_get(hass).async_create(area_name)
        updates["area_id"] = area.id
    if updates:
        ent.async_update_entity(reg.entity_id, **updates)
    hass.states.async_set(reg.entity_id, "idle", {"friendly_name": friendly})
    return reg.entity_id


def test_resolves_by_device_name_location(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, expose_all: None
) -> None:
    """LIVE D2 repro — user names the camera by its device/location 'Front yard'."""
    mock_config_entry.add_to_hass(hass)
    eid = mock_config_entry.entry_id
    g4 = _make_unifi_camera(
        hass, eid, "g4", device_name="Front yard", friendly="G4 Instant"
    )
    _make_unifi_camera(
        hass, eid, "g6", device_name="Backyard gazebo", friendly="G6 Instant"
    )
    _make_unifi_camera(hass, eid, "ptz", device_name="Back yard", friendly="G6 PTZ")

    # Mirrors live Q2 (shared/D2-live-result.md): cue + names the camera by location.
    assert (
        vision.resolve_camera(hass, "Подивись на камеру Front yard, що там видно?")
        == g4
    )


def test_resolves_by_entity_alias(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, expose_all: None
) -> None:
    """A user's registered Assist alias ('Front yard') resolves the camera."""
    mock_config_entry.add_to_hass(hass)
    eid = mock_config_entry.entry_id
    g4 = _make_unifi_camera(
        hass, eid, "g4", friendly="G4 Instant", aliases={"Front yard"}
    )
    _make_unifi_camera(hass, eid, "other", friendly="Back yard")

    assert vision.resolve_camera(hass, "подивись на камеру Front yard") == g4


def test_resolves_by_area_name_already(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, expose_all: None
) -> None:
    """Sanity: an assigned AREA 'Front yard' already resolved pre-fix.

    Confirms the live None was a device-name/alias gap, not an area-matching bug.
    """
    mock_config_entry.add_to_hass(hass)
    eid = mock_config_entry.entry_id
    g4 = _make_unifi_camera(
        hass, eid, "g4", friendly="G4 Instant", area_name="Front yard"
    )
    _make_unifi_camera(hass, eid, "other", friendly="Back yard")

    assert vision.resolve_camera(hass, "подивись на камеру Front yard") == g4


# --- rec#3 + I6: multi-channel cameras and channel-suffix name matching ---------
# A physical UniFi camera exposes 2 channels (high+medium) in the same area, each
# named "<location> …Resolution Channel". Exposing both used to trip the ≥2-ambiguity
# guard → silent decline. rec#3 collapses same-device channels; I6 matches the label
# with the channel suffix stripped.


def _make_channels(
    hass: HomeAssistant,
    entry_id: str,
    unique: str,
    device_name: str,
    friendlies: list[str],
) -> list[str]:
    """Create N channels of ONE physical camera (one device) and return entity ids."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry_id,
        identifiers={(DOMAIN, unique)},
        name=device_name,
    )
    ent = er.async_get(hass)
    ids: list[str] = []
    for index, friendly in enumerate(friendlies):
        reg = ent.async_get_or_create(
            "camera", "unifi", f"{unique}_{index}", device_id=device.id
        )
        hass.states.async_set(reg.entity_id, "idle", {"friendly_name": friendly})
        ids.append(reg.entity_id)
    return ids


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("front yard high resolution channel", "front yard"),
        ("front yard medium resolution channel", "front yard"),
        ("front yard low resolution channel", "front yard"),
        ("garage resolution channel", "garage"),
        ("hallway channel", "hallway"),
        ("kitchen", "kitchen"),
    ],
)
def test_strip_channel_suffix(raw: str, expected: str) -> None:
    """A trailing camera-channel suffix is dropped; plain names are untouched."""
    assert vision._strip_channel_suffix(raw) == expected


def test_channel_suffix_stripped_for_matching(
    hass: HomeAssistant, expose_all: None
) -> None:
    """I6: a resolution-channel friendly name still matches the plain location."""
    hass.states.async_set(
        "camera.g4", "idle", {"friendly_name": "Front Yard High Resolution Channel"}
    )
    hass.states.async_set("camera.back", "idle", {"friendly_name": "Back Yard"})
    assert vision.resolve_camera(hass, "подивись на камеру front yard") == "camera.g4"


def test_multichannel_same_device_resolves_to_high(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, expose_all: None
) -> None:
    """rec#3: two exposed channels of ONE camera resolve (to the high-res one)."""
    mock_config_entry.add_to_hass(hass)
    eid = mock_config_entry.entry_id
    high, _med = _make_channels(
        hass,
        eid,
        "g4",
        "Front Yard",
        ["Front Yard High Resolution Channel", "Front Yard Medium Resolution Channel"],
    )
    _make_unifi_camera(hass, eid, "back", friendly="Back Yard")

    assert vision.resolve_camera(hass, "подивись на камеру front yard") == high


def test_multichannel_no_high_label_falls_back_deterministically(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, expose_all: None
) -> None:
    """rec#3: same-device channels with no 'high' label → deterministic single pick."""
    mock_config_entry.add_to_hass(hass)
    eid = mock_config_entry.entry_id
    ids = _make_channels(
        hass, eid, "g4", "Front Yard", ["Front Yard Stream A", "Front Yard Stream B"]
    )
    result = vision.resolve_camera(hass, "подивись на камеру front yard")
    assert result == sorted(ids)[0]


def test_multichannel_distinct_cameras_stay_ambiguous(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, expose_all: None
) -> None:
    """Matches across DISTINCT physical cameras stay ambiguous → None."""
    mock_config_entry.add_to_hass(hass)
    eid = mock_config_entry.entry_id
    _make_unifi_camera(hass, eid, "a", device_name="Front Yard", friendly="Cam A")
    _make_unifi_camera(hass, eid, "b", device_name="Front Gate", friendly="Cam B")

    assert (
        vision.resolve_camera(hass, "подивись на камеру front yard front gate") is None
    )


def test_prefer_high_res_skips_entities_without_state(hass: HomeAssistant) -> None:
    """The high-res pick tolerates a channel id with no state, then falls back."""
    hass.states.async_set("camera.b", "idle", {"friendly_name": "B"})
    assert vision._prefer_high_res(hass, ["camera.b", "camera.a"]) == "camera.a"


# --- D5: separator-insensitive matching + parenthetical location -----------------
# Live repro (shared/D5-camera-name-match-separator.md): the user named the AREA
# "Frontyard" (no space) and exposed BOTH G4 channels. The old raw-substring matcher
# had "frontyard" ∉ "front yard", so named=[] and (2 exposed) vision silently declined.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Front Yard", "frontyard"),
        ("front-yard", "frontyard"),
        ("Front_Yard", "frontyard"),
        ("Frontyard", "frontyard"),
        ("G4 Instant (Front Yard)", "g4instantfrontyard"),
        ("!!!", ""),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    """Separators, punctuation and case are dropped for matching."""
    assert vision._normalize(raw) == expected


def _two_channels(
    hass: HomeAssistant,
    entry_id: str,
    friendlies: list[str],
    *,
    area_name: str | None = None,
) -> list[str]:
    """Two exposed channels of one physical camera; return entity ids."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry_id, identifiers={(DOMAIN, "g4")}, name="G4 Instant"
    )
    ent = er.async_get(hass)
    area_id = ar.async_get(hass).async_create(area_name).id if area_name else None
    ids: list[str] = []
    for index, friendly in enumerate(friendlies):
        reg = ent.async_get_or_create(
            "camera", "unifi", f"g4_{index}", device_id=device.id
        )
        if area_id is not None:
            ent.async_update_entity(reg.entity_id, area_id=area_id)
        hass.states.async_set(reg.entity_id, "idle", {"friendly_name": friendly})
        ids.append(reg.entity_id)
    return ids


def test_d5_area_without_space_resolves(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, expose_all: None
) -> None:
    """LIVE D5: area 'Frontyard' (no space) + 2 channels + spoken 'front yard'."""
    mock_config_entry.add_to_hass(hass)
    high, _med = _two_channels(
        hass,
        mock_config_entry.entry_id,
        ["G4 Instant (High)", "G4 Instant (Medium)"],
        area_name="Frontyard",  # the user named it WITHOUT a space
    )
    result = vision.resolve_camera(hass, "подивись на камеру front yard, що видно?")
    assert result == high


def test_d5_parenthetical_location_resolves(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, expose_all: None
) -> None:
    """A long friendly name embedding '(Front Yard)' resolves via the parenthetical."""
    mock_config_entry.add_to_hass(hass)
    high, _med = _two_channels(
        hass,
        mock_config_entry.entry_id,
        [
            "G4 Instant (Front Yard) High Resolution Channel",
            "G4 Instant (Front Yard) Medium Resolution Channel",
        ],
    )
    assert vision.resolve_camera(hass, "подивись на камеру front yard") == high


def test_d5_hyphen_and_case_variants(hass: HomeAssistant, expose_all: None) -> None:
    """Hyphen and case differences still match (separator-insensitive)."""
    hass.states.async_set("camera.a", "idle", {"friendly_name": "Front-Yard"})
    hass.states.async_set("camera.b", "idle", {"friendly_name": "Garage"})
    assert vision.resolve_camera(hass, "подивись на камеру FRONT YARD") == "camera.a"


def test_name_matcher_skips_empty_normalized_candidate(
    hass: HomeAssistant, expose_all: None
) -> None:
    """A punctuation-only label normalizes to '' and must not match everything."""
    hass.states.async_set("camera.a", "idle", {"friendly_name": "!!!"})
    hass.states.async_set("camera.b", "idle", {"friendly_name": "Garage"})
    assert vision.resolve_camera(hass, "подивись на камеру front yard") is None

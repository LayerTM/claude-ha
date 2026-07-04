"""Tests for the deterministic risk classifier."""

from __future__ import annotations

import pytest

from custom_components.claude_ha.risk import is_auto_ok, is_critical
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er


def _register(
    hass: HomeAssistant,
    entity_id: str,
    *,
    device_class: str | None = None,
    entity_category: EntityCategory | None = None,
    platform: str = "test",
) -> str:
    domain, obj = entity_id.split(".", 1)
    entry = er.async_get(hass).async_get_or_create(
        domain,
        platform,
        obj,
        suggested_object_id=obj,
        original_device_class=device_class,
        entity_category=entity_category,
    )
    return entry.entity_id


@pytest.mark.parametrize(
    "entity_id",
    [
        "lock.front",
        "alarm_control_panel.house",
        "update.firmware",
        "valve.main",
        "siren.alarm",
        "lawn_mower.mower",
        "water_heater.tank",
    ],
)
async def test_critical_domains(hass: HomeAssistant, entity_id: str) -> None:
    """Inherently critical domains always confirm."""
    assert is_critical(hass, entity_id, "HassTurnOn")


@pytest.mark.parametrize("entity_id", ["light.kitchen", "switch.fan", "fan.bedroom"])
async def test_benign_domains(hass: HomeAssistant, entity_id: str) -> None:
    """Ordinary domains are benign."""
    assert not is_critical(hass, entity_id, "HassTurnOn")


async def test_cover_garage_is_critical(hass: HomeAssistant) -> None:
    """A garage cover is critical; a shade is not."""
    garage = _register(hass, "cover.garage", device_class="garage")
    shade = _register(hass, "cover.blind", device_class="shade")
    assert is_critical(hass, garage, "HassTurnOn")
    assert not is_critical(hass, shade, "HassTurnOn")


async def test_button_restart_is_critical(hass: HomeAssistant) -> None:
    """A restart button is critical; a plain button is not."""
    restart = _register(hass, "button.box", device_class="restart")
    plain = _register(hass, "button.doorbell")
    assert is_critical(hass, restart, "HassPress")
    assert not is_critical(hass, plain, "HassPress")


async def test_config_category_is_critical(hass: HomeAssistant) -> None:
    """Config-category entities (router/AP/PoE/firmware) always confirm."""
    poe = _register(hass, "switch.poe_port", entity_category=EntityCategory.CONFIG)
    assert is_critical(hass, poe, "HassTurnOff")


async def test_network_platform_is_critical(hass: HomeAssistant) -> None:
    """Network-infrastructure integrations always confirm."""
    ap = _register(hass, "switch.ap_led", platform="unifi")
    assert is_critical(hass, ap, "HassTurnOff")


@pytest.mark.parametrize(
    "verb", ["HassUnlock", "alarm_disarm", "update_install", "reboot_now", "restart"]
)
async def test_critical_verbs(hass: HomeAssistant, verb: str) -> None:
    """State-critical verbs confirm regardless of the entity."""
    assert is_critical(hass, "light.x", verb)


async def test_user_critical_entities(hass: HomeAssistant) -> None:
    """A user-pinned entity always confirms."""
    assert is_critical(
        hass, "light.special", "HassTurnOn", critical_entities=["light.special"]
    )
    assert not is_critical(hass, "light.special", "HassTurnOn")


async def test_is_auto_ok_true(hass: HomeAssistant) -> None:
    """Low-risk benign intents are auto-ok."""
    intents = [{"intent": "HassTurnOn", "targets": ["light.k"], "risk": "low"}]
    assert is_auto_ok(hass, intents)


@pytest.mark.parametrize(
    "intents",
    [
        [],
        [{"intent": "HassTurnOn", "targets": ["light.k"], "risk": "sensitive"}],
        [{"intent": "HassTurnOn", "targets": ["light.k"]}],
        [{"intent": "HassTurnOff", "targets": ["lock.front"], "risk": "low"}],
    ],
    ids=["empty", "not-low", "missing-risk", "critical-target"],
)
async def test_is_auto_ok_false(hass: HomeAssistant, intents: list[dict]) -> None:
    """Auto is refused unless every intent is low-risk and no target is critical."""
    assert not is_auto_ok(hass, intents)


async def test_is_auto_ok_respects_user_critical(hass: HomeAssistant) -> None:
    """A user-pinned target blocks auto even for a low-risk benign action."""
    intents = [{"intent": "HassTurnOn", "targets": ["light.k"], "risk": "low"}]
    assert not is_auto_ok(hass, intents, critical_entities=["light.k"])


async def test_cover_unknown_device_class_is_critical(hass: HomeAssistant) -> None:
    """A cover whose device class cannot be resolved is critical (fail-closed)."""
    assert is_critical(hass, "cover.mystery", "HassTurnOn")


async def test_cover_garage_via_state_is_critical(hass: HomeAssistant) -> None:
    """A registry-less garage cover is caught via its live state device_class."""
    hass.states.async_set("cover.diy_garage", "closed", {"device_class": "garage"})
    hass.states.async_set("cover.diy_blind", "open", {"device_class": "shade"})
    assert is_critical(hass, "cover.diy_garage", "HassTurnOn")
    assert not is_critical(hass, "cover.diy_blind", "HassTurnOn")


async def test_button_restart_via_state_is_critical(hass: HomeAssistant) -> None:
    """A registry-less restart button is caught via its live state device_class."""
    hass.states.async_set("button.router", "unknown", {"device_class": "restart"})
    assert is_critical(hass, "button.router", "HassPress")


async def test_state_without_device_class_leaves_cover_unresolved(
    hass: HomeAssistant,
) -> None:
    """A cover state carrying no device_class stays unresolved and confirms."""
    hass.states.async_set("cover.generic", "closed", {})
    assert is_critical(hass, "cover.generic", "HassTurnOn")


@pytest.mark.parametrize(
    "entity_id", ["script.unlock_all", "scene.movie", "automation.away"]
)
async def test_effect_wrappers_are_critical(
    hass: HomeAssistant, entity_id: str
) -> None:
    """Opaque-effect wrappers always confirm — their effect can't be inspected."""
    assert is_critical(hass, entity_id, "HassTurnOn")


async def test_malformed_target_is_critical(hass: HomeAssistant) -> None:
    """A target that is not a dotted entity_id cannot be bounded, so it confirms."""
    assert is_critical(hass, "kitchen", "HassTurnOn")
    assert is_critical(hass, "", "HassTurnOn")


async def test_is_auto_ok_empty_targets(hass: HomeAssistant) -> None:
    """An intent with no targets is unbounded and never auto-executes."""
    intents = [{"intent": "HassTurnOff", "targets": [], "risk": "low"}]
    assert not is_auto_ok(hass, intents)


@pytest.mark.parametrize(
    "intents",
    [
        [{"intent": "HassTurnOff", "targets": None, "risk": "low"}],
        [{"intent": "HassTurnOff", "targets": "light.k", "risk": "low"}],
        [{"intent": "HassTurnOff", "targets": [123], "risk": "low"}],
        ["not-a-dict"],
    ],
    ids=["null-targets", "str-targets", "non-str-target", "non-dict-intent"],
)
async def test_is_auto_ok_malformed_never_crashes(
    hass: HomeAssistant, intents: list
) -> None:
    """Malformed model-supplied intents fail closed instead of raising."""
    assert not is_auto_ok(hass, intents)


async def test_is_auto_ok_rejects_routing_data(hass: HomeAssistant) -> None:
    """A data slot that could steer the action past the classifier blocks auto."""
    intents = [
        {
            "intent": "HassTurnOff",
            "targets": ["light.k"],
            "data": {"name": "Front Door Lock"},
            "risk": "low",
        }
    ]
    assert not is_auto_ok(hass, intents)


async def test_is_auto_ok_allows_benign_param_data(hass: HomeAssistant) -> None:
    """Benign parameter data (brightness etc.) does not block auto."""
    intents = [
        {
            "intent": "HassLightSet",
            "targets": ["light.k"],
            "data": {"brightness_pct": 50},
            "risk": "low",
        }
    ]
    assert is_auto_ok(hass, intents)

"""Deterministic per-action criticality classifier for chat-driven actions.

Judges whether a proposed Home Assistant action is important enough to require
explicit confirmation, per ACTION from live entity metadata (not per domain).
This is the INNER gate: the add-on's coarse per-domain 403 is a backstop beneath
it, and Assist entity exposure is the OUTER, HA-controlled ceiling. The model's
``risk`` hint is advisory only — never the sole gate.

The gate is deliberately asymmetric: it only auto-executes an action it can
*positively* prove benign. Anything it cannot resolve, cannot bound, or whose
effect is opaque (a malformed target, a cover with no known device class, a
``script``/``scene``/``automation`` wrapper, an intent steered by ``data``
slots rather than its ``targets``) fails closed to confirmation.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.const import ATTR_DEVICE_CLASS, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import INTENT_RISK, RISK_LOW, RISK_SENSITIVE

# Domains where any action is treated as critical.
CRITICAL_DOMAINS = frozenset(
    {
        "lock",
        "alarm_control_panel",
        "update",
        "valve",
        "siren",
        "lawn_mower",
        "water_heater",
    }
)
# Wrapper domains whose activation runs an OPAQUE, unbounded effect the
# classifier cannot see through (a script may unlock a door, a scene may arm an
# alarm), so their activation always confirms.
CRITICAL_EFFECT_DOMAINS = frozenset({"script", "scene", "automation"})
# Cover device classes that are physical-access / security critical
# (shade/blind/awning/curtain/shutter/window/damper are benign).
CRITICAL_COVER_CLASSES = frozenset({"garage", "door", "gate"})
# Button device classes that are disruptive.
CRITICAL_BUTTON_CLASSES = frozenset({"restart", "update"})
# Integrations that manage network / infrastructure. This is a supplementary
# heuristic, not an exhaustive list — it cannot name every vendor, so network
# switches on other integrations rely on the user's critical-entities list and
# the structural gates below rather than on being enumerated here.
CRITICAL_PLATFORMS = frozenset(
    {
        "unifi",
        "mikrotik",
        "fritzbox",
        "fritz",
        "openwrt",
        "asuswrt",
        "tplink_omada",
        "ubnt",
    }
)
# Verb substrings that are state-critical regardless of entity metadata.
CRITICAL_VERBS = ("unlock", "disarm", "install", "reboot", "restart")
# ``data`` slots that can redirect an action onto an entity the ``targets`` list
# does not name; their presence means the action is not bounded by ``targets``,
# so it cannot be auto-executed.
ROUTING_DATA_KEYS = frozenset(
    {
        "name",
        "area",
        "area_id",
        "floor",
        "floor_id",
        "label",
        "label_id",
        "device_id",
        "entity_id",
        "device_class",
        "domain",
    }
)


@callback
def _resolve_device_class(
    hass: HomeAssistant, entity_id: str, entry: er.RegistryEntry | None
) -> str | None:
    """Resolve device_class from the registry first, then live state attributes.

    Entities without a unique_id have no registry entry, yet still expose their
    device class in live state — so a garage cover or restart button defined in
    YAML is still classified.
    """
    if entry is not None:
        registry_class = entry.device_class or entry.original_device_class
        if registry_class is not None:
            return registry_class
    state = hass.states.get(entity_id)
    if state is not None:
        state_class = state.attributes.get(ATTR_DEVICE_CLASS)
        if isinstance(state_class, str):
            return state_class
    return None


@callback
def is_critical(
    hass: HomeAssistant,
    entity_id: str,
    intent: str,
    *,
    critical_entities: Iterable[str] = (),
) -> bool:
    """Return True if acting on ``entity_id`` needs explicit confirmation.

    Fails closed: a malformed entity_id, a cover whose device class cannot be
    resolved, or an opaque-effect wrapper is treated as critical.
    """
    domain, sep, _ = entity_id.partition(".")
    if not sep:
        return True  # malformed id — cannot bound the action, confirm
    entry = er.async_get(hass).async_get(entity_id)
    entity_category = entry.entity_category if entry is not None else None
    platform = entry.platform if entry is not None else None
    device_class = _resolve_device_class(hass, entity_id, entry)
    verb = intent.lower()

    return (
        entity_id in set(critical_entities)
        or domain in CRITICAL_DOMAINS
        or domain in CRITICAL_EFFECT_DOMAINS
        or (
            domain == "cover"
            and (device_class is None or device_class in CRITICAL_COVER_CLASSES)
        )
        or (domain == "button" and device_class in CRITICAL_BUTTON_CLASSES)
        or entity_category == EntityCategory.CONFIG
        or platform in CRITICAL_PLATFORMS
        or any(v in verb for v in CRITICAL_VERBS)
    )


@callback
def _intent_auto_ok(hass: HomeAssistant, intent: object, critical: set[str]) -> bool:
    """Return True only when a single intent is provably benign and bounded."""
    if not isinstance(intent, dict):
        return False
    if intent.get(INTENT_RISK, RISK_SENSITIVE) != RISK_LOW:
        return False
    data = intent.get("data")
    if isinstance(data, dict) and ROUTING_DATA_KEYS.intersection(data):
        return False  # a data slot could steer the action past the classifier
    targets = intent.get("targets")
    if not isinstance(targets, list) or not targets:
        return False  # unbounded action — nothing concrete to classify
    name = str(intent.get("intent", ""))
    return all(
        isinstance(target, str)
        and not is_critical(hass, target, name, critical_entities=critical)
        for target in targets
    )


@callback
def is_auto_ok(
    hass: HomeAssistant,
    intents: list[dict[str, Any]],
    critical_entities: Iterable[str] = (),
) -> bool:
    """Allow auto only when EVERY intent is provably benign and target-bounded."""
    if not intents:
        return False
    critical = set(critical_entities)
    return all(_intent_auto_ok(hass, intent, critical) for intent in intents)

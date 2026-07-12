"""Soundness guard for the payload-aware automation gate.

These tests FREEZE the security-critical policy sets in ``automation_commit``. They
have no runtime effect — their only job is to FAIL LOUDLY the moment one of those
sets changes, so a change to the automation safety boundary can never land silently.

IF A TEST HERE FAILS you changed a security-critical set. STOP and re-audit before
updating the frozen value below:

* ``_ALLOWED_DOMAINS`` — for each ADDED domain, open its ``services.yaml`` and check:
  (1) does any service expose a NON-entity danger surface (a url / command / file /
  code / raw-passthrough field)? If so, add it to ``_DENIED_SERVICES``.
  (2) does any service reference entities under a data key OTHER than the standard
  ``entity_id`` / ``entities`` / ``snapshot_entities`` / ``group_members``? If so,
  extend the extraction in ``_collect_scene_entity_refs`` (and add a reject test).
  Only then add the domain and update the frozen set here.
* The other sets — confirm the change is intentional and the walker still inspects
  every runtime-merged call key and every entity-bearing key.

This mirrors the doc-comment above ``_ALLOWED_DOMAINS`` and makes that soundness
boundary un-rottable — the ``tests`` CI job blocks a policy change that skips it.
"""

from __future__ import annotations

from custom_components.claude_ha.automation_commit import (
    _ALLOWED_DOMAINS,
    _DENIED_SERVICES,
    _ENTITY_DATA_LIST_KEYS,
    _ENTITY_DATA_MAP_KEY,
    _SERVICE_KEYS,
    _TARGET_DATA_KEYS,
    _TARGET_SELECTOR_KEYS,
)

# The exact policy as audited at S3 (integration 1.5.0). Change here only after the
# re-audit described in this module's docstring.
_FROZEN_ALLOWED_DOMAINS = frozenset(
    {
        "light",
        "switch",
        "fan",
        "cover",
        "climate",
        "scene",
        "media_player",
        "lock",
        "notify",
        "persistent_notification",
        "input_boolean",
        "input_number",
        "input_text",
        "input_select",
        "input_datetime",
        "input_button",
        "timer",
        "counter",
        "button",
        "number",
        "select",
        "humidifier",
        "vacuum",
    }
)


def test_allowed_domains_are_frozen() -> None:
    """Adding/removing a domain requires the re-audit in this module's docstring."""
    assert _ALLOWED_DOMAINS == _FROZEN_ALLOWED_DOMAINS


def test_denied_services_are_frozen() -> None:
    """The residual non-entity-danger denylist is frozen."""
    assert (
        frozenset({"notify.file", "media_player.play_media", "vacuum.send_command"})
        == _DENIED_SERVICES
    )


def test_entity_bearing_data_keys_are_frozen() -> None:
    """The audited entity-bearing data keys the gate extracts are frozen."""
    assert _ENTITY_DATA_MAP_KEY == "entities"
    assert set(_ENTITY_DATA_LIST_KEYS) == {"snapshot_entities", "group_members"}


def test_inspected_call_keys_are_frozen() -> None:
    """The runtime-merged call keys the walker must inspect are frozen."""
    assert set(_TARGET_SELECTOR_KEYS) == {
        "device_id",
        "area_id",
        "floor_id",
        "label_id",
    }
    assert set(_TARGET_DATA_KEYS) == {"target", "data", "data_template"}
    assert set(_SERVICE_KEYS) == {"action", "service_template", "service"}

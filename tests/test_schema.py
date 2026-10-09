"""The schema library is chosen once, in ``schema.py``."""

from __future__ import annotations

import importlib
import sys

import pytest

from custom_components.claude_ha import schema


def test_prefers_probatio() -> None:
    """Home Assistant 2026.9+ validates with probatio, so the shim exposes it."""
    probatio = pytest.importorskip("probatio")

    assert schema.vol is probatio


def test_falls_back_to_voluptuous_without_probatio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Releases that predate probatio only ship voluptuous."""
    import voluptuous

    monkeypatch.setitem(sys.modules, "probatio", None)
    try:
        assert importlib.reload(schema).vol is voluptuous
    finally:
        monkeypatch.undo()
        importlib.reload(schema)

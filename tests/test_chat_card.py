"""Behaviour of the bundled chat card, run in Node (``chat_card_harness.mjs``)."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

import pytest

from custom_components.claude_ha.frontend import CARD_FILENAME

HARNESS = Path(__file__).with_name("chat_card_harness.mjs")
CARD = (
    Path(__file__).parents[1]
    / "custom_components"
    / "claude_ha"
    / "www"
    / CARD_FILENAME
)
INTENTS = [{"intent": "HassTurnOff"}]


@pytest.fixture(scope="module")
def results() -> dict[str, Any]:
    """Run every card scenario once. Node is required, never skipped."""
    node = shutil.which("node")
    assert node is not None, "the chat card tests need Node.js on PATH"
    completed = subprocess.run(
        [node, str(HARNESS), str(CARD)],
        capture_output=True,
        check=True,
        text=True,
        timeout=60,
    )
    parsed: dict[str, Any] = json.loads(completed.stdout)
    return parsed


def test_proposal_is_not_applied_after_the_target_changes(
    results: dict[str, Any],
) -> None:
    """A proposal from entry A never reaches entry B, and the history resets."""
    outcome = results["retarget_then_apply"]
    assert outcome["calls"] == [{"prompt": "lights", "config_entry": "A"}]
    assert outcome["messages"] == 0


def test_apply_goes_to_the_entry_that_proposed(results: dict[str, Any]) -> None:
    """A config change that keeps the target keeps the proposal and its entry."""
    assert results["same_target_apply"]["calls"] == [
        {"prompt": "lights", "config_entry": "A"},
        {
            "prompt": "lights",
            "mode": "write",
            "intents": INTENTS,
            "config_entry": "A",
        },
    ]


def test_late_reply_after_a_target_change_is_dropped(
    results: dict[str, Any],
) -> None:
    """An answer for the old target is discarded; the new target works at once."""
    outcome = results["late_reply"]
    assert outcome["busyAfterLate"] is False
    assert outcome["calls"] == [
        {"prompt": "lights", "config_entry": "A"},
        {"prompt": "hello", "config_entry": "B"},
    ]
    assert outcome["messages"] == [{"entry": "B", "text": "Done."}]


def test_without_a_target_nothing_extra_is_sent(results: dict[str, Any]) -> None:
    """With no ``config_entry`` the payloads are exactly what they always were."""
    assert results["no_target"]["calls"] == [
        {"prompt": "lights"},
        {"prompt": "lights", "mode": "write", "intents": INTENTS},
    ]

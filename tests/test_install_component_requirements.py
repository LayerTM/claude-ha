"""Tests for scripts/install_component_requirements.py."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "install_component_requirements.py"
ROOT = SCRIPT.parent.parent


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("component_requirements", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load()


def _component(root: Path, name: str, **manifest: object) -> None:
    (root / name).mkdir()
    (root / name / "manifest.json").write_text(json.dumps({"domain": name, **manifest}))


def test_requirements_follow_dependencies_transitively(tmp_path: Path) -> None:
    """A dependency's own dependencies count; after_dependencies do not."""
    _component(tmp_path, "a", dependencies=["b"], requirements=["one==1.0"])
    _component(tmp_path, "b", dependencies=["c", "a"], requirements=["two[x]==2.0"])
    _component(
        tmp_path,
        "c",
        requirements=["one==1.0", "three==3.0.post1"],
        after_dependencies=["d"],
    )

    assert script.component_requirements(tmp_path, ["a"]) == [
        "one==1.0",
        "three==3.0.post1",
        "two[x]==2.0",
    ]


def test_a_missing_manifest_fails(tmp_path: Path) -> None:
    """A dependency Home Assistant does not ship is an error, not a skip."""
    _component(tmp_path, "a", dependencies=["gone"])

    with pytest.raises(script.RequirementError, match="gone: no manifest"):
        script.component_requirements(tmp_path, ["a"])


@pytest.mark.parametrize(
    "requirement",
    [
        "one>=1.0",
        "one",
        "one==1.0,<2",
        "one @ https://x/y.whl",
        "one==1.0; python_version>'3'",
    ],
)
def test_an_unpinned_requirement_fails(tmp_path: Path, requirement: str) -> None:
    """Only an exact pin can be installed as the version Home Assistant ships."""
    _component(tmp_path, "a", requirements=[requirement])

    with pytest.raises(script.RequirementError, match="not pinned to one version"):
        script.component_requirements(tmp_path, ["a"])


def test_the_real_dependencies_resolve() -> None:
    """The installed Home Assistant yields the component pins the tests need."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--print"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    names = {line.split("==")[0] for line in result.stdout.split()}
    assert {"hassil", "home-assistant-intents", "home-assistant-frontend"} <= names


def test_main_reports_a_bad_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure is reported on stderr with exit code 1, and nothing is installed."""
    monkeypatch.chdir(ROOT)
    with (
        patch.object(
            script,
            "component_requirements",
            side_effect=script.RequirementError("x: bad"),
        ),
        patch.object(script.subprocess, "call") as call,
    ):
        assert script.main([]) == 1

    call.assert_not_called()
    assert "component requirements: x: bad" in capsys.readouterr().err


def test_main_installs_under_home_assistant_constraints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pins are installed with Home Assistant's package constraints."""
    monkeypatch.chdir(ROOT)
    with (
        patch.object(script, "component_requirements", return_value=["one==1.0"]),
        patch.object(script.subprocess, "call", return_value=0) as call,
    ):
        assert script.main([]) == 0

    args = call.call_args.args[0]
    assert args[:4] == [sys.executable, "-m", "pip", "install"]
    assert args[4] == "--constraint"
    assert args[5].endswith("package_constraints.txt")
    assert Path(args[5]).is_file()
    assert args[6:] == ["one==1.0"]


def test_main_with_nothing_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """No requirements means no pip call."""
    monkeypatch.chdir(ROOT)
    with (
        patch.object(script, "component_requirements", return_value=[]),
        patch.object(script.subprocess, "call") as call,
    ):
        assert script.main([]) == 0

    call.assert_not_called()

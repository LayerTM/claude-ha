"""The project version has exactly one source: the integration manifest."""

from __future__ import annotations

import json
from pathlib import Path
import re
import tomllib

_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST = "custom_components/claude_ha/manifest.json"


def test_project_version_is_read_from_the_manifest() -> None:
    """`pyproject.toml` states no version of its own, and reads the manifest's.

    Home Assistant loads the version from the manifest, so the copy in
    `pyproject.toml` was a second place to be wrong: a release that bumped one
    and not the other left the two disagreeing with nothing to notice it.

    The pattern is taken from the config and applied to the file the config
    names — the same thing the build backend does — rather than restated here,
    where a stale copy would pass while the build resolved nothing.
    """
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())

    assert "version" not in pyproject["project"]
    assert pyproject["project"]["dynamic"] == ["version"]

    source = pyproject["tool"]["hatch"]["version"]
    assert source["path"] == _MANIFEST

    manifest_text = (_ROOT / _MANIFEST).read_text()
    match = re.search(source["pattern"], manifest_text)
    assert match is not None, "the version pattern no longer matches the manifest"
    assert match.group("version") == json.loads(manifest_text)["version"]

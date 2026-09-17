#!/usr/bin/env python3
"""Install what the Home Assistant components this integration depends on need.

The integration's `dependencies` are set up before it, in tests as in a real
install, so their Python requirements must be present. Home Assistant states
those requirements in each component's manifest, pinned to the versions it
ships. This script reads them from the installed `homeassistant` package,
following `dependencies` transitively, and installs exactly those pins, under
Home Assistant's own package constraints. Nothing here, or in
requirements_test.txt, repeats a version, so a newer Home Assistant brings its
own and no bot can bump one past it.

Fails (exit 1) when a component's manifest is missing or a requirement is not
pinned to one version.

Usage:
    python scripts/install_component_requirements.py           # install
    python scripts/install_component_requirements.py --print   # list only
"""

from __future__ import annotations

from collections.abc import Iterable
import json
from pathlib import Path
import re
import subprocess
import sys

MANIFEST = Path("custom_components/claude_ha/manifest.json")
PINNED = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?==[^=<>!~,;\s]+$"
)


class RequirementError(Exception):
    """A dependency's requirements cannot be read as exact pins."""


def component_requirements(components_dir: Path, roots: Iterable[str]) -> list[str]:
    """Return the sorted requirements of ``roots`` and their dependencies."""
    seen: set[str] = set()
    pending = list(roots)
    requirements: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        manifest_path = components_dir / name / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text())
        except FileNotFoundError as err:
            raise RequirementError(f"{name}: no manifest at {manifest_path}") from err
        for requirement in manifest.get("requirements", []):
            if not PINNED.match(requirement):
                raise RequirementError(
                    f"{name}: requirement {requirement!r} is not pinned to one version"
                )
            requirements.add(requirement)
        pending.extend(manifest.get("dependencies", []))
    return sorted(requirements)


def main(argv: list[str]) -> int:
    """Install (or with ``--print`` list) the dependencies' requirements."""
    import homeassistant  # noqa: PLC0415 - the installed one is the source

    package = Path(homeassistant.__file__).parent
    roots = json.loads(MANIFEST.read_text())["dependencies"]
    try:
        requirements = component_requirements(package / "components", roots)
    except RequirementError as err:
        print(f"component requirements: {err}", file=sys.stderr)
        return 1
    if "--print" in argv or not requirements:
        print("\n".join(requirements))
        return 0
    constraints = package / "package_constraints.txt"
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--constraint",
            str(constraints),
            *requirements,
        ]
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

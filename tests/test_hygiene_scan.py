"""Tests for scripts/hygiene_scan.py (excluded from its own scan via SELF).

A scanner can fail in three ways and only one of them shows in its output, so all
three are proved here: every rule catches the shape it is for; every rule is the
one doing the catching (disable it and its own vectors go unreported, which is
what keeps a rule from being decorative); and the two scanners in this repository
do not overlap, in either direction.

Then the properties that are about the tree rather than the text: a clean tree
passes, a symlink that leaves the repository fails, and a file that cannot be
decoded is a FAILURE rather than a skip.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest

from .test_secret_scan import SHOULD_FLAG

SCRIPTS = Path(__file__).parent.parent / "scripts"


def _load(name: str) -> types.ModuleType:
    """Import a scanner from scripts/, which is not a package."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: a dataclass defined in the module resolves its
    # own annotations through sys.modules while the module body is still running.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hygiene_scan = _load("hygiene_scan")
secret_scan = _load("secret_scan")

# rule name -> lines that rule must catch.
VECTORS: dict[str, list[str]] = {
    "machine-path": [
        "cwd = /Users/someone/project",
        'workdir: "/home/builder/app"',
        "socket at /private/tmp/session-4/ipc",
        "cache /var/folders/9k/abcd1234/T/build",
        r"path = C:\Users\someone\src",
        "see file:///Users/someone/project/notes.md",
        "launch: file:///home/builder/app/index.html",
    ],
    "transcript-url": [
        "see https://claude.ai/code/session_01ABCDEFxyz for the discussion",
        "notes: claude.ai/code/session/0123456789",
    ],
    "attribution-trailer": [
        "Co-Authored-By: Someone <someone@example.com>",
        "  co-authored-by: Someone Else <else@example.com>",
        "Generated with [Some Tool](https://example.com)",
        "\U0001f916 Generated with a tool",
    ],
}

# Lines that must NOT be reported: ordinary content that resembles a rule.
CLEAN = [
    "the integration stores state under /data and reads /config",
    "docs live under /Users and are not paths",  # no path into it
    "See https://www.home-assistant.io/integrations/ for the list",
    "co-authored the specification with the working group",  # not a trailer line
    "The badge is generated with shields.io",  # not at line start
    "/home/ is not a path either",
    "mkdir -p /data/home/.storage",  # a home inside a data dir
    "cache lives in /srv/Users/shared/x",  # not a per-machine path
    'mkdir -p "${work}/home/config"',  # built from a variable
    'cd "$(mktemp -d)/Users/test/app"',  # likewise
]

ALL_VECTORS = [(name, line) for name, lines in VECTORS.items() for line in lines]


def _run_git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )


def _make_tree(root: Path, files: dict[str, bytes]) -> None:
    """Build a git repository containing exactly these files, all tracked."""
    _run_git(root, "init", "-q")
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _run_git(root, "add", "-A")


def test_every_rule_has_a_vector() -> None:
    """A rule with no vector is a rule nothing proves."""
    assert {rule.name for rule in hygiene_scan.RULES} == set(VECTORS)


@pytest.mark.parametrize(("name", "line"), ALL_VECTORS)
def test_rule_catches_its_own_shape(name: str, line: str) -> None:
    assert name in [hit for hit, _ in hygiene_scan.scan_text(line)]


@pytest.mark.parametrize("line", CLEAN)
def test_ordinary_content_is_not_reported(line: str) -> None:
    assert hygiene_scan.scan_text(line) == []


@pytest.mark.parametrize(("name", "line"), ALL_VECTORS)
def test_disabling_a_rule_leaves_its_vector_unreported(name: str, line: str) -> None:
    """Prove that no rule is decorative.

    A vector still caught with its own rule removed was being caught by something
    else — the rule could be deleted and no test would notice.
    """
    weakened = [rule for rule in hygiene_scan.RULES if rule.name != name]
    assert hygiene_scan.scan_text(line, weakened) == []


@pytest.mark.parametrize(("name", "line"), ALL_VECTORS)
def test_secret_scan_does_not_report_a_hygiene_vector(name: str, line: str) -> None:
    assert secret_scan.scan_text(line) == []


@pytest.mark.parametrize("line", SHOULD_FLAG)
def test_hygiene_does_not_report_a_secret_scan_vector(line: str) -> None:
    assert hygiene_scan.scan_text(line) == []


def test_a_clean_tree_passes(tmp_path: Path) -> None:
    _make_tree(
        tmp_path,
        {
            "README.md": b"# Fine\n\nPaths here are relative: ./src/app.js\n",
            "src/app.js": b"const base = './data';\n",
            "logo.png": b"\x89PNG\r\n\x1a\n\x00\x00binary",
        },
    )
    os.symlink("src/app.js", tmp_path / "docs_link")
    _run_git(tmp_path, "add", "-A")
    assert hygiene_scan.scan(tmp_path) == []


def test_symlinks_leaving_the_repository_are_reported(tmp_path: Path) -> None:
    _make_tree(tmp_path, {"keep.txt": b"content\n", "sub/keep.txt": b"content\n"})
    os.symlink("/etc/hosts", tmp_path / "absolute_link")
    os.symlink("../../elsewhere", tmp_path / "sub" / "escaping_link")
    os.symlink("keep.txt", tmp_path / "sub" / "inside_link")
    _run_git(tmp_path, "add", "-A")

    reported = {f.path: f.rule for f in hygiene_scan.scan(tmp_path)}
    assert reported == {
        "absolute_link": "symlink-absolute",
        "sub/escaping_link": "symlink-outside-repo",
    }
    # The mutation half for a check that is not a regex.
    assert [
        f for f in hygiene_scan.scan(tmp_path, symlinks=False) if "link" in f.path
    ] == []


def test_an_undecodable_file_is_a_failure_not_a_skip(tmp_path: Path) -> None:
    # Text in a single-byte encoding: no NUL, so it is not taken for a binary,
    # and it is not valid UTF-8 either.
    _make_tree(tmp_path, {"notes.txt": "héllo wörld".encode("latin-1")})
    assert any(f.rule == "unreadable" for f in hygiene_scan.scan(tmp_path))


def test_a_tracked_file_that_is_gone_is_a_failure(tmp_path: Path) -> None:
    _make_tree(tmp_path, {"gone.txt": b"content\n"})
    (tmp_path / "gone.txt").unlink()
    assert any(f.rule == "unreadable" for f in hygiene_scan.scan(tmp_path))


def test_the_scanner_skips_itself_by_path_not_by_name(tmp_path: Path) -> None:
    _make_tree(tmp_path, {"tools/hygiene_scan.py": b"p = '/Users/someone/x'\n"})
    assert hygiene_scan.scan(tmp_path) != []


def test_a_tree_it_cannot_list_is_an_error(tmp_path: Path) -> None:
    """Fail closed on a tree that cannot be listed.

    Not a git repository: the scan could not be performed, so the result is
    neither clean nor a finding.
    """
    with pytest.raises(hygiene_scan.ScanError):
        hygiene_scan.scan(tmp_path)


def test_this_repository_is_clean() -> None:
    """The check every other test exists to make trustworthy."""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "hygiene_scan.py"), str(SCRIPTS.parent)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

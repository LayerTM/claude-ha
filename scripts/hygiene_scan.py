#!/usr/bin/env python3
"""Repository hygiene — traces of one machine must not reach a distribution.

A repository is copied onto machines that are nothing like the one it was written
on, so anything that only resolves on the author's computer is a defect there: an
absolute home-directory path, a temporary-directory path, a symlink pointing at
something the clone does not contain. None of it fails a build here, and all of it
fails somewhere else — which is why it needs a check rather than a habit.

The same applies to the by-products of authoring tools. A link to an editing or
chat transcript is only reachable by the person who made it, and an attribution
trailer belongs to the commit metadata, not to a file that ships.

WHAT THIS DOES NOT OWN. Secrets, personal e-mail addresses and non-routable
network addresses belong to `scripts/secret_scan.py`. This scanner matches none of them,
so no line is ever failed by both checks; `tests/test_hygiene_scan.py` asserts that
split mechanically, in both directions.

FAIL CLOSED. A tracked file that cannot be read or decoded is a FAILURE, not a
skip: "I could not look" and "I looked and it was clean" must not share an exit
code. Tracked files only — what is not tracked is not distributed.

Usage:
    python scripts/hygiene_scan.py [path]   # default: current directory
Exit: 0 clean, 1 findings, 2 the scan could not be performed.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys


@dataclass(frozen=True)
class Rule:
    """One named rule: the shape it recognises, written as a pattern."""

    name: str
    pattern: re.Pattern[str]


RULES: list[Rule] = [
    # A path rooted in somebody's home or in a per-boot temporary directory.
    # Two boundaries matter, and both were found by running this over a real
    # tree rather than by reasoning about it. A trailing separator, so that a
    # mention of the directory itself ("files under /Users") is not read as a
    # path into it. And a leading one: an application that keeps its own home
    # under a data directory writes /data/home/... , which contains "/home/"
    # without being anybody's home directory — and a path assembled from a
    # variable, "${work}/home/x", is a temporary directory, not this machine.
    # The one place a leading slash is not a segment boundary is a file:// URL,
    # which is how such a path reaches a README or a launch configuration, so
    # that scheme is matched explicitly rather than excluded with everything else.
    Rule(
        "machine-path",
        re.compile(
            r"(?<![A-Za-z0-9._/})-])(?:file://)?"
            r"(?:/Users/[A-Za-z0-9._-]+/"
            r"|/home/[A-Za-z0-9._-]+/"
            r"|/private/tmp/[A-Za-z0-9._-]+"
            r"|/var/folders/[A-Za-z0-9._+-]+"
            r"|[A-Za-z]:\\Users\\[A-Za-z0-9._-]+)"
        ),
    ),
    # A link into an authoring session: private to whoever created it, dead for
    # every reader of the repository.
    Rule(
        "transcript-url",
        re.compile(r"(?i)\bclaude\.ai/code/session[_/][A-Za-z0-9_-]+"),
    ),
    # Trailers are commit metadata. In a tracked file they are a copy that no
    # longer describes anything, and they name people and tools the file does not
    # otherwise involve.
    Rule(
        "attribution-trailer",
        re.compile(
            r"(?im)^[ \t]*co-authored-by[ \t]*:"
            r"|^[ \t]*(?:\U0001F916[ \t]*)?generated with[ \t]"
        ),
    ),
]

# This file and its test contain every pattern literally, so they are skipped by
# exact repository-relative path. A file of the same name anywhere else is scanned.
SELF = {
    "scripts/hygiene_scan.py",
    "tests/test_hygiene_scan.py",
}

SKIP_SUFFIX = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".woff",
    ".woff2",
    ".ttf",
}

OWNERSHIP = (
    "hygiene: machine-specific paths, symlinks leaving the repository, "
    "transcript URLs and attribution trailers.\n"
    "secret-scan: secrets, personal e-mail addresses and non-routable network "
    "addresses. No line is reported by both."
)


@dataclass(frozen=True)
class Finding:
    """One trace found: where it is, which rule named it, and what matched."""

    path: str
    line: int  # 0 = the file itself rather than a line in it
    rule: str
    text: str

    def __str__(self) -> str:
        """Render the finding as `path:line: rule — matched text`."""
        return f"{self.path}:{self.line}: {self.rule} — {self.text[:70]}"


class ScanError(Exception):
    """The scan could not be performed — never reported as a clean tree."""


def tracked_files(root: Path) -> list[str]:
    """Return the repository-relative path of every tracked file under root."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScanError(f"cannot list tracked files in {root}: {exc}") from exc
    return [p for p in out.stdout.split("\0") if p]


def symlink_finding(root: Path, rel: str) -> Finding | None:
    """Report a symlink whose target no clone is guaranteed to contain.

    A symlink is a file whose content is a path. One that is absolute, or that
    climbs out of the repository, describes a layout only its author has.
    """
    full = root / rel
    if not full.is_symlink():
        return None
    try:
        target = os.readlink(full)
    except OSError as exc:
        return Finding(rel, 0, "symlink-unreadable", str(exc))
    if os.path.isabs(target):
        return Finding(rel, 0, "symlink-absolute", f"-> {target}")
    resolved = os.path.normpath(os.path.join(os.path.dirname(rel), target))
    if resolved == ".." or resolved.startswith(".." + os.sep):
        return Finding(rel, 0, "symlink-outside-repo", f"-> {target}")
    return None


def file_text(root: Path, rel: str) -> str | None:
    """Return the file's text, or None when it is binary.

    Raises when the file cannot be read or decoded; the caller turns that into a
    finding, never into a pass.
    """
    try:
        data = (root / rel).read_bytes()
    except OSError as exc:
        raise ScanError(str(exc)) from exc
    if b"\x00" in data[:2048]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ScanError(f"not UTF-8 text: {exc}") from exc


def scan_text(text: str, rules: list[Rule] | None = None) -> list[tuple[str, str]]:
    """(rule name, matched text) for one line."""
    hits: list[tuple[str, str]] = []
    for rule in rules if rules is not None else RULES:
        for match in rule.pattern.finditer(text):
            hits.append((rule.name, match.group(0)))
    return hits


def scan(
    root: Path, rules: list[Rule] | None = None, symlinks: bool = True
) -> list[Finding]:
    """Return every finding in the tracked files of root."""
    findings: list[Finding] = []
    for rel in tracked_files(root):
        if rel in SELF:
            continue
        if symlinks:
            link = symlink_finding(root, rel)
            if link is not None:
                findings.append(link)
                continue  # its content is the target, already judged
        elif (root / rel).is_symlink():
            continue
        if Path(rel).suffix.lower() in SKIP_SUFFIX:
            continue
        try:
            text = file_text(root, rel)
        except ScanError as exc:
            findings.append(Finding(rel, 0, "unreadable", str(exc)))
            continue
        if text is None:
            continue  # binary
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, found in scan_text(line, rules):
                findings.append(Finding(rel, lineno, name, found))
    return findings


def main(argv: list[str]) -> int:
    """Scan the given path (default cwd); 0 clean, 1 findings, 2 could not scan."""
    root = Path(argv[1]) if len(argv) > 1 else Path()
    try:
        findings = scan(root)
    except ScanError as exc:
        print(f"::error::hygiene: {exc}", file=sys.stderr)
        return 2
    if findings:
        print(
            "::error::hygiene: this tree carries traces of the machine it was "
            "written on:\n",
            file=sys.stderr,
        )
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        print(f"\n{OWNERSHIP}", file=sys.stderr)
        return 1
    print("hygiene: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

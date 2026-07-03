#!/usr/bin/env python3
"""Secret / PII scanner — blocks secrets and personal data from the repo.

Pattern-based and placeholder-aware, so sanitized fixtures and docs pass while
real credentials, tokens, personal paths/emails and private IPs are blocked.
Runs in pre-commit and CI. Exit code 1 on any finding.

Usage:
    python scripts/secret_scan.py [path]   # default: current directory
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

# Snippets that mark a value as an intentional placeholder (allowed), so test
# tokens and examples do not trip the scanner. Keep these anchored to explicit
# placeholder tokens — do not add broad substrings.
ALLOW = re.compile(
    r"(?i)(redacted|example|placeholder|synthetic|dummy|sample|changeme|your[-_]?|"
    r"s3cr3t|test[-_]?token|<[a-z0-9_.\-]+>|noreply)"
)

# name -> compiled pattern. Each matches a *real-looking* secret / PII value.
PATTERNS: dict[str, re.Pattern[str]] = {
    "JWT / bearer token": re.compile(
        r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,}"
    ),
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    "OpenAI API key": re.compile(r"\bsk-[A-Za-z0-9]{32,}"),
    "GitHub token (classic)": re.compile(r"\bghp_[A-Za-z0-9]{30,}"),
    "GitHub token (fine-grained)": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    "GitHub OAuth/user token": re.compile(r"\bgh[ousr]_[A-Za-z0-9]{30,}"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    "AWS access key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "private key block": re.compile(r"BEGIN (RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY"),
    "X-API-Key value": re.compile(r'(?i)x-api-key\s*[:=]\s*["\']?[A-Za-z0-9_\-]{16,}'),
    "TOKEN cookie value": re.compile(r"\bTOKEN=[A-Za-z0-9._\-]{16,}"),
    "hardcoded password": re.compile(
        r'(?i)\bpassword\b\s*[:=]\s*["\'][^"\'{}<\s]{4,}["\']'
    ),
    "personal macOS path": re.compile(r"/Users/[a-z]"),
    "personal email (gmail)": re.compile(r"[A-Za-z0-9._%+-]+@gmail\.com"),
    "private LAN IP": re.compile(r"\b(?:192\.168|10\.0\.0)\.\d{1,3}\b"),
}

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".research",
}
SKIP_SUFFIX = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".svg",
    ".pdf",
    ".zip",
    ".gz",
    ".mo",
    ".woff",
    ".woff2",
}
# These files legitimately hold pattern literals / secret test vectors.
SKIP_FILES = {Path(__file__).name, "test_secret_scan.py"}


def _git_tracked(root: Path) -> list[Path] | None:
    """Files git would commit (tracked + untracked, excluding .gitignored)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-c", "-o", "--exclude-standard"],
            capture_output=True,
            text=True,
            check=True,
        )
    except OSError:
        return None
    except subprocess.SubprocessError:
        return None
    return [root / line for line in out.stdout.splitlines() if line]


def _is_binary(path: Path) -> bool:
    try:
        return b"\x00" in path.read_bytes()[:2048]
    except OSError:
        return True


def iter_files(root: Path):
    """Yield the text files that git would commit (respecting .gitignore)."""
    tracked = _git_tracked(root)
    candidates = tracked if tracked is not None else sorted(root.rglob("*"))
    for p in candidates:
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIX:
            continue
        if p.name in SKIP_FILES:
            continue
        if _is_binary(p):
            continue
        yield p


def scan_text(text: str) -> list[tuple[str, str]]:
    """Return (pattern-name, snippet) for every non-placeholder match in text."""
    hits: list[tuple[str, str]] = []
    for name, rx in PATTERNS.items():
        for m in rx.finditer(text):
            snippet = m.group(0)
            if ALLOW.search(snippet):
                continue
            hits.append((name, snippet[:70]))
    return hits


def main(argv: list[str]) -> int:
    """Scan the given path (default cwd); return 1 if anything is flagged."""
    root = Path(argv[1]) if len(argv) > 1 else Path()
    problems: list[tuple[Path, str, str]] = []
    for f in iter_files(root):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, snip in scan_text(text):
            problems.append((f, name, snip))

    if problems:
        print("SECRET-SCAN: potential secret/PII leakage detected:\n", file=sys.stderr)
        for f, name, snip in problems:
            print(f"  {f}: [{name}] {snip}", file=sys.stderr)
        print(
            "\nBlocked. Sanitize the value, use a placeholder, or move it to a "
            "gitignored path.",
            file=sys.stderr,
        )
        return 1

    print("secret-scan: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

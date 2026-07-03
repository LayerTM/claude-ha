"""Tests for scripts/secret_scan.py (excluded from the scan via SKIP_FILES)."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "secret_scan.py"


def _run(tmp_path: Path, content: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "sample.txt").write_text(content, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "content",
    [
        "key = sk-ant-abcdefghijklmnopqrstuvwxyz012345",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "home = /Users/somebody/secret",
        "contact = someone@gmail.com",
        "host = 192.168.1.50",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
    ],
)
def test_flags_real_secrets(tmp_path: Path, content: str) -> None:
    """A real-looking secret/PII value fails the scan."""
    assert _run(tmp_path, content).returncode == 1


@pytest.mark.parametrize(
    "content",
    [
        "token = s3cr3t-bearer-token",
        "key = sk-ant-EXAMPLE-not-a-real-key",
        "just a normal line of prose",
        "url = https://github.com/LayerTM/claude-ha",
    ],
)
def test_allows_clean_and_placeholders(tmp_path: Path, content: str) -> None:
    """Placeholders and ordinary text pass the scan."""
    assert _run(tmp_path, content).returncode == 0

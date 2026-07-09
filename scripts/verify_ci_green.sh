#!/usr/bin/env bash
# Verify every CI check on a commit concluded green before a release is cut.
#
# A release is not "done" until the CI run on the released commit is verified
# green — local checks are not enough. Run this on the pushed commit and only
# `gh release create` when it exits 0.
#
# Usage: scripts/verify_ci_green.sh [<sha>]   (default: current HEAD)
set -euo pipefail

REPO="LayerTM/claude-ha"
SHA="${1:-$(git rev-parse HEAD)}"

echo "Verifying CI for ${SHA} on ${REPO} ..."
lines="$(gh api "repos/${REPO}/commits/${SHA}/check-runs" \
  --jq '.check_runs[] | "\(.status) \(.conclusion) \(.name)"')"

if [ -z "${lines}" ]; then
  echo "FAIL: no CI checks found for ${SHA}. Pushed yet? Has CI started?"
  exit 1
fi

echo "${lines}"

# Any check that is not completed+success blocks the release.
if printf '%s\n' "${lines}" | grep -vq '^completed success '; then
  echo "FAIL: not all checks are completed + success — do NOT release ${SHA}."
  exit 1
fi

echo "OK: all checks green on ${SHA} — clear to release."

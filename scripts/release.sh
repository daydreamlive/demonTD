#!/usr/bin/env bash
#
# Pre-release gate.
#
# Runs the same drift check that CI runs, plus the local pytest suite.
# Exits non-zero on any failure so you cannot accidentally tag a release
# while protocol drift is outstanding.
#
# Usage:
#     bash scripts/release.sh
#
# Required: a sibling checkout of demon-public-demo at
# ~/git/demon-public-demo. Override via DEMON_PUBLIC_DEMO env var.

set -euo pipefail

DEMON_PUBLIC_DEMO="${DEMON_PUBLIC_DEMO:-$HOME/git/demon-public-demo}"

if [[ ! -d "$DEMON_PUBLIC_DEMO" ]]; then
    echo "error: demon-public-demo not found at $DEMON_PUBLIC_DEMO" >&2
    echo "       clone it first or set DEMON_PUBLIC_DEMO=/path/to/checkout" >&2
    exit 2
fi

# Find the repo root (works whether invoked from repo root or scripts/).
REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$REPO_ROOT"

echo ">>> [1/3] Fetching latest demon-public-demo (origin)..."
# Just fetch — do NOT `git pull` the checked-out branch. That checkout is
# usually parked on a stale `claude/sync/*` branch, and pulling it (then
# diffing the working tree) once hid 23 commits of real backend drift.
# The drift checker reads the reference from `origin/main` directly, so a
# plain fetch is all we need here.
git -C "$DEMON_PUBLIC_DEMO" fetch --quiet origin || \
    echo "    (fetch failed — drift check will use the local origin ref)"

echo
echo ">>> [2/3] Running protocol drift check (vs origin/main)..."
# The script defaults to --ref origin/main and self-fetches; it reads the
# reference from that ref, never the working tree.
python3 scripts/check_protocol_drift.py \
    --demonTD . \
    --demon-public-demo "$DEMON_PUBLIC_DEMO"

echo
echo ">>> [3/3] Running pytest..."
if [[ -d tests ]]; then
    PYTHONPATH=src python3 -m pytest tests/ -v
else
    echo "    (no tests/ directory — skipping)"
fi

echo
echo "============================================================"
echo " All pre-release checks passed."
echo "============================================================"
echo
echo "Next steps (manual):"
echo
echo "  1. Open TouchDesigner, Run Script on build/build_tox.py."
echo "  2. Confirm dist/demonTD.tox mtime is fresh:"
echo "       ls -la dist/demonTD.tox"
echo "  3. Update CHANGELOG.md with the new version's entry."
echo "  4. git commit -am 'vX.Y.Z — ...'"
echo "  5. git tag -a vX.Y.Z -m 'vX.Y.Z — ...'"
echo "  6. git push origin main vX.Y.Z"
echo "  7. gh release create vX.Y.Z --notes-file <(...)"
echo
echo "  Only AFTER you have visually confirmed dist/demonTD.tox is the"
echo "  newly-built artifact:"
echo "     gh release upload vX.Y.Z dist/demonTD.tox"
echo
echo "  (We deliberately upload the .tox in a separate step. The v0.1.5"
echo "  release initially shipped a stale .tox; this two-step protocol"
echo "  prevents recurrence.)"

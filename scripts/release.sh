#!/usr/bin/env bash
#
# Pre-release gate.
#
# 1. Contract freshness: vendor/demon_contract.json must match
#    DEMON@origin/main (scripts/sync_contract.py --check fetches origin
#    and re-extracts; reads the ref, never the working tree).
# 2. pytest — includes tests/test_contract.py, which holds demonTD's
#    real surface against that vendored contract.
#
# Exits non-zero on any failure so you cannot tag a release while the
# contract is stale or parity is broken.
#
# Usage:
#     bash scripts/release.sh
#
# Required: a checkout of daydreamlive/DEMON at ~/git/DEMON (override
# via DEMON_REPO). The contract dumper needs an interpreter with numpy —
# sync_contract.py auto-detects .venv-test/ (create with:
#   python3 -m venv .venv-test && .venv-test/bin/pip install numpy pytest).

set -euo pipefail

DEMON_REPO="${DEMON_REPO:-$HOME/git/DEMON}"

if [[ ! -d "$DEMON_REPO" ]]; then
    echo "error: DEMON checkout not found at $DEMON_REPO" >&2
    echo "       clone daydreamlive/DEMON or set DEMON_REPO=/path/to/checkout" >&2
    exit 2
fi

# Find the repo root (works whether invoked from repo root or scripts/).
REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$REPO_ROOT"

# Prefer the numpy-bearing test venv for both steps (the repo .venv's
# numpy is known-broken on this machine; see memory/CLAUDE notes).
PY="$REPO_ROOT/.venv-test/bin/python"
[[ -x "$PY" ]] || PY=python3

echo ">>> [1/2] Contract freshness check (vs DEMON origin/main)..."
# --check fetches DEMON's origin itself and compares the committed
# artifact against a fresh extraction. Exit 1 = stale: run
#   $PY scripts/sync_contract.py --demon "$DEMON_REPO"
# review the diff, fix or whitelist until pytest is green, commit both.
"$PY" scripts/sync_contract.py --demon "$DEMON_REPO" --check

echo
echo ">>> [2/2] Running pytest (includes contract tests)..."
PYTHONPATH=src "$PY" -m pytest tests/ -v

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
echo
echo "  Remember the bundle zip: the release must include demonTD-vX.Y.Z.zip"
echo "  (.tox + vendor/ — which now carries demon_contract.json for the"
echo "  runtime drift check)."

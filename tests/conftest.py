"""Shared fixtures: the vendored DEMON contract + the parity whitelist.

`contract` is the artifact scripts/sync_contract.py extracts from
DEMON@origin/main (the backend's authoritative wire registry + the web
UI's parity data). `whitelist` is demonTD's registry of intentional
feature gaps. tests/test_contract.py compares the two against the real
src/ modules — that suite IS the drift checker.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def contract() -> dict:
    return json.loads(
        (REPO_ROOT / "vendor" / "demon_contract.json").read_text())


@pytest.fixture(scope="session")
def whitelist() -> dict:
    return json.loads(
        (REPO_ROOT / "contracts" / "parity_whitelist.json").read_text())

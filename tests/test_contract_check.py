"""Unit tests for src/contract_check.py (the runtime drift check)."""
from __future__ import annotations

import json

import contract_check as cc


# ---------------------------------------------------------------------------
# URL derivation
# ---------------------------------------------------------------------------

def test_http_base_wss_strips_signed_path():
    assert cc.http_base_from_ws_url(
        "wss://pod-abc.daydream.live/ws/session?token=secret123"
    ) == "https://pod-abc.daydream.live"


def test_http_base_ws_keeps_port():
    assert cc.http_base_from_ws_url(
        "ws://192.168.1.20:8765/stream") == "http://192.168.1.20:8765"


def test_http_base_rejects_garbage():
    assert cc.http_base_from_ws_url("not a url") is None
    assert cc.http_base_from_ws_url("ftp://x/y") is None
    assert cc.http_base_from_ws_url("") is None


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def _local(commands=("prompt", "params"), events=("ready", "error"),
           config=("sde", "steps"), knobs=("denoise", "shift"),
           version=1, knob_version=1):
    return {
        "protocol": {
            "version": version,
            "commands": {c: {} for c in commands},
            "events": {e: {} for e in events},
            "config": {f: {} for f in config},
        },
        "knobs": {"version": knob_version,
                  "ode": {k: {} for k in knobs}},
    }


def _remote(commands=("prompt", "params"), events=("ready", "error"),
            config=("sde", "steps"), version=1):
    return {
        "version": version,
        "commands": {c: {} for c in commands},
        "events": {e: {} for e in events},
        "config": {f: {} for f in config},
    }


def _remote_knobs(knobs=("denoise", "shift"), version=1):
    return {"version": version, "knobs": {k: {} for k in knobs}}


def test_identical_contracts_no_drift():
    assert cc.diff_contract(_local(), _remote(), _remote_knobs()) == []


def test_server_added_command_and_event():
    lines = cc.diff_contract(
        _local(),
        _remote(commands=("prompt", "params", "set_groove"),
                events=("ready", "error", "groove_applied")))
    assert any("added command 'set_groove'" in l for l in lines)
    assert any("added event 'groove_applied'" in l for l in lines)


def test_server_dropped_command():
    lines = cc.diff_contract(_local(), _remote(commands=("prompt",)))
    assert any("dropped command 'params'" in l for l in lines)


def test_version_bump_reported():
    lines = cc.diff_contract(_local(), _remote(version=2))
    assert any("pod has v2" in l and "expects v1" in l for l in lines)


def test_config_field_added():
    lines = cc.diff_contract(
        _local(), _remote(config=("sde", "steps", "capabilities")))
    assert any("added config field 'capabilities'" in l for l in lines)


def test_knob_diff_ignores_per_session_lora_strengths():
    lines = cc.diff_contract(
        _local(), _remote(),
        _remote_knobs(knobs=("denoise", "shift", "lora_str_bach")))
    assert lines == []


def test_knob_added_and_dropped():
    lines = cc.diff_contract(
        _local(), _remote(),
        _remote_knobs(knobs=("denoise", "groove_amt")))
    assert any("added knob 'groove_amt'" in l for l in lines)
    assert any("dropped knob 'shift'" in l for l in lines)


def test_malformed_remote_side_fabricates_nothing():
    # A pod that returns {} for commands must not produce a wall of
    # "dropped" lines.
    lines = cc.diff_contract(_local(), {"version": 1, "commands": {},
                                        "events": {}, "config": {}})
    assert lines == []


def test_no_remote_knobs_skips_knob_diff():
    assert cc.diff_contract(_local(), _remote(), None) == []


# ---------------------------------------------------------------------------
# local contract loading
# ---------------------------------------------------------------------------

def test_load_local_contract_roundtrip(tmp_path):
    p = tmp_path / cc.CONTRACT_FILENAME
    p.write_text(json.dumps({"protocol": {"version": 1}}))
    assert cc.load_local_contract(str(tmp_path)) == {"protocol": {"version": 1}}


def test_load_local_contract_missing_or_bad(tmp_path):
    assert cc.load_local_contract(None) is None
    assert cc.load_local_contract(str(tmp_path / "nope")) is None
    (tmp_path / cc.CONTRACT_FILENAME).write_text("{broken")
    assert cc.load_local_contract(str(tmp_path)) is None


def test_run_check_returns_none_without_local_contract(tmp_path):
    # No vendored contract -> the whole check is a silent no-op, no
    # network touched (the URL is bogus on purpose).
    assert cc.run_check("wss://example.invalid/ws", str(tmp_path)) is None


# ---------------------------------------------------------------------------
# against the REAL vendored artifact — the shipped contract must diff
# clean against itself reshaped as the live endpoints' payloads
# ---------------------------------------------------------------------------

def test_real_artifact_self_diff_is_clean(contract):
    remote_protocol = contract["protocol"]
    remote_knobs = {"version": contract["knobs"]["version"],
                    "knobs": contract["knobs"]["ode"]}
    assert cc.diff_contract(contract, remote_protocol, remote_knobs) == []

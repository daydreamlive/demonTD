"""Runtime contract check — pure helpers, no TouchDesigner dependencies.

At connect (first `ready` per WS generation), demon_ext spawns a
fire-and-forget thread that GETs the pod's self-describing contract
(`/api/protocol`, `/api/knobs`) and diffs it against the contract this
build shipped with (`vendor/demon_contract.json`, vendored by
scripts/sync_contract.py). A stale .tox then TELLS you it's stale —
"server added command X / event Y" in the textport and Status — instead
of mysteriously misbehaving.

Names-only comparison: field-level and default-value drift is the CI
suite's job (tests/test_contract.py); at runtime only vocabulary gaps
are actionable enough to surface.

Never blocks, never raises into the caller's thread, never tears down a
session — a pod without these endpoints (older backend) just skips the
check.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any
from urllib.parse import urlsplit, urlunsplit

CONTRACT_FILENAME = "demon_contract.json"


def http_base_from_ws_url(ws_url: str) -> str | None:
    """Derive the pod's HTTP origin from its WS URL.

    ws:// -> http://, wss:// -> https://, host:port kept, path/query
    DROPPED — hosted WS URLs carry signed paths/tokens, but the API
    endpoints live at the pod root."""
    try:
        parts = urlsplit(ws_url)
    except (ValueError, AttributeError, TypeError):
        return None
    scheme = {"ws": "http", "wss": "https",
              "http": "http", "https": "https"}.get(parts.scheme)
    if not scheme or not parts.netloc:
        return None
    return urlunsplit((scheme, parts.netloc, "", "", ""))


def fetch_json(url: str, timeout: float = 5.0) -> dict | None:
    """GET a JSON endpoint. None on ANY failure (non-200, bad JSON,
    timeout, no route) — callers treat None as 'endpoint unavailable'.
    TLS trust comes from the vendored certifi via the SSL_CERT_FILE env
    demon_ext sets at boot."""
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def load_local_contract(vendor_root: str | None) -> dict | None:
    """Read the vendored contract shipped next to the .tox. None when the
    bundle has no contract (or it's unreadable) — the check then skips."""
    if not vendor_root:
        return None
    path = os.path.join(vendor_root, CONTRACT_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _names(d: Any) -> set[str]:
    return set(d) if isinstance(d, dict) else set()


def diff_contract(local: dict, remote_protocol: dict,
                  remote_knobs: dict | None = None) -> list[str]:
    """Vocabulary diff between this build's contract and the live pod's.

    Returns sorted human-readable drift lines; empty list = in sync.
    `remote_knobs` is the plain `/api/knobs` payload (ODE mode — the
    no-query default), compared against the local ODE catalog.
    """
    lines: list[str] = []
    lp = local.get("protocol", {})

    lv, rv = lp.get("version"), remote_protocol.get("version")
    if lv != rv and rv is not None:
        lines.append(f"protocol version: pod has v{rv}, this build "
                     f"expects v{lv}")

    pairs = [
        ("command", _names(lp.get("commands")),
         _names(remote_protocol.get("commands")),
         "this build can't send it", "this build still sends it"),
        ("event", _names(lp.get("events")),
         _names(remote_protocol.get("events")),
         "unhandled by this build", "this build still expects it"),
        ("config field", _names(lp.get("config")),
         _names(remote_protocol.get("config")),
         "this build never sends it", "this build may still send it"),
    ]
    if remote_knobs is not None:
        lk = _names(local.get("knobs", {}).get("ode"))
        rk = _names(remote_knobs.get("knobs"))
        # Per-session lora_str_<id> knobs appear in the live manifest
        # but never in the static catalog — not drift.
        rk = {k for k in rk if not k.startswith("lora_str_")}
        if lk and rk:
            pairs.append(("knob", lk, rk,
                          "this build doesn't stream it",
                          "the server dropped it"))
        kv = remote_knobs.get("version")
        klv = local.get("knobs", {}).get("version")
        if kv is not None and kv != klv:
            lines.append(f"knob schema version: pod has v{kv}, this "
                         f"build expects v{klv}")

    for label, loc, rem, added_note, removed_note in pairs:
        if not loc or not rem:
            continue  # malformed side — don't fabricate drift
        for name in sorted(rem - loc):
            lines.append(f"server added {label} '{name}' ({added_note})")
        for name in sorted(loc - rem):
            lines.append(f"server dropped {label} '{name}' ({removed_note})")

    return lines


def run_check(ws_url: str, vendor_root: str | None,
              timeout: float = 5.0) -> list[str] | None:
    """The whole check, designed to run on a fire-and-forget thread.

    Returns None when the check could not run (no local contract, no
    /api/protocol on the pod), else the (possibly empty) drift lines.
    Never raises."""
    try:
        local = load_local_contract(vendor_root)
        if local is None:
            return None
        base = http_base_from_ws_url(ws_url)
        if base is None:
            return None
        remote_protocol = fetch_json(f"{base}/api/protocol", timeout=timeout)
        if remote_protocol is None:
            return None
        remote_knobs = fetch_json(f"{base}/api/knobs", timeout=timeout)
        return diff_contract(local, remote_protocol, remote_knobs)
    except Exception:
        return None

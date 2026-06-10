#!/usr/bin/env python3
"""
End-to-end canary against a live DEMON server on vast.ai.

What this catches that the static drift check can't:
  * Default-value flips (e.g. server changes slice compression default
    from `none` to `zstd` without a type change).
  * Behavioral changes (server now sends an extra binary blob per slice,
    rebuilds the initial buffer with a different layout, etc.).
  * Mis-encoded SessionConfig fields that the server silently rejects.

What this is NOT:
  * A unit test. It's a smoke test. Failures need eyeballs.
  * Free. Each run spawns an RTX 5090 vast.ai instance for ~5 minutes
    (~$0.10). Don't put it on a schedule.

Flow:
  1. `vastai search offers ...` -> pick cheapest RTX 5090 offer.
  2. `vastai create instance <id>` with the demon-pod:warm image.
  3. Poll `vastai logs <instance>` until `Starting HTTP+WS on :8765`.
  4. Resolve the direct-port WS URL via `vastai show instance`.
  5. Open WS, send minimal SessionConfig + 1-sec silence audio.
  6. Receive for ~30 s; assert every JSON `type` is known and every
     slice has flags in {SLICE_FLAG_RAW, SLICE_FLAG_DELTA}.
  7. `vastai destroy instance <id>` -- ALWAYS, even on failure.

Prereqs:
  * `vastai` CLI on PATH and authenticated (VASTAI_API_KEY env var or
    `vastai set api-key`).
  * pip install: websocket-client, numpy, zstandard.

Usage:
  python scripts/canary.py
  python scripts/canary.py --keep-pod   # leave the instance running on
                                        # failure so you can inspect it

Exit:
  0 -- canary passed
  1 -- canary failed (drift / connect failure / unhandled message)
  2 -- usage / infrastructure error (no offers, vastai CLI missing, ...)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Reuse our own protocol code -- no TouchDesigner imports, so this works
# headless.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import wire  # noqa: E402

import numpy as np  # noqa: E402

try:
    import websocket  # websocket-client
except ImportError:
    print("error: pip install websocket-client", file=sys.stderr)
    sys.exit(2)


# ----------------------------------------------------------------------------
# vast.ai helpers
# ----------------------------------------------------------------------------

# Cheapest verified RTX 5090, decent network, recent-driver.
SEARCH_FILTER = (
    "gpu_name=RTX_5090 num_gpus=1 reliability>=0.99 "
    "cuda_max_good>=12.4 verified=true rentable=true "
    "direct_port_count>=2 inet_down>=1500"
)

# The DEMON public demo's warm image. Boots in ~30 s and runs the WS
# server on :8765.
DEMON_IMAGE = "daydreamlive/demon-pod:warm"
WS_PORT = 8765


def vastai(*args: str, capture: bool = True, check: bool = True) -> str:
    """Run a `vastai <subcommand>` and return stdout."""
    cmd = ["vastai", *args]
    res = subprocess.run(cmd, capture_output=capture, text=True, check=False)
    if check and res.returncode != 0:
        raise RuntimeError(
            f"`{' '.join(cmd)}` failed (exit={res.returncode}):\n"
            f"  stdout: {res.stdout.strip()}\n"
            f"  stderr: {res.stderr.strip()}"
        )
    return res.stdout


def pick_offer() -> str:
    """Find the cheapest offer matching SEARCH_FILTER."""
    out = vastai("search", "offers", SEARCH_FILTER, "-o", "dph+", "--raw")
    offers = json.loads(out)
    if not offers:
        raise RuntimeError(f"no vast.ai offers matched: {SEARCH_FILTER}")
    # `--raw` returns dicts with `id` (the askable offer id).
    return str(offers[0]["id"])


def create_instance(offer_id: str) -> str:
    """Create an instance from `offer_id`, return the instance id."""
    out = vastai(
        "create", "instance", offer_id,
        "--image", DEMON_IMAGE,
        "--disk", "40",
        "--direct",
        "--raw",
    )
    res = json.loads(out)
    if not res.get("success"):
        raise RuntimeError(f"create instance failed: {res}")
    return str(res["new_contract"])


def wait_for_ready(instance_id: str, timeout_s: int = 600) -> dict[str, Any]:
    """Poll until the pod logs `Starting HTTP+WS on :8765`. Return the
    instance dict (host, ports, etc.)."""
    deadline = time.monotonic() + timeout_s
    last_status = ""
    while time.monotonic() < deadline:
        out = vastai("show", "instance", instance_id, "--raw")
        inst = json.loads(out)
        status = inst.get("actual_status", "")
        if status != last_status:
            print(f"  [vast] status={status}")
            last_status = status
        if status == "running":
            # Pod is up; check the WS server is actually listening by
            # scraping logs. `vastai logs <id>` writes a file; we read
            # its tail.
            try:
                logs = vastai("logs", instance_id, "--tail", "200")
            except Exception:
                logs = ""
            if "Starting HTTP+WS on :8765" in logs:
                return inst
        time.sleep(15)
    raise RuntimeError(
        f"timed out after {timeout_s}s waiting for instance {instance_id} "
        f"to start serving (last status={last_status})"
    )


def resolve_ws_url(instance: dict[str, Any]) -> str:
    """Pull the direct-port WS URL out of an instance dict."""
    host = instance.get("public_ipaddr") or instance.get("public_ip")
    if not host:
        raise RuntimeError(f"no public ip on instance: {instance}")
    # Direct ports map internal -> external. WS server listens on 8765.
    ports = instance.get("ports") or {}
    mapping = ports.get(f"{WS_PORT}/tcp") or []
    if not mapping:
        raise RuntimeError(
            f"no external port mapping for {WS_PORT}/tcp; got ports={ports}"
        )
    ext_port = mapping[0]["HostPort"]
    return f"ws://{host}:{ext_port}/"


def destroy_instance(instance_id: str) -> None:
    print(f"  [vast] destroying instance {instance_id}")
    try:
        vastai("destroy", "instance", instance_id, check=False)
    except Exception as e:
        print(f"  [vast] destroy WARNING: {e}", file=sys.stderr)


# ----------------------------------------------------------------------------
# Canary
# ----------------------------------------------------------------------------

# `kind` values demonTD handles (events.EVENT_HANDLERS — the same table
# _on_text dispatches through) plus the whitelisted intentionally-ignored
# events. Derived, not hand-copied: contract CI keeps EVENT_HANDLERS
# aligned with the server registry, and this inherits it.
import events as _events  # noqa: E402

with open(ROOT / "contracts" / "parity_whitelist.json") as _f:
    _IGNORED_EVENTS = set(json.load(_f).get("events_ignored", {}))

KNOWN_SERVER_KINDS = (
    set(_events.EVENT_HANDLERS)
    | _IGNORED_EVENTS
    | {"_invalid"}  # decode_control's marker for non-dict JSON
)

# Slice flags we accept. Anything else triggers FAIL.
KNOWN_SLICE_FLAGS = {wire.SLICE_FLAG_RAW, wire.SLICE_FLAG_DELTA}


def build_minimal_config() -> dict[str, Any]:
    """Match demon-public-demo's buildConfig() with neutral values."""
    return {
        "sde": False,
        "lora": True,
        "depth": 4,
        "vae_window": 6.0,
        "crop": 0.0,
        "steps": 8,
        "fast_vae": False,
        "walk_window": False,
        "walk_window_s": 60.0,
        "enabled_loras": [],
        "prompt": "ambient pad",
        "lora_strengths": {},
        "fixture_name": "",
        # Force the raw path — we don't want zstd in the canary so we can
        # decode without an extra dependency on the canary side. (zstd is
        # still imported above, just not used here.)
        "compression": "none",
    }


def build_silence_pcm(seconds: float = 1.0, channels: int = 2) -> np.ndarray:
    """1 second of silence at SAMPLE_RATE, shape (channels, samples)."""
    n = int(wire.SAMPLE_RATE * seconds)
    return np.zeros((channels, n), dtype=np.float32)


def run_canary(ws_url: str, duration_s: float = 30.0) -> None:
    """Connect, send config + audio, listen, assert. Raises on failure."""
    print(f"  [canary] connecting to {ws_url}")
    ws = websocket.create_connection(ws_url, timeout=30)

    cfg = build_minimal_config()
    print(f"  [canary] sending SessionConfig (compression=none)")
    ws.send(wire.encode_config(cfg))

    silence = build_silence_pcm(seconds=1.0, channels=2)
    print(f"  [canary] sending 1-sec silence audio "
          f"({silence.shape[1]} frames stereo)")
    ws.send_binary(wire.encode_audio_frame(silence, channels=2))

    seen_kinds: set[str] = set()
    seen_flags: set[int] = set()
    n_slices = 0
    n_text = 0
    n_binary = 0
    got_initial_buffer = False
    errors: list[str] = []

    deadline = time.monotonic() + duration_s
    ws.settimeout(2.0)
    while time.monotonic() < deadline:
        try:
            msg = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        except Exception as e:
            errors.append(f"WS recv exception: {e}")
            break

        if isinstance(msg, (bytes, bytearray)):
            n_binary += 1
            # The first binary frame after `ready` is the initial buffer
            # (raw float32 PCM, no slice header). Subsequent are slices.
            if not got_initial_buffer:
                got_initial_buffer = True
                print(f"  [canary] initial buffer: {len(msg)} bytes")
                continue
            try:
                slc = wire.decode_slice(bytes(msg))
            except Exception as e:
                errors.append(f"decode_slice failed: {e}")
                continue
            seen_flags.add(slc.flags)
            n_slices += 1
        else:
            n_text += 1
            try:
                data = wire.decode_control(msg)
            except Exception as e:
                errors.append(f"decode_control failed: {e}; raw={msg[:200]}")
                continue
            kind = data.get("type", "_invalid")
            seen_kinds.add(kind)
            if kind == "ready":
                print(f"  [canary] ready: {json.dumps(data)[:200]}")

    ws.close()

    # ---- assertions ----------------------------------------------------
    print()
    print(f"  [canary] received {n_text} text + {n_binary} binary "
          f"({n_slices} slices)")
    print(f"  [canary] kinds:  {sorted(seen_kinds) or '(none)'}")
    print(f"  [canary] flags:  {sorted(seen_flags) or '(none)'}")
    print(f"  [canary] errors: {errors or '(none)'}")

    if errors:
        raise AssertionError("decode errors during canary; see log above")
    if "ready" not in seen_kinds:
        raise AssertionError("server never sent `ready` JSON")
    if not got_initial_buffer:
        raise AssertionError("server never sent the initial-buffer frame")
    if n_slices == 0:
        raise AssertionError("server never sent any slice frames")

    unknown_kinds = seen_kinds - KNOWN_SERVER_KINDS
    unknown_flags = seen_flags - KNOWN_SLICE_FLAGS
    if unknown_kinds:
        raise AssertionError(
            f"server sent unhandled JSON `type`s: {sorted(unknown_kinds)} "
            f"-- run `python scripts/sync_contract.py` then "
            f"`PYTHONPATH=src pytest tests/test_contract.py` for details."
        )
    if unknown_flags:
        # Server flag bits beyond 0/1 (e.g. the 0x07 stem flag) ought to
        # be safe to *ignore* in production, but they ARE drift -- the
        # next breaking change might arrive on those bits.
        raise AssertionError(
            f"server sent unknown slice flags: "
            f"{[hex(f) for f in sorted(unknown_flags)]}"
        )


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep-pod", action="store_true",
        help="On failure, leave the vast instance running so you can "
             "inspect it. Default: destroy on every exit path.",
    )
    parser.add_argument(
        "--duration", type=float, default=30.0,
        help="How long to listen on the WS (seconds). Default: 30",
    )
    args = parser.parse_args()

    # Sanity-check the CLI is there.
    try:
        vastai("--version", capture=True, check=False)
    except FileNotFoundError:
        print("error: `vastai` CLI not found. `pip install vastai`.",
              file=sys.stderr)
        return 2

    api_key = os.environ.get("VASTAI_API_KEY", "")
    if api_key:
        vastai("set", "api-key", api_key, check=False)

    instance_id: str | None = None
    try:
        print("[canary] picking cheapest RTX 5090 offer...")
        offer_id = pick_offer()
        print(f"[canary] offer={offer_id}")

        print("[canary] creating instance...")
        instance_id = create_instance(offer_id)
        print(f"[canary] instance={instance_id}")

        print("[canary] waiting for pod to start serving WS...")
        instance = wait_for_ready(instance_id, timeout_s=600)

        ws_url = resolve_ws_url(instance)
        print(f"[canary] ws_url={ws_url}")

        run_canary(ws_url, duration_s=args.duration)
        print()
        print("[canary] OK")
        return 0

    except Exception as e:
        print()
        print(f"[canary] FAIL: {e}", file=sys.stderr)
        return 1

    finally:
        if instance_id and not args.keep_pod:
            destroy_instance(instance_id)
        elif instance_id:
            print(f"  [vast] --keep-pod set; leaving {instance_id} running")


if __name__ == "__main__":
    sys.exit(main())

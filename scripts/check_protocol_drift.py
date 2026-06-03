#!/usr/bin/env python3
"""
Compare demonTD's local protocol surface against demon-public-demo (the
DEMON web client, which updates in lockstep with the DEMON server).

Surface monitored
-----------------
* Server -> client JSON `type` literals (do we have a `kind == "..."`
  handler for each one?)
* Client -> server JSON `type` literals (do we have an encoder for each?)
* `SLICE_FLAG_*` integer constants (do they match by name AND value?)
* Protocol constants: `SAMPLE_RATE`, `CROSSFADE_SECONDS`, `SLICE_HDR_SIZE`,
  `SEAM_FADE_SECONDS`
* `SessionConfig` fields (do we send all of them?)

Reference freshness
-------------------
By DEFAULT the reference protocol files are read from `origin/main` of the
demon-public-demo checkout (via `git fetch` + `git show origin/main:<path>`),
NOT from its working tree. This is deliberate: the working tree is often
parked on a stale `claude/sync/*` feature branch, and reading it once hid
23 commits of real backend drift behind a false "no drift" result. Override
the ref with `--ref`, or read the working tree with `--worktree`.

Usage
-----
    python scripts/check_protocol_drift.py \
        --demonTD <path>            # default: cwd
        --demon-public-demo <path>  # required
        [--ref origin/main]         # reference git ref (default)
        [--worktree]                # read reference from working tree instead
        [--no-fetch]                # skip git fetch before comparing
        [--json]                    # machine-readable output

Exit
----
* 0 -- protocol surfaces match
* 1 -- drift detected (one or more categories have new items in the
       web client that we don't handle)
* 2 -- usage / IO error

No third-party deps. Pure stdlib. Regex-based; we don't need a real
TypeScript parser for the shape of demon-public-demo's source today.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# TypeScript / JavaScript parsers
# ---------------------------------------------------------------------------

# Inside an `interface XxxMessage { type: "literal"; ... }`, capture "literal".
TS_TYPE_LITERAL_RE = re.compile(r'^\s*type:\s*"(\w+)"', re.MULTILINE)

# Client-side JSON sends: ws.send(JSON.stringify({ type: "literal", ... }))
TS_CLIENT_SEND_RE = re.compile(
    r'(?:JSON\.stringify\s*\(\s*\{|\{\s*)\s*type:\s*"(\w+)"',
)

# Indirect sends via sendAudioFrame("...", ...) — the messageType is the
# first positional arg.
TS_AUDIO_FRAME_RE = re.compile(r'sendAudioFrame\s*\(\s*"(\w+)"')

# export const SLICE_FLAG_RAW = 0; (and similar)
TS_SLICE_FLAG_RE = re.compile(r'SLICE_FLAG_(\w+)\s*=\s*(\d+)')

# export const SAMPLE_RATE = 48000;
TS_CONST_INT_RE = re.compile(
    r'(?:export\s+)?const\s+(SAMPLE_RATE|SLICE_HDR_SIZE)\s*=\s*(\d+)'
)
TS_CONST_FLOAT_RE = re.compile(
    r'(?:export\s+)?const\s+(CROSSFADE_SECONDS)\s*=\s*([\d.]+)'
)
# Constants that are intentionally NOT in wire.py because they're audio-
# playback concerns (seam crossfade lives in src/audio.py's LoopBuffer).
# We could scan audio.py instead, but the value isn't on the wire so a
# direct constant-drift check would be noise.
_TS_CONSTS_IGNORE = {"SEAM_FADE_SECONDS"}

# interface SessionConfig { ...fields... }
TS_INTERFACE_BLOCK_RE = re.compile(
    r'interface\s+SessionConfig\s*\{([^}]+)\}', re.DOTALL
)
TS_FIELD_NAME_RE = re.compile(r'^\s*(\w+)\??:', re.MULTILINE)


def _strip_line_comments(src: str) -> str:
    """Strip `//` line comments from a TS/JS source string.

    Conservative: only strips comments where `//` appears in the leading
    indentation (so we don't munge URLs like `http://...` inside strings).
    """
    out = []
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("//"):
            continue
        out.append(line)
    return "\n".join(out)


def parse_ts_protocol(types_protocol_ts: str,
                      engine_protocol_ts: str,
                      audio_worklet_js: str) -> dict:
    """Extract the protocol surface from demon-public-demo source files."""

    # Strip `//` line comments first — otherwise prose like
    # `// server replies with JSON {type: "ready", ...}` is picked up as a
    # client send.
    types_protocol_ts = _strip_line_comments(types_protocol_ts)
    engine_protocol_ts = _strip_line_comments(engine_protocol_ts)
    audio_worklet_js = _strip_line_comments(audio_worklet_js)

    # Server -> client message `type` literals appear inside MessageType
    # interface declarations in types/protocol.ts.
    server_types = set(TS_TYPE_LITERAL_RE.findall(types_protocol_ts))

    # Client -> server message types appear as JSON.stringify({ type: "..." })
    # and as sendAudioFrame("...", ...) calls in engine/protocol.ts.
    client_types = set(TS_CLIENT_SEND_RE.findall(engine_protocol_ts))
    client_types |= set(TS_AUDIO_FRAME_RE.findall(engine_protocol_ts))
    # Drop noise like { type: "module" } (AudioWorkletNode options).
    client_types -= {"module"}
    # Drop server-message types that also appear (in TS examples / comments
    # we couldn't strip). These are documented as server -> client, never
    # sent by the client.
    client_types -= server_types

    # Slice flag constants.
    slice_flags = {name: int(val)
                   for name, val in TS_SLICE_FLAG_RE.findall(types_protocol_ts)}

    # Numeric protocol constants. Combine both files. We only track
    # constants that travel on the wire (SAMPLE_RATE, SLICE_HDR_SIZE,
    # CROSSFADE_SECONDS). SEAM_FADE_SECONDS is excluded — it lives in the
    # client-side worklet, not on the wire.
    consts: dict[str, float] = {}
    for src in (types_protocol_ts, engine_protocol_ts, audio_worklet_js):
        for name, val in TS_CONST_INT_RE.findall(src):
            if name in _TS_CONSTS_IGNORE:
                continue
            consts.setdefault(name, int(val))
        for name, val in TS_CONST_FLOAT_RE.findall(src):
            if name in _TS_CONSTS_IGNORE:
                continue
            consts.setdefault(name, float(val))

    # SessionConfig fields. The interface declaration is in types/protocol.ts.
    session_fields: set[str] = set()
    m = TS_INTERFACE_BLOCK_RE.search(types_protocol_ts)
    if m:
        for line in m.group(1).splitlines():
            # Skip lines that are inside /** ... */ doc comments — they look
            # like ` * ... ` and would trip the field regex.
            stripped = line.lstrip()
            if (stripped.startswith("*") or stripped.startswith("//")
                    or stripped.startswith("/*")):
                continue
            for field in TS_FIELD_NAME_RE.findall(line):
                # Reject the catch-all index signature `[k: string]`.
                if field.startswith("["):
                    continue
                session_fields.add(field)

    return {
        "server_types": server_types,
        "client_types": client_types,
        "slice_flags": slice_flags,
        "consts": consts,
        "session_fields": session_fields,
    }


# ---------------------------------------------------------------------------
# Python parsers (our local protocol surface)
# ---------------------------------------------------------------------------

# Find all `kind == "literal"` and `kind in (...)` in _on_text in demon_ext.py.
PY_KIND_EQ_RE = re.compile(r'kind\s*==\s*"(\w+)"')
PY_KIND_IN_RE = re.compile(r'kind\s+in\s+\(([^)]+)\)')

# Find encode_<name> functions in wire.py.
PY_ENCODER_RE = re.compile(r'^def\s+encode_(\w+)\s*\(', re.MULTILINE)

# Find SLICE_FLAG_* constants in wire.py.
PY_SLICE_FLAG_RE = re.compile(r'SLICE_FLAG_(\w+)\s*:\s*int\s*=\s*(\d+)')

# Find protocol constants in wire.py.
PY_CONST_RE = re.compile(
    r'^(SAMPLE_RATE|SLICE_HDR_SIZE|CROSSFADE_SECONDS|SEAM_FADE_SECONDS|T)\s*:\s*\w+\s*=\s*([\d.]+)',
    re.MULTILINE,
)

# Keys passed into the cfg dict built by _build_session_config.
# Match `"key":` at the start of a (whitespace-stripped) line. We don't
# constrain the value — it can be a function call like
# `bool(init_val("Sde", False))` containing commas, which an
# `[^,]+`-style regex would miss.
PY_CFG_KEY_RE = re.compile(r'^\s*"([a-z_][a-z0-9_]*)"\s*:', re.MULTILINE)

# SessionConfig fields that demon-public-demo's TS interface lists but
# the JS client deliberately does NOT send unconditionally (server
# resolves them from fixture sidecar / detection; sending stale dropdown
# values would regress server-side detection), or that the demonTD
# operator has no UX for and chooses to omit.
# Source: demon-public-demo/vendor/demon-ui/hooks/useStartSession.ts
# buildConfig() — see its docstrings for the per-field rationale.
_TS_SESSION_FIELDS_INTENTIONALLY_OMITTED = {
    # Server fills these from sidecar / CNN detection. Sending the TD
    # dropdown's stale value would override that — same regression the
    # JS client documents.
    "key", "time_signature",
    # Conditional in the JS client too: only sent when the user uploads
    # a custom track and selects a stem mode. demonTD has no stems UX
    # in v0.2; omit unconditionally to match the JS default-fixture
    # path.
    "stem_source_mode",
}

# Client-side `type` literals demonTD knowingly doesn't encode. These are
# UX features in the web client that demonTD has no equivalent for yet;
# the drift script would otherwise flag them every run.
# Source: demon-public-demo/vendor/demon-ui/engine/protocol.ts
_TS_CLIENT_TYPES_INTENTIONALLY_OMITTED = {
    # Runtime depth retune. Depth is Init-only in TD (immutable while
    # connected); adding live retune is a UX feature, not a parity gap.
    "set_depth",
    # Mirrors a UI-controlled loop band to the server. demonTD's
    # LoopBuffer does its own seam crossfade locally; the band isn't
    # exposed as a TD parameter. Add an encoder + Loopbandstart/end
    # pars to revisit.
    "loop_band",
}


def _slice_function_body(src: str, func_name: str) -> str:
    """Return the body of `def func_name(self, ...)` in `src` as a substring.

    Naive: finds the def, then captures until the next top-level def or
    class declaration (or end of file). Good enough for our purposes — we
    just want a substring to regex over.
    """
    m = re.search(rf'(\s*def\s+{func_name}\s*\(.*?\):.*?)(?=\n    def\s|\nclass\s|\Z)',
                  src, re.DOTALL)
    return m.group(1) if m else ""


def parse_py_protocol(demon_ext_py: str, wire_py: str) -> dict:
    """Extract the protocol surface from our Python source."""

    # Server message kinds dispatched in _on_text.
    on_text = _slice_function_body(demon_ext_py, "_on_text")
    server_kinds = set(PY_KIND_EQ_RE.findall(on_text))
    for tuple_body in PY_KIND_IN_RE.findall(on_text):
        for s in re.findall(r'"(\w+)"', tuple_body):
            server_kinds.add(s)

    # Client encoders in wire.py: encode_<x> -> we send <x>.
    encoders = set(PY_ENCODER_RE.findall(wire_py))
    # encode_audio_frame, encode_config, encode_params, encode_prompt, ...
    # Filter to ones that look like message-type encoders. encode_params
    # corresponds to {"type": "params"} on the wire, encode_audio_frame is
    # the binary frame (not a JSON message), encode_config is the
    # SessionConfig (also not a message). Map encode_X -> wire-name.
    encoder_to_wire = {
        "params": "params",
        "prompt": "prompt",
        "set_prompt_blend": "set_prompt_blend",
        "enable_lora": "enable_lora",
        "disable_lora": "disable_lora",
        "set_timbre_strength": "set_timbre_strength",
        "set_timbre_source": "set_timbre_source",
        "set_timbre_fixture": "set_timbre_fixture",
        "clear_timbre_source": "clear_timbre_source",
        "set_structure_source": "set_structure_source",
        "set_structure_fixture": "set_structure_fixture",
        "clear_structure_source": "clear_structure_source",
        "swap_source": "swap_source",
        "set_interp_method": "set_interp_method",
    }
    client_types_we_send: set[str] = set()
    for enc in encoders:
        if enc in encoder_to_wire:
            client_types_we_send.add(encoder_to_wire[enc])

    # Slice flags.
    slice_flags = {name: int(val)
                   for name, val in PY_SLICE_FLAG_RE.findall(wire_py)}

    # Protocol constants.
    consts: dict[str, float] = {}
    for name, val in PY_CONST_RE.findall(wire_py):
        consts[name] = float(val) if "." in val else int(val)

    # SessionConfig fields we actually send. Pulled from the cfg dict in
    # _build_session_config — match `"key": ...` lines inside that body.
    build_cfg = _slice_function_body(demon_ext_py, "_build_session_config")
    session_fields = set(PY_CFG_KEY_RE.findall(build_cfg))
    # Strip the catch-all keys that are not real config fields.
    session_fields -= {"compression"}  # we set this conditionally; not in TS

    return {
        "server_kinds": server_kinds,
        "client_types_we_send": client_types_we_send,
        "slice_flags": slice_flags,
        "consts": consts,
        "session_fields": session_fields,
    }


# ---------------------------------------------------------------------------
# Drift report
# ---------------------------------------------------------------------------

@dataclass
class DriftItem:
    category: str
    description: str
    items: list


def compute_drift(ts: dict, py: dict) -> list[DriftItem]:
    """Compare TS surface against PY surface. Returns a list of drift items.

    'Drift' here means: TS has something we don't. We don't flag the
    reverse (us-only items) because that's not a server protocol concern
    -- it just means we left a TD-specific feature in.
    """
    drift: list[DriftItem] = []

    # Server messages: TS has, we don't dispatch.
    new_server = sorted(ts["server_types"] - py["server_kinds"])
    if new_server:
        drift.append(DriftItem(
            "server_message_types",
            "Server emits these `type` values; we have no `kind == ...` handler",
            new_server,
        ))

    # Client messages: TS sends, we don't. Filter out the deliberately
    # not-implemented set (see _TS_CLIENT_TYPES_INTENTIONALLY_OMITTED).
    new_client = sorted(ts["client_types"]
                        - py["client_types_we_send"]
                        - _TS_CLIENT_TYPES_INTENTIONALLY_OMITTED)
    if new_client:
        drift.append(DriftItem(
            "client_message_types",
            "Web client sends these `type` values; we have no encoder for them",
            new_client,
        ))

    # Slice flags: TS has named value, we don't (or differ).
    flag_drift = []
    for name, ts_val in ts["slice_flags"].items():
        py_val = py["slice_flags"].get(name)
        if py_val is None:
            flag_drift.append(f"{name}={ts_val} (not in wire.py)")
        elif py_val != ts_val:
            flag_drift.append(f"{name}={ts_val} (wire.py has {py_val})")
    if flag_drift:
        drift.append(DriftItem(
            "slice_flags",
            "SLICE_FLAG_* mismatch",
            flag_drift,
        ))

    # Constants.
    const_drift = []
    for name, ts_val in ts["consts"].items():
        py_val = py["consts"].get(name)
        if py_val is None:
            const_drift.append(f"{name}={ts_val} (not in wire.py)")
        elif py_val != ts_val:
            const_drift.append(f"{name}={ts_val} (wire.py has {py_val})")
    if const_drift:
        drift.append(DriftItem(
            "protocol_constants",
            "Numeric constant mismatch",
            const_drift,
        ))

    # SessionConfig fields: TS has, we don't send. Filter out fields that
    # the JS client itself deliberately omits (see
    # _TS_SESSION_FIELDS_INTENTIONALLY_OMITTED).
    new_fields = sorted(ts["session_fields"]
                        - py["session_fields"]
                        - _TS_SESSION_FIELDS_INTENTIONALLY_OMITTED)
    if new_fields:
        drift.append(DriftItem(
            "session_config_fields",
            "SessionConfig fields in TS that we don't send",
            new_fields,
        ))

    return drift


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _git_short_sha(path: Path, ref: str = "HEAD") -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", ref],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except Exception:
        return "?"


def _git_fetch(path: Path) -> bool:
    """Best-effort `git fetch` so origin/<branch> reflects the true remote.
    Returns True on success. Never raises — offline is non-fatal (we warn
    and fall back to whatever the local ref already points at)."""
    try:
        subprocess.run(
            ["git", "-C", str(path), "fetch", "--quiet", "origin"],
            capture_output=True, text=True, check=True, timeout=60,
        )
        return True
    except Exception as e:
        print(f"warning: `git fetch` in {path} failed ({e}); comparing "
              f"against the local ref as-is (may be stale).", file=sys.stderr)
        return False


def _read_ref_file(repo: Path, ref: str, relpath: str) -> str:
    """Read <relpath> from a git <ref> (e.g. 'origin/main') WITHOUT touching
    the working tree / current branch. This is what makes drift detection
    immune to the local checkout sitting on a stale feature branch — the
    exact trap that once hid 23 commits of backend drift."""
    out = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{relpath}"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demonTD", default=".",
                        help="Path to demon-td checkout (default: cwd)")
    parser.add_argument("--demon-public-demo", required=True,
                        help="Path to demon-public-demo checkout")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON instead of text")
    parser.add_argument(
        "--ref", default="origin/main",
        help="Git ref in demon-public-demo to compare against (default: "
             "origin/main). The reference protocol files are read from THIS "
             "ref via `git show`, NOT from the working tree — so a local "
             "checkout sitting on a stale feature branch can't hide drift.")
    parser.add_argument(
        "--worktree", action="store_true",
        help="Read demon-public-demo files from the working tree instead of "
             "--ref (legacy behavior; only use when diffing uncommitted local "
             "edits to the reference).")
    parser.add_argument(
        "--no-fetch", action="store_true",
        help="Skip `git fetch` before comparing (default fetches origin so "
             "--ref reflects the true remote).")
    args = parser.parse_args()

    td_root = Path(args.demonTD).resolve()
    dpd_root = Path(args.demon_public_demo).resolve()

    # demonTD files always come from the local working tree — that's the
    # code under test (we WANT local changes). demon-public-demo (the
    # reference) is read from --ref by default.
    td_files = {
        "demon_ext_py": td_root / "src/demon_ext.py",
        "wire_py":      td_root / "src/wire.py",
    }
    for label, p in td_files.items():
        if not p.is_file():
            print(f"error: missing source file ({label}): {p}", file=sys.stderr)
            return 2

    dpd_rel = {
        "types_protocol_ts":  "vendor/demon-ui/types/protocol.ts",
        "engine_protocol_ts": "vendor/demon-ui/engine/protocol.ts",
        "audio_worklet_js":   "public/audio-worklet.js",
    }

    if args.worktree:
        # Legacy: read the reference straight from the working tree.
        dpd_sha = _git_short_sha(dpd_root)
        try:
            dpd_src = {k: (dpd_root / rel).read_text()
                       for k, rel in dpd_rel.items()}
        except OSError as e:
            print(f"error: reading demon-public-demo working tree: {e}",
                  file=sys.stderr)
            return 2
        # Loud warning if the working tree is behind the remote.
        if not args.no_fetch:
            _git_fetch(dpd_root)
        behind = subprocess.run(
            ["git", "-C", str(dpd_root), "rev-list", "--count",
             f"HEAD..{args.ref}"],
            capture_output=True, text=True).stdout.strip()
        if behind and behind != "0":
            print(f"warning: demon-public-demo working tree is {behind} "
                  f"commits behind {args.ref}; --worktree results may be "
                  f"stale. Drop --worktree to diff {args.ref} directly.",
                  file=sys.stderr)
    else:
        # Default + recommended: read the reference from --ref (origin/main).
        if not args.no_fetch:
            _git_fetch(dpd_root)
        dpd_sha = _git_short_sha(dpd_root, args.ref)
        if dpd_sha == "?":
            print(f"error: ref '{args.ref}' not found in {dpd_root}. Use "
                  f"--ref <branch> or --worktree.", file=sys.stderr)
            return 2
        try:
            dpd_src = {k: _read_ref_file(dpd_root, args.ref, rel)
                       for k, rel in dpd_rel.items()}
        except subprocess.CalledProcessError as e:
            print(f"error: reading {args.ref} from demon-public-demo: "
                  f"{e.stderr or e}", file=sys.stderr)
            return 2
        # Informational: note when the checkout differs from the compared ref.
        head_sha = _git_short_sha(dpd_root)
        if head_sha != dpd_sha:
            print(f"note: comparing against {args.ref} ({dpd_sha}); local "
                  f"checkout HEAD is {head_sha}. Reference read from the ref, "
                  f"not the working tree.", file=sys.stderr)

    ts = parse_ts_protocol(
        dpd_src["types_protocol_ts"],
        dpd_src["engine_protocol_ts"],
        dpd_src["audio_worklet_js"],
    )
    py = parse_py_protocol(
        td_files["demon_ext_py"].read_text(),
        td_files["wire_py"].read_text(),
    )
    drift = compute_drift(ts, py)

    report = {
        "demon_public_demo_sha": dpd_sha,
        "demon_public_demo_ref": (args.ref if not args.worktree
                                  else "(worktree)"),
        "demonTD_sha": _git_short_sha(td_root),
        "ts_surface": {
            "server_types": sorted(ts["server_types"]),
            "client_types": sorted(ts["client_types"]),
            "slice_flags": ts["slice_flags"],
            "consts": ts["consts"],
            "session_fields": sorted(ts["session_fields"]),
        },
        "py_surface": {
            "server_kinds": sorted(py["server_kinds"]),
            "client_types_we_send": sorted(py["client_types_we_send"]),
            "slice_flags": py["slice_flags"],
            "consts": py["consts"],
            "session_fields": sorted(py["session_fields"]),
        },
        "drift": [
            {"category": d.category, "description": d.description,
             "items": d.items}
            for d in drift
        ],
    }

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        print(f"== protocol drift report ==")
        print(f"  demon-public-demo: {report['demon_public_demo_sha']} "
              f"({report['demon_public_demo_ref']})")
        print(f"  demonTD:           {report['demonTD_sha']}")
        print()
        if not drift:
            print("OK: no drift detected.")
        else:
            for d in drift:
                print(f"[{d.category}] {d.description}")
                for it in d.items:
                    print(f"  + {it}")
                print()
            print(f"== {len(drift)} drift {'item' if len(drift)==1 else 'categories'} ==")

    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())

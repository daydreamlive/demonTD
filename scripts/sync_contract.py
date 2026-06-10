#!/usr/bin/env python3
"""
Sync DEMON's authoritative wire contract into vendor/demon_contract.json.

The DEMON backend is self-describing: `demos/realtime_motion_graph_web/
protocol.py :: wire_contract()` is the registry every other surface is
generated from (the TS SDK types, the /api/protocol endpoint), and
`acestep/streaming/knobs.py :: knob_catalog()` is the knob manifest behind
/api/knobs. This script extracts both — plus the UI-side parity data demonTD
mirrors (canonical labels, the loraTriggers.ts source hash, the web
installation's engine defaults) — from a git ref of the DEMON repo into ONE
committed JSON artifact. The contract tests (tests/test_contract.py) then
compare demonTD's real surface against that artifact with plain imports;
no TypeScript regex-scraping at test time, no demon-public-demo at all.

Reference freshness
-------------------
Files are read from `--ref` (default origin/main) via `git show`, NEVER from
the DEMON working tree. Same hard-won rule as the old drift checker: a local
checkout parked on a stale feature branch must not be able to hide drift.

Usage
-----
    python3 scripts/sync_contract.py
        [--demon PATH]      # default: $DEMON_REPO or ~/git/DEMON
        [--ref origin/main] # git ref to read via `git show`
        [--no-fetch]        # skip `git fetch origin` first
        [--out PATH]        # default: vendor/demon_contract.json
        [--python EXE]      # interpreter for the dumper subprocess
                            # (needs numpy; auto-detected if omitted)
        [--check]           # compare only; exit 1 if the committed
                            # artifact is stale, write nothing

Exit
----
* 0 -- artifact written / already current
* 1 -- (--check only) committed artifact is stale
* 2 -- extraction error: missing files, dumper import failure, zero
       labels parsed. ALWAYS loud — if DEMON restructures, this script
       fails by name, it never goes silently blind.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "vendor" / "demon_contract.json"
ARTIFACT_VERSION = 1

# ---------------------------------------------------------------------------
# Upstream file manifest. Each entry lists candidate paths tried in order —
# when DEMON restructures, the fix is adding the new path at the front.
# A total miss is a loud exit 2 naming every path tried.
# ---------------------------------------------------------------------------

# Python modules the dumper subprocess imports (tempdir package layout).
PY_FILES: dict[str, list[str]] = {
    "demos/realtime_motion_graph_web/protocol.py": [
        "demos/realtime_motion_graph_web/protocol.py",
    ],
    "acestep/streaming/config.py": [
        "acestep/streaming/config.py",
    ],
    "acestep/streaming/knobs.py": [
        "acestep/streaming/knobs.py",
    ],
}

# UI-side parity sources (text extraction, no node/TS toolchain).
UI_FILES: dict[str, list[str]] = {
    "slider_tile": [
        "demos/realtime_motion_graph_web/web/components/Performance/SliderTile.tsx",
    ],
    "lora_triggers": [
        "demos/realtime_motion_graph_web/web/lib/loraTriggers.ts",
    ],
    "use_start_session": [
        "demos/realtime_motion_graph_web/web/hooks/useStartSession.ts",
    ],
    "web_config": [
        "demos/realtime_motion_graph_web/web/public/config.json",
    ],
}

# Module-level constants pulled from protocol.py alongside the contract.
# Missing any of these is a hard dumper failure — they're load-bearing for
# the binary framing demonTD implements in src/wire.py.
_REQUIRED_CONSTANTS = (
    "SAMPLE_RATE", "T", "CROSSFADE_SECONDS", "SLICE_HDR_FMT",
    "SLICE_HDR_SIZE", "SLICE_FLAG_RAW", "SLICE_FLAG_DELTA",
    "PROTOCOL_VERSION",
)

# The dumper that runs inside the numpy-bearing subprocess with PYTHONPATH
# pointing at the tempdir package layout. Emits one JSON object on stdout.
_DUMPER = """\
import json, sys
from demos.realtime_motion_graph_web import protocol as P
from acestep.streaming import knobs as K
const_names = %r
missing = [n for n in const_names if not hasattr(P, n)]
if missing:
    sys.stderr.write("protocol.py is missing required constants: %%s\\n"
                     %% ", ".join(missing))
    sys.exit(3)
print(json.dumps({
    "protocol": P.wire_contract(),
    "constants": {n: getattr(P, n) for n in const_names},
    "knobs": {
        "version": K.KNOB_SCHEMA_VERSION,
        "ode": K.knob_catalog(sde=False, loras=[]),
        "sde": K.knob_catalog(sde=True, loras=[]),
    },
}, sort_keys=True))
""" % (_REQUIRED_CONSTANTS,)


# ---------------------------------------------------------------------------
# Git helpers (ported from check_protocol_drift.py — same ref-not-worktree
# discipline)
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, timeout: float = 60) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, timeout=timeout,
    )
    return out.stdout


def _git_fetch(repo: Path) -> None:
    """Best-effort fetch; offline is non-fatal (warn, compare local ref)."""
    try:
        _git(repo, "fetch", "--quiet", "origin")
    except Exception as e:
        print(f"warning: `git fetch` in {repo} failed ({e}); reading the "
              f"local ref as-is (may be stale).", file=sys.stderr)


def _read_ref_file(repo: Path, ref: str, candidates: list[str],
                   label: str) -> tuple[str, str]:
    """Read the first existing candidate path at <ref>. Returns
    (relpath, content). Exits 2 naming every tried path on a total miss."""
    for relpath in candidates:
        try:
            return relpath, _git(repo, "show", f"{ref}:{relpath}")
        except subprocess.CalledProcessError:
            continue
    print(f"error: none of the candidate paths for {label!r} exist at "
          f"{ref} in {repo}:", file=sys.stderr)
    for relpath in candidates:
        print(f"  tried {relpath}", file=sys.stderr)
    print("DEMON likely restructured — update the manifest at the top of "
          "scripts/sync_contract.py.", file=sys.stderr)
    sys.exit(2)


def _blob_sha(repo: Path, ref: str, relpath: str) -> str:
    return _git(repo, "rev-parse", f"{ref}:{relpath}").strip()


# ---------------------------------------------------------------------------
# UI-side extraction (ported from check_protocol_drift.py)
# ---------------------------------------------------------------------------

TS_LABEL_OVERRIDE_RE = re.compile(
    r'^\s*([a-z_][a-z0-9_]*)\s*:\s*"([^"]+)"\s*,?\s*$', re.MULTILINE
)


def parse_ts_label_overrides(slider_tile_tsx: str) -> dict[str, str]:
    """Extract the `{wire_key: ui_label}` map from SliderTile.tsx.

    The canonical table is `DISPLAY_NAMES` (historically also
    LABEL_OVERRIDES / LABEL_MAP / LABEL_TABLE). These labels define how a
    user FINDS a control — `hint_strength` reads "structure" in the UI,
    and a TD label of "Hint Strength" is exactly how Structure once went
    missing from the Synthesis page."""
    block_re = re.compile(
        r'(?:DISPLAY_NAMES|LABEL_OVERRIDES|LABEL_MAP|LABEL_TABLE)'
        r'[^=]*=\s*\{([^}]+)\}',
        re.DOTALL,
    )
    out: dict[str, str] = {}
    for m in block_re.finditer(slider_tile_tsx):
        for key, label in TS_LABEL_OVERRIDE_RE.findall(m.group(1)):
            out[key] = label
    return out


def _extract_ui(demon: Path, ref: str) -> dict:
    slider_path, slider_src = _read_ref_file(
        demon, ref, UI_FILES["slider_tile"], "slider_tile")
    labels = parse_ts_label_overrides(slider_src)
    if not labels:
        # The anti-silently-blind guard: the file exists but the label
        # table didn't parse — the map was renamed/reshaped. Fail loudly
        # rather than committing an artifact with an empty label set.
        print(f"error: found {slider_path} at {ref} but parsed ZERO label "
              f"overrides from it. The DISPLAY_NAMES table likely moved or "
              f"changed shape — update parse_ts_label_overrides().",
              file=sys.stderr)
        sys.exit(2)

    lt_path, lt_src = _read_ref_file(
        demon, ref, UI_FILES["lora_triggers"], "lora_triggers")
    uss_path, _ = _read_ref_file(
        demon, ref, UI_FILES["use_start_session"], "use_start_session")
    cfg_path, cfg_src = _read_ref_file(
        demon, ref, UI_FILES["web_config"], "web_config")

    try:
        web_cfg = json.loads(cfg_src)
    except json.JSONDecodeError as e:
        print(f"error: {cfg_path} at {ref} is not valid JSON: {e}",
              file=sys.stderr)
        sys.exit(2)

    def _clean(d: dict) -> dict:
        return {k: v for k, v in d.items() if not k.startswith("_")}

    engine_defaults = _clean(web_cfg.get("engine", {}))
    # The web UI's per-knob starting values — the values a user gets when
    # they open the webapp. TD parity on these is what makes the operator
    # SOUND like the web demo out of the box. `seed` lives at the top
    # level of config.json, not inside `controls`; fold it in.
    control_defaults = _clean(web_cfg.get("controls", {}))
    if "seed" in web_cfg:
        control_defaults.setdefault("seed", web_cfg["seed"])

    return {
        "labels": labels,
        "engine_defaults": engine_defaults,
        "control_defaults": control_defaults,
        "channel_ranges": _clean(web_cfg.get("channel_ranges", {})),
        "sources": {
            "slider_tile": {
                "path": slider_path,
                "blob_sha": _blob_sha(demon, ref, slider_path),
            },
            "lora_triggers": {
                "path": lt_path,
                "blob_sha": _blob_sha(demon, ref, lt_path),
                "lines": len(lt_src.splitlines()),
            },
            "use_start_session": {
                "path": uss_path,
                "blob_sha": _blob_sha(demon, ref, uss_path),
            },
        },
    }


# ---------------------------------------------------------------------------
# Python-contract extraction (subprocess import of the real registry)
# ---------------------------------------------------------------------------

def _pick_dumper_python(explicit: str | None) -> str:
    """The dumper needs numpy (protocol.py imports it at module top).
    Try the explicit choice, then this interpreter, then known venvs."""
    candidates = ([explicit] if explicit else []) + [
        sys.executable,
        str(REPO_ROOT / ".venv-test" / "bin" / "python"),
        str(REPO_ROOT / ".venv" / "bin" / "python"),
    ]
    for exe in candidates:
        if not exe or not Path(exe).exists():
            continue
        probe = subprocess.run([exe, "-c", "import numpy"],
                               capture_output=True)
        if probe.returncode == 0:
            return exe
    print("error: no interpreter with numpy found (tried: "
          + ", ".join(c for c in candidates if c)
          + "). protocol.py needs numpy at import time — pass --python "
          "or `python3 -m venv .venv-test && .venv-test/bin/pip install "
          "numpy`.", file=sys.stderr)
    sys.exit(2)


def _extract_python_contract(demon: Path, ref: str, python_exe: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="demon-contract-") as tmp:
        tmpdir = Path(tmp)
        pkg_dirs: set[Path] = set()
        for label, candidates in PY_FILES.items():
            relpath, content = _read_ref_file(demon, ref, candidates, label)
            dest = tmpdir / relpath
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
            # Stub __init__.py for every package level — stubs, not the
            # real ones, so upstream adding heavy imports to a package
            # __init__ can't break (or slow) extraction.
            d = dest.parent
            while d != tmpdir:
                pkg_dirs.add(d)
                d = d.parent
        for d in pkg_dirs:
            init = d / "__init__.py"
            if not init.exists():
                init.write_text("")

        env = {**os.environ, "PYTHONPATH": str(tmpdir)}
        proc = subprocess.run([python_exe, "-c", _DUMPER],
                              capture_output=True, text=True, env=env,
                              timeout=120)
        if proc.returncode != 0:
            print("error: contract dumper subprocess failed. If this is an "
                  "ImportError for a new sibling module, add it to PY_FILES "
                  "in scripts/sync_contract.py. Dumper stderr:",
                  file=sys.stderr)
            print(proc.stderr, file=sys.stderr)
            sys.exit(2)
        return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# Artifact assembly / comparison
# ---------------------------------------------------------------------------

def payload_of(artifact: dict) -> dict:
    """Everything except the `source` stamp — the basis for change
    detection, so a no-op sync (new SHA, same contract) never rewrites
    the file and the nightly job stays quiet."""
    return {k: v for k, v in artifact.items() if k != "source"}


def _summarize_diff(old: dict, new: dict) -> list[str]:
    """Category-level human summary of payload changes."""
    lines: list[str] = []

    def _names(payload: dict, *keys: str) -> set[str]:
        node = payload
        for k in keys:
            node = node.get(k, {}) if isinstance(node, dict) else {}
        return set(node) if isinstance(node, dict) else set()

    for label, keys in (
        ("command", ("protocol", "commands")),
        ("event", ("protocol", "events")),
        ("config field", ("protocol", "config")),
        ("ODE knob", ("knobs", "ode")),
        ("SDE knob", ("knobs", "sde")),
        ("label", ("ui", "labels")),
    ):
        added = sorted(_names(new, *keys) - _names(old, *keys))
        removed = sorted(_names(old, *keys) - _names(new, *keys))
        if added:
            lines.append(f"+ {label}s added: {', '.join(added)}")
        if removed:
            lines.append(f"- {label}s removed: {', '.join(removed)}")

    old_c = old.get("constants", {})
    new_c = new.get("constants", {})
    for name in sorted(set(old_c) | set(new_c)):
        if old_c.get(name) != new_c.get(name):
            lines.append(f"~ constant {name}: {old_c.get(name)!r} -> "
                         f"{new_c.get(name)!r}")

    def _blob(payload: dict, src: str) -> str:
        return (payload.get("ui", {}).get("sources", {})
                .get(src, {}).get("blob_sha", ""))
    if _blob(old, "lora_triggers") != _blob(new, "lora_triggers"):
        lines.append("~ loraTriggers.ts changed upstream — re-review "
                     "src/lora_triggers.py")

    if not lines:
        lines.append("(field-level changes only — see the artifact diff)")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demon",
                        default=os.environ.get("DEMON_REPO",
                                               str(Path.home() / "git/DEMON")),
                        help="Path to the DEMON checkout (default: "
                             "$DEMON_REPO or ~/git/DEMON)")
    parser.add_argument("--ref", default="origin/main",
                        help="Git ref in DEMON to read (default: origin/main; "
                             "read via `git show`, never the working tree)")
    parser.add_argument("--no-fetch", action="store_true",
                        help="Skip `git fetch origin` before reading")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help=f"Artifact path (default: {DEFAULT_OUT})")
    parser.add_argument("--python", default=None,
                        help="Interpreter for the dumper subprocess "
                             "(needs numpy; auto-detected if omitted)")
    parser.add_argument("--check", action="store_true",
                        help="Compare only: exit 1 if the committed artifact "
                             "is stale, write nothing")
    args = parser.parse_args()

    demon = Path(args.demon).expanduser().resolve()
    if not (demon / ".git").exists():
        print(f"error: {demon} is not a git checkout (clone "
              f"daydreamlive/DEMON or pass --demon)", file=sys.stderr)
        return 2

    if not args.no_fetch:
        _git_fetch(demon)

    try:
        sha = _git(demon, "rev-parse", args.ref).strip()
        commit_date = _git(demon, "log", "-1", "--format=%cI", args.ref).strip()
    except subprocess.CalledProcessError:
        print(f"error: ref {args.ref!r} not found in {demon}",
              file=sys.stderr)
        return 2

    python_exe = _pick_dumper_python(args.python)
    contract = _extract_python_contract(demon, args.ref, python_exe)
    ui = _extract_ui(demon, args.ref)

    artifact = {
        "artifact_version": ARTIFACT_VERSION,
        "source": {
            "repo": "daydreamlive/DEMON",
            "ref": args.ref,
            "sha": sha,
            "commit_date": commit_date,
            "synced_at": _dt.datetime.now(_dt.timezone.utc)
                         .isoformat(timespec="seconds"),
        },
        "protocol": contract["protocol"],
        "constants": contract["constants"],
        "knobs": contract["knobs"],
        "ui": ui,
    }

    out_path = Path(args.out)
    old_artifact: dict | None = None
    if out_path.exists():
        try:
            old_artifact = json.loads(out_path.read_text())
        except json.JSONDecodeError:
            print(f"warning: existing {out_path} is not valid JSON; "
                  f"treating as absent.", file=sys.stderr)

    changed = (old_artifact is None
               or payload_of(old_artifact) != payload_of(artifact))

    print(f"DEMON: {sha[:9]} ({args.ref}), dumper: {python_exe}")
    if not changed:
        print(f"contract: no change ({out_path} is current)")
        return 0

    if old_artifact is not None:
        for line in _summarize_diff(payload_of(old_artifact),
                                    payload_of(artifact)):
            print(f"  {line}")

    if args.check:
        print(f"contract: STALE — {out_path} does not match DEMON@{args.ref}."
              f" Run scripts/sync_contract.py, review the diff, fix or "
              f"whitelist, and commit.")
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    print(f"contract: wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

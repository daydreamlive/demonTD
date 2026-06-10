"""Contract tests — demonTD's real surface vs the vendored DEMON contract.

This suite replaces the regex-based scripts/check_protocol_drift.py. The
reference is vendor/demon_contract.json (extracted from DEMON@origin/main
by scripts/sync_contract.py); the subject is the real importable modules
(src/wire.py, src/params.py) plus targeted source-text checks on
src/demon_ext.py (which can't import outside TouchDesigner).

Every intentional gap lives in contracts/parity_whitelist.json with a
mandatory rationale; test_whitelist_hygiene fails entries that go stale
(subject vanished from the contract) or false (demonTD implements it),
so the whitelist cannot quietly hide real drift.
"""
from __future__ import annotations

import fnmatch
import json
import struct
from pathlib import Path

import pytest

import params as P
import wire

REPO_ROOT = Path(__file__).resolve().parent.parent

# Encoders in wire.py that are not wire *commands*: encode_config builds
# the session-init payload (the contract's `config` catalog), and
# encode_audio_frame builds the binary PCM frame that follows source
# uploads (documented per-command as `binary`, not a command itself).
NON_MESSAGE_ENCODERS = {"config", "audio_frame"}

# SessionConfig keys demonTD computes at Connect() time rather than
# reading from an Init par (see _build_session_config in demon_ext.py).
# Phase 2 replaces this list with the pure session-config builder's
# actual emitted keys.
COMPUTED_CONFIG_FIELDS = {
    "enabled_loras", "lora_strengths", "prompt_b", "client_id",
    "use_server_fixture",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _all_knobs(contract: dict) -> dict:
    """ODE ∪ SDE knob manifests (mode-specific knobs like sde_amp appear
    in only one; the union is what 'exists upstream' means for TD)."""
    return {**contract["knobs"]["ode"], **contract["knobs"]["sde"]}


def _encoder_names() -> set[str]:
    return {n[len("encode_"):] for n in dir(wire)
            if n.startswith("encode_") and callable(getattr(wire, n))}


def _td_streamed_params() -> dict[str, P.Param]:
    """Continuous params that actually ride the `params` raw dict —
    wire-named, minus the keys filter_params_for_wire strips (those have
    dedicated command messages or are UI-only)."""
    return {p.wire_name: p for p in P.PARAMS
            if p.category == "continuous" and p.wire_name
            and p.wire_name not in P.PARAMS_NOT_FOR_WIRE}


def _td_init_params() -> dict[str, P.Param]:
    return {p.wire_name: p for p in P.PARAMS
            if p.category == "init" and p.wire_name}


def _matches_any(name: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(name, pat) for pat in patterns)


def _num_eq(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return a == b


# ---------------------------------------------------------------------------
# whitelist hygiene — the whitelist itself is under test
# ---------------------------------------------------------------------------

def test_whitelist_hygiene(contract, whitelist):
    problems: list[str] = []
    cmds = contract["protocol"]["commands"]
    events = contract["protocol"]["events"]
    cfg = contract["protocol"]["config"]
    knobs = _all_knobs(contract)
    encoders = _encoder_names()
    streamed = _td_streamed_params()

    def _rationales(section: str):
        for key, why in whitelist.get(section, {}).items():
            if not isinstance(why, str) or len(why) < 20:
                problems.append(
                    f"{section}.{key}: rationale missing or too thin "
                    f"(need a real 'why', >= 20 chars)")

    for section in ("commands_not_sent", "events_ignored",
                    "handshake_not_implemented", "config_fields_not_sent",
                    "config_fields_extra", "knobs_not_streamed",
                    "td_extra_knobs", "config_default_overrides"):
        _rationales(section)

    for name in whitelist.get("commands_not_sent", {}):
        if name not in cmds:
            problems.append(
                f"commands_not_sent.{name}: not a contract command any more "
                f"— stale entry, delete it")
        if f"encode_{name}" in dir(wire):
            problems.append(
                f"commands_not_sent.{name}: wire.encode_{name} EXISTS — the "
                f"gap closed, delete the entry")

    for name in whitelist.get("events_ignored", {}):
        if name not in events:
            problems.append(
                f"events_ignored.{name}: not a contract event any more — "
                f"stale entry, delete it")

    for name in whitelist.get("handshake_not_implemented", {}):
        if name not in contract["protocol"]["handshake"]["commands"]:
            problems.append(
                f"handshake_not_implemented.{name}: not a handshake command "
                f"any more — stale entry, delete it")

    for name in whitelist.get("config_fields_not_sent", {}):
        if name not in cfg:
            problems.append(
                f"config_fields_not_sent.{name}: not a contract config "
                f"field any more — stale entry, delete it")
        if name in _td_init_params() or name in COMPUTED_CONFIG_FIELDS:
            problems.append(
                f"config_fields_not_sent.{name}: demonTD DOES send this — "
                f"the gap closed, delete the entry")

    for pat in whitelist.get("knobs_not_streamed", {}):
        hits = [k for k in knobs if fnmatch.fnmatchcase(k, pat)]
        if not hits:
            problems.append(
                f"knobs_not_streamed.{pat}: matches no knob in the manifest "
                f"— stale entry, delete it")
        if any(k in streamed for k in hits):
            problems.append(
                f"knobs_not_streamed.{pat}: TD streams a matching knob — "
                f"the gap closed, delete the entry")

    for name in whitelist.get("td_extra_knobs", {}):
        if name not in streamed:
            problems.append(
                f"td_extra_knobs.{name}: TD doesn't stream this — stale "
                f"entry, delete it")
        if name in knobs:
            problems.append(
                f"td_extra_knobs.{name}: now in the upstream manifest — "
                f"delete the entry")

    for name, entry in whitelist.get("knob_overrides", {}).items():
        if name not in streamed:
            problems.append(f"knob_overrides.{name}: not a TD-streamed key")
        if not isinstance(entry, dict) or not entry.get("fields") \
                or len(entry.get("why", "")) < 20:
            problems.append(
                f"knob_overrides.{name}: needs {{'fields': [...], "
                f"'why': '...'}} with a real rationale")

    for name in whitelist.get("config_default_overrides", {}):
        if name not in cfg:
            problems.append(
                f"config_default_overrides.{name}: not a contract config "
                f"field — stale entry, delete it")

    for name in whitelist.get("label_overrides", {}):
        if name not in contract["ui"]["labels"]:
            problems.append(
                f"label_overrides.{name}: not a canonical label any more — "
                f"stale entry, delete it")

    lt = whitelist.get("lora_triggers_reviewed", {})
    if not lt.get("blob_sha") or lt["blob_sha"] == "PLACEHOLDER" \
            or len(lt.get("why", "")) < 20:
        problems.append(
            "lora_triggers_reviewed: needs the reviewed blob_sha and a why")

    assert not problems, "whitelist hygiene:\n  " + "\n  ".join(problems)


# ---------------------------------------------------------------------------
# commands ↔ encoders
# ---------------------------------------------------------------------------

def test_every_contract_command_has_encoder(contract, whitelist):
    missing = (set(contract["protocol"]["commands"])
               - _encoder_names()
               - set(whitelist["commands_not_sent"]))
    assert not missing, (
        f"contract commands with no wire.encode_* and no whitelist entry: "
        f"{sorted(missing)} — add the encoder (and UI) or whitelist with a "
        f"rationale in contracts/parity_whitelist.json")


def test_every_encoder_is_a_contract_command(contract):
    extra = (_encoder_names() - NON_MESSAGE_ENCODERS
             - set(contract["protocol"]["commands"]))
    assert not extra, (
        f"wire.py encodes message types the server no longer accepts: "
        f"{sorted(extra)} — the server dropped them; remove the encoders "
        f"and their call sites")


def test_every_encoder_has_a_call_site(contract, whitelist):
    """Reachability: an encoder nobody calls is the 'set_interp_method
    shipped with no UI' bug. Text-level because demon_ext.py only
    imports inside TouchDesigner."""
    src = (REPO_ROOT / "src" / "demon_ext.py").read_text()
    pacer = (REPO_ROOT / "src" / "params_pacer.py").read_text()
    unreferenced = [n for n in sorted(_encoder_names())
                    if f"encode_{n}(" not in src
                    and f"encode_{n}(" not in pacer]
    assert not unreferenced, (
        f"encoders never called from demon_ext.py/params_pacer.py: "
        f"{unreferenced} — wire them to a par or remove them")


# ---------------------------------------------------------------------------
# command payload schemas — what the encoders EMIT, validated field by
# field against the contract's specs (coverage the old checker never had)
# ---------------------------------------------------------------------------

_PY_TYPE_FOR = {
    "float": (int, float), "int": int, "bool": bool, "str": str,
    "dict": dict, "list": list,
}

# command → list of sample invocations (args, kwargs). Encoders whose
# optional fields are conditionally emitted get one maximal sample so
# every emitted key is exercised.
_ENCODER_SAMPLES: dict[str, list[tuple[tuple, dict]]] = {
    "params": [((
        {"denoise": 0.5, "seed": 42}, 1.25), {})],
    "prompt": [(("clean prompt",),
                {"key": "C major", "time_signature": "3",
                 "tags_b": "prompt b"})],
    "set_prompt_blend": [((0.4,), {})],
    "enable_lora": [(("lora-id",), {"strength": 0.7})],
    "disable_lora": [(("lora-id",), {})],
    "set_timbre_strength": [((0.8,), {})],
    "set_timbre_source": [(("src.wav",), {})],
    "set_timbre_fixture": [(("fixture",), {})],
    "clear_timbre_source": [((), {})],
    "set_structure_source": [(("src.wav",), {})],
    "set_structure_fixture": [(("fixture",), {})],
    "clear_structure_source": [((), {})],
    "swap_source": [((), {"tags": "t", "key": "C major",
                          "time_signature": "3", "fixture_name": "f"})],
    # set_interp_method is exercised exhaustively below, over every
    # (path, method) enum combination from the contract.
}


def test_command_payload_shapes(contract, whitelist):
    cmds = contract["protocol"]["commands"]
    problems: list[str] = []

    def _validate(name: str, payload_json: str):
        msg = json.loads(payload_json)
        spec = cmds[name]["fields"]
        if msg.get("type") != name:
            problems.append(f"{name}: emitted type={msg.get('type')!r}")
            return
        for key, val in msg.items():
            if key == "type":
                continue
            fs = spec.get(key)
            if fs is None:
                problems.append(
                    f"{name}.{key}: emitted but not in the contract — the "
                    f"server will ignore (or reject) it")
                continue
            want = _PY_TYPE_FOR.get(fs["type"])
            if fs["type"] == "enum":
                if fs.get("options") and val not in fs["options"]:
                    problems.append(
                        f"{name}.{key}: emitted {val!r}, contract options "
                        f"are {fs['options']}")
            elif want and not isinstance(val, want):
                problems.append(
                    f"{name}.{key}: emitted {type(val).__name__}, contract "
                    f"says {fs['type']}")
        for key, fs in spec.items():
            if fs.get("required") and key not in msg:
                problems.append(
                    f"{name}.{key}: required by the contract but missing "
                    f"from the encoder output")

    for name, samples in _ENCODER_SAMPLES.items():
        enc = getattr(wire, f"encode_{name}", None)
        if enc is None:
            continue  # absence is test_every_contract_command_has_encoder's job
        for args, kwargs in samples:
            _validate(name, enc(*args, **kwargs))

    # Drive set_interp_method through the full enum grid.
    sim = cmds.get("set_interp_method")
    if sim and hasattr(wire, "encode_set_interp_method"):
        for path in sim["fields"]["path"]["options"]:
            for method in sim["fields"]["method"]["options"]:
                _validate("set_interp_method",
                          wire.encode_set_interp_method(path, method))

    # Every implemented command must have a sample (a new encoder without
    # one would silently skip payload validation).
    sampled = set(_ENCODER_SAMPLES) | {"set_interp_method"}
    implemented = (_encoder_names() & set(cmds))
    unsampled = implemented - sampled
    assert not unsampled, (
        f"encoders with no payload sample in _ENCODER_SAMPLES: "
        f"{sorted(unsampled)}")
    assert not problems, "payload schema:\n  " + "\n  ".join(problems)


# ---------------------------------------------------------------------------
# binary framing constants
# ---------------------------------------------------------------------------

def test_slice_constants(contract):
    c = contract["constants"]
    assert wire.SAMPLE_RATE == c["SAMPLE_RATE"]
    assert wire.T == c["T"]
    assert wire.CROSSFADE_SECONDS == c["CROSSFADE_SECONDS"]
    assert wire.SLICE_HDR_SIZE == c["SLICE_HDR_SIZE"]
    assert struct.calcsize(c["SLICE_HDR_FMT"]) == wire.SLICE_HDR_SIZE, (
        f"contract header fmt {c['SLICE_HDR_FMT']!r} is "
        f"{struct.calcsize(c['SLICE_HDR_FMT'])}B, wire.SLICE_HDR_SIZE is "
        f"{wire.SLICE_HDR_SIZE}")
    assert wire.SLICE_FLAG_RAW == c["SLICE_FLAG_RAW"]
    assert wire.SLICE_FLAG_DELTA == c["SLICE_FLAG_DELTA"]


def test_protocol_versions_pinned(contract):
    """A version bump upstream is a STOP sign, not a knob tweak — look at
    what changed before updating these pins."""
    assert contract["protocol"]["version"] == 1
    assert contract["knobs"]["version"] == 1


# ---------------------------------------------------------------------------
# knobs (the `params` raw dict) — both directions, plus ranges/defaults
# ---------------------------------------------------------------------------

def test_td_streams_only_manifest_knobs(contract, whitelist):
    knobs = _all_knobs(contract)
    extra = (set(_td_streamed_params()) - set(knobs)
             - set(whitelist["td_extra_knobs"]))
    assert not extra, (
        f"TD streams wire keys the knob manifest doesn't declare: "
        f"{sorted(extra)} — the server dropped them (dead knobs); remove "
        f"the Param or whitelist in td_extra_knobs with a rationale")


def test_every_knob_is_reachable_from_td(contract, whitelist):
    knobs = _all_knobs(contract)
    streamed = set(_td_streamed_params())
    missing = [k for k in sorted(knobs)
               if k not in streamed
               and not _matches_any(k, whitelist["knobs_not_streamed"])]
    assert not missing, (
        f"manifest knobs with no TD continuous Param and no whitelist "
        f"entry: {missing} — new upstream knobs; add Params or whitelist "
        f"with a rationale")


_TD_TYPE_OK = {
    "Float": {"float", "int"},
    "Int": {"int", "float"},
    "Toggle": {"bool"},
    "Menu": {"enum", "str"},
    "Str": {"str"},
}


def test_knob_ranges_types_defaults(contract, whitelist):
    """TD range ⊆ knob range (the server CLAMPS out-of-range values, so a
    TD slider stretch past the knob max is dead travel — the
    hint_strength-1.4 class of bug), and TD defaults match the web UI's
    starting values (ui.control_defaults) falling back to the registry
    default — so the op SOUNDS like the webapp out of the box."""
    knobs = _all_knobs(contract)
    web = contract["ui"]["control_defaults"]
    overrides = whitelist["knob_overrides"]
    problems: list[str] = []

    for wire_key, p in sorted(_td_streamed_params().items()):
        k = knobs.get(wire_key)
        if k is None:
            continue  # test_td_streams_only_manifest_knobs covers it
        waived = set(overrides.get(wire_key, {}).get("fields", []))

        ok_types = _TD_TYPE_OK.get(p.type, set())
        if "type" not in waived and k["type"] not in ok_types:
            problems.append(
                f"{wire_key}: TD par type {p.type} vs knob type {k['type']}")

        kmin, kmax = k.get("min"), k.get("max")
        if "min" not in waived and p.min is not None and kmin is not None \
                and p.min < kmin:
            problems.append(
                f"{wire_key}: TD min {p.min} below knob min {kmin} "
                f"(server clamps — dead slider travel)")
        if "max" not in waived and p.max is not None and kmax is not None \
                and p.max > kmax:
            problems.append(
                f"{wire_key}: TD max {p.max} above knob max {kmax} "
                f"(server clamps — dead slider travel)")

        if "default" not in waived and p.type in ("Float", "Int", "Toggle",
                                                  "Menu"):
            expected = web.get(wire_key, k.get("default"))
            src = ("web config.json controls" if wire_key in web
                   else "knob registry")
            if expected is not None and not _num_eq(p.default, expected):
                problems.append(
                    f"{wire_key}: TD default {p.default!r} != {expected!r} "
                    f"({src}; registry default {k.get('default')!r}) — fix "
                    f"or add knob_overrides.{wire_key} fields=['default']")

    assert not problems, "knob parity:\n  " + "\n  ".join(problems)


def test_menu_options_match_enum(contract, whitelist):
    knobs = _all_knobs(contract)
    overrides = whitelist["knob_overrides"]
    problems: list[str] = []
    for wire_key, p in sorted(_td_streamed_params().items()):
        k = knobs.get(wire_key)
        if k is None or p.type != "Menu" or k.get("type") != "enum":
            continue
        if "options" in set(overrides.get(wire_key, {}).get("fields", [])):
            continue
        want = set(k.get("options", ()))
        got = set(p.menu_names)
        if want != got:
            problems.append(
                f"{wire_key}: TD menu {sorted(got)} != contract options "
                f"{sorted(want)}")
    assert not problems, "menu/enum parity:\n  " + "\n  ".join(problems)


# ---------------------------------------------------------------------------
# SessionConfig — fields both directions, defaults vs canonical
# ---------------------------------------------------------------------------

def test_session_config_field_parity(contract, whitelist):
    cfg = set(contract["protocol"]["config"])
    sent = set(_td_init_params()) | COMPUTED_CONFIG_FIELDS

    not_sent = cfg - sent - set(whitelist["config_fields_not_sent"])
    assert not not_sent, (
        f"contract config fields demonTD never sends (and no whitelist "
        f"entry): {sorted(not_sent)} — new SessionConfig fields; wire them "
        f"into _build_session_config or whitelist with a rationale")

    extra = sent - cfg - set(whitelist["config_fields_extra"])
    assert not extra, (
        f"demonTD sends config keys the contract doesn't list: "
        f"{sorted(extra)} — the server dropped them; stop sending or "
        f"whitelist in config_fields_extra")


def test_session_config_defaults(contract, whitelist):
    """TD Init-par defaults vs the web installation's engine defaults
    (ui.engine_defaults), falling back to the SessionConfig dataclass
    default. The vae_window=6.0-for-a-release class of bug."""
    cfg = contract["protocol"]["config"]
    engine = contract["ui"]["engine_defaults"]
    overrides = whitelist["config_default_overrides"]
    problems: list[str] = []

    for wire_key, p in sorted(_td_init_params().items()):
        if wire_key in overrides:
            continue
        spec = cfg.get(wire_key)
        if spec is None:
            continue  # field-parity test covers it
        expected = engine.get(wire_key, spec.get("default"))
        src = ("web config.json engine" if wire_key in engine
               else "SessionConfig default")
        if expected is not None and not _num_eq(p.default, expected):
            problems.append(
                f"{wire_key}: TD default {p.default!r} != {expected!r} "
                f"({src}) — fix params.py or add "
                f"config_default_overrides.{wire_key}")

    assert not problems, "config defaults:\n  " + "\n  ".join(problems)


# ---------------------------------------------------------------------------
# UI-side parity — labels, pulse coverage, the loraTriggers port
# ---------------------------------------------------------------------------

def test_label_parity(contract, whitelist):
    """For wire keys the canonical UI renames (DISPLAY_NAMES), the TD
    label must contain the canonical word — users look for 'structure',
    not 'hint strength'. Lenient contains-match; richer TD labels pass."""
    labels = contract["ui"]["labels"]
    overrides = whitelist["label_overrides"]
    problems: list[str] = []
    for p in P.PARAMS:
        if not p.wire_name or p.wire_name not in labels:
            continue
        if p.wire_name in overrides:
            continue
        want = labels[p.wire_name].strip().lower()
        got = (p.label or p.name).strip().lower()
        if want not in got:
            problems.append(
                f"{p.name} (wire {p.wire_name}): canonical labels this "
                f"{labels[p.wire_name]!r}, TD labels it {p.label!r} — users "
                f"will look for the canonical word and not find it")
    assert not problems, "label parity:\n  " + "\n  ".join(problems)


def test_pulse_ui_coverage(contract):
    """Every discrete pulse routes to a real contract command, has an
    encoder, and is backed by a visible TD par."""
    cmds = set(contract["protocol"]["commands"])
    problems: list[str] = []
    for par_name, kind in P.DISCRETE_PULSE_TO_KIND.items():
        p = P.PARAM_BY_NAME.get(par_name)
        if p is None:
            problems.append(f"{par_name}: pulse not in PARAMS")
        elif p.ui_hidden:
            problems.append(f"{par_name}: pulse is ui_hidden — unreachable")
        if kind not in cmds:
            problems.append(
                f"{par_name}: routes to {kind!r}, not a contract command")
        if not hasattr(wire, f"encode_{kind}"):
            problems.append(f"{par_name}: no wire.encode_{kind}")
    assert not problems, "pulse coverage:\n  " + "\n  ".join(problems)


def test_lora_triggers_port_fresh(contract, whitelist):
    upstream = contract["ui"]["sources"]["lora_triggers"]["blob_sha"]
    reviewed = whitelist["lora_triggers_reviewed"]["blob_sha"]
    assert upstream == reviewed, (
        f"upstream loraTriggers.ts changed ({reviewed[:9]} -> "
        f"{upstream[:9]}): re-review src/lora_triggers.py against "
        f"{contract['ui']['sources']['lora_triggers']['path']} in the DEMON "
        f"repo, then update lora_triggers_reviewed.blob_sha")


def test_handshake_coverage(contract, whitelist):
    missing = (set(contract["protocol"]["handshake"]["commands"])
               - set(whitelist["handshake_not_implemented"]))
    # demonTD implements no handshake commands today; anything not
    # whitelisted is a new upstream handshake verb to look at.
    assert not missing, (
        f"handshake commands with no whitelist entry: {sorted(missing)}")

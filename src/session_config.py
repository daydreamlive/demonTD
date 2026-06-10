"""SessionConfig assembly — pure, no TouchDesigner dependencies.

The single source of truth for WHICH fields demonTD sends in the
session-init config and where each value comes from. `demon_ext.py`'s
`_build_session_config` is a thin wrapper: it gathers the par-backed
values via `_read_par` and calls :func:`build_session_config` here.

Keeping this importable outside TD lets tests/test_contract.py compare
the actual emitted key set against the vendored DEMON contract
(`protocol.config`) instead of regex-scraping demon_ext.py — and lets
field defaults come from `params.py` (one source of truth) instead of
duplicated literals.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

try:
    _mod = mod  # type: ignore[name-defined]  # noqa: F821
    P = _mod('params')
except NameError:
    import params as P  # type: ignore


# Wire key -> cast, for every SessionConfig field backed by an Init par.
# The TD par name and the fallback default both come from params.py
# (PARAM_BY_WIRE), so a default fixed there is fixed everywhere. Order
# mirrors the web client's buildConfig() field order — irrelevant to the
# server but it keeps wire logs diffable against the webapp's.
PAR_BACKED_FIELDS: tuple[tuple[str, Callable[[Any], Any]], ...] = (
    ("sde", bool),
    ("lora", bool),
    ("depth", int),
    ("vae_window", float),
    ("crop", float),
    ("steps", int),
    ("fast_vae", bool),
    ("walk_window", bool),
    ("walk_window_s", float),
    ("prompt", str),
    ("fixture_name", str),
    # Playback-lead tuning (server-side decode buffer). Optional in the
    # protocol ("omit to use server default") but the web client sends
    # them from its config.json defaults, so we do too for parity.
    ("lead_floor_s", float),
    ("lead_ceiling_s", float),
    ("lead_release_tau_s", float),
)


def par_names() -> dict[str, str]:
    """{wire_key: TD par name} for the par-backed fields — what the
    extension needs to gather before calling build_session_config."""
    return {wire_key: P.PARAM_BY_WIRE[wire_key].name
            for wire_key, _ in PAR_BACKED_FIELDS}


def build_session_config(
    par_values: Mapping[str, Any],
    *,
    enabled_loras: Iterable[str],
    lora_strengths: Mapping[str, float],
    prompt_b: str,
    device_id: str | None,
    zstd_available: bool,
) -> dict[str, Any]:
    """Build the SessionConfig dict sent right after WS open.

    `par_values` maps TD par name -> raw par value for the par-backed
    fields (missing/uncastable entries fall back to the params.py
    default). The remaining keyword fields are session state the
    extension computes at Connect() time.
    """
    def val(wire_key: str, cast: Callable[[Any], Any]) -> Any:
        p = P.PARAM_BY_WIRE[wire_key]
        raw = par_values.get(p.name, p.default)
        try:
            return cast(raw)
        except (TypeError, ValueError):
            return cast(p.default)

    cfg: dict[str, Any] = {
        "sde":           val("sde", bool),
        "lora":          val("lora", bool),
        "depth":         val("depth", int),
        "vae_window":    val("vae_window", float),
        "crop":          val("crop", float),
        "steps":         val("steps", int),
        "fast_vae":      val("fast_vae", bool),
        "walk_window":   val("walk_window", bool),
        "walk_window_s": val("walk_window_s", float),
        "enabled_loras": list(enabled_loras),
        "prompt":        val("prompt", str),
        # Secondary prompt for A/B blending. Sourced from the LIVE
        # `Promptb` par so it tracks whatever the user has typed at
        # session start; editable mid-session via SendPrompt (matches
        # the web client's `prompt_b: perf.promptB`). Empty string =
        # no B side.
        "prompt_b":      str(prompt_b or ""),
        "lora_strengths": dict(lora_strengths),
        "fixture_name":  val("fixture_name", str),
        "lead_floor_s":  val("lead_floor_s", float),
        "lead_ceiling_s": val("lead_ceiling_s", float),
        "lead_release_tau_s": val("lead_release_tau_s", float),
        # Capability gate — when True the server loads the fixture from
        # its own /fixtures cache and the client skips the audio frame
        # upload. The JS client capability-probes via /api/server-info
        # before flipping this to True; we send False unconditionally so
        # the unchanged upload path is used.
        "use_server_fixture": False,
        # Per-machine identifier the server stashes into loguru
        # contextvars so pod-side log records on this WS carry it. We
        # reuse the deviceId generated for hosted-mode queue joins.
        # `or None` makes encode_config drop the field on the wire when
        # _load_auth didn't populate it.
        "client_id":     device_id or None,
    }
    # Without a working zstd decompressor (TD's bundled Python can't
    # load the vendored zstandard binary, etc.), ask the server for raw
    # float16 slices — otherwise every slice lands with
    # flags=SLICE_FLAG_DELTA and is rejected by decode_slice. Trade-off
    # is ~1.5x receive bandwidth.
    if not zstd_available:
        cfg["compression"] = "none"
    return cfg

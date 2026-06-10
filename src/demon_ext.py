"""
DemonExt — the TouchDesigner extension class for the DEMON operator.

This module is loaded inside TD as the Extension of a Base COMP. It owns
session state, drives the WebSocket DAT, fans parameter changes out at the
8ms tick, and exposes a clean public API (PascalCase methods) for other TD
networks to call.

Internal operators expected inside the COMP
-------------------------------------------
- ws1            : WebSocket DAT (Receive Binary on)
- http_queue     : Web Client DAT (queue API calls; we mostly use src/queue.py
                   for HTTP, so http_queue is optional/legacy)
- param_exec1    : Parameter Execute DAT pointing at this COMP's custom pages
- tick8ms        : Timer CHOP, segment 0.008s, cycles infinite
- heartbeat      : Timer CHOP, segment 5s, cycles infinite
- audio_in       : In CHOP (the COMP's CHOP input port)
- resample_in    : Resample CHOP, target 48000 Hz
- script_send    : Script CHOP (encodes input audio + sends on WS)
- audio_out      : Script CHOP feeding the COMP's CHOP output port
- resample_out   : Resample CHOP (48k -> project rate)
- lora_catalog   : Table DAT (server-provided LoRA list)
- state          : Table DAT (session state for UI binding)

Threading
---------
TD calls extension methods from the cook thread. WebSocket DAT callbacks fire
on its own thread (we keep that work minimal — parse + write to ring buffer).
Access to mutable state (self._dirty, self._connected, ring buffer) is
protected by locks where needed.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any

# --- vendored dependencies -----------------------------------------------------
# Bundled libs live under <repo>/vendor/.
#   - zstandard: per-platform wheels for compressed audio slice decompression.
#   - websocket-client: pure-Python, replaces TD's broken WebSocket DAT.

# The vendor/ directory _prepend_vendor_paths discovered, or None. Other
# vendor-relative lookups (the PortAudio dylib) MUST resolve from this,
# not from `me.par.file` — the build clears every DAT's file par (so no
# dev-machine path bakes into shipped .tox files), which made any
# par.file-derived path garbage in a freshly built .tox.
_VENDOR_ROOT: str | None = None


def _prepend_vendor_paths() -> None:
    global _VENDOR_ROOT
    try:
        import platform
        sysname = platform.system().lower()
        machine = platform.machine().lower()
        if sysname == "darwin":
            zstd_plat = "darwin-arm64" if "arm" in machine else "darwin-x64"
        elif sysname == "windows":
            zstd_plat = "win-amd64"
        else:
            zstd_plat = None

        # Discover vendor/. Try several candidate base paths in order:
        #   1. Path that THIS DAT is file-synced from (most reliable)
        #   2. The COMP's externaltox load location
        #   3. The TD project folder
        #   4. cwd
        candidates: list[str] = []
        try:
            # `me` is the Text DAT this code is compiled into.
            dat_file = me.par.file.eval()  # type: ignore[name-defined]  # noqa: F821
            if dat_file:
                # demon_ext.py lives at <repo>/src/demon_ext.py — vendor is at <repo>/vendor.
                candidates.append(os.path.abspath(
                    os.path.join(os.path.dirname(dat_file), os.pardir, "vendor")))
        except Exception:
            pass
        try:
            comp = me.owner  # type: ignore[name-defined]  # noqa: F821
            extox = comp.par.externaltox.eval() or ""
            if extox:
                base = os.path.dirname(extox) if extox.endswith(".tox") else extox
                candidates.append(os.path.join(base, "vendor"))
                candidates.append(os.path.join(os.path.dirname(base), "vendor"))
        except Exception:
            pass
        try:
            pf = project.folder  # type: ignore[name-defined]  # noqa: F821
            if pf:
                for n in range(4):
                    p = pf
                    for _ in range(n):
                        p = os.path.dirname(p)
                    candidates.append(os.path.join(p, "vendor"))
        except Exception:
            pass
        candidates.append(os.path.join(os.getcwd(), "vendor"))

        vendor_root = None
        for c in candidates:
            if c and os.path.isdir(c):
                vendor_root = c
                break

        if not vendor_root:
            print(f"[demon_ext] WARNING: vendor/ not found. Tried: {candidates}")
            return

        _VENDOR_ROOT = vendor_root
        print(f"[demon_ext] vendor at {vendor_root}")

        # zstandard (platform-specific)
        if zstd_plat:
            zstd_dir = os.path.join(vendor_root, "zstandard", zstd_plat)
            if os.path.isdir(zstd_dir) and zstd_dir not in sys.path:
                sys.path.insert(0, zstd_dir)
                print(f"[demon_ext]   + {zstd_dir}")
        # websocket-client (pure-Python)
        wsc_dir = os.path.join(vendor_root, "websocket-client")
        if os.path.isdir(wsc_dir) and wsc_dir not in sys.path:
            sys.path.insert(0, wsc_dir)
            print(f"[demon_ext]   + {wsc_dir}")
        # sounddevice (pure Python; bundled portaudio dylib is loaded from
        # vendor/sounddevice/_sounddevice_data/portaudio-binaries/ at
        # sounddevice import time)
        sd_dir = os.path.join(vendor_root, "sounddevice")
        if os.path.isdir(sd_dir) and sd_dir not in sys.path:
            sys.path.insert(0, sd_dir)
            print(f"[demon_ext]   + {sd_dir}")
            # macOS Gatekeeper may quarantine the dylib on first download.
            # Strip the quarantine attribute proactively — silent no-op if
            # already clear or on a non-darwin host.
            try:
                if sysname == "darwin":
                    import subprocess
                    dylib = os.path.join(
                        sd_dir, "_sounddevice_data", "portaudio-binaries",
                        "libportaudio.dylib")
                    if os.path.isfile(dylib):
                        subprocess.run(
                            ["xattr", "-d", "com.apple.quarantine", dylib],
                            capture_output=True, check=False)
            except Exception:
                pass
        # certifi CA bundle: TD's bundled Python has no system trust store,
        # so every urllib HTTPS call raises SSL: CERTIFICATE_VERIFY_FAILED
        # by default. Pointing SSL_CERT_FILE (and SSL_CERT_DIR for OpenSSL
        # compatibility) at our vendored Mozilla bundle gets stdlib urllib
        # — and websocket-client over wss:// — to trust commercial CAs.
        # Read by ssl.create_default_context() at first HTTPS use.
        cacert = os.path.join(vendor_root, "certifi", "cacert.pem")
        if os.path.isfile(cacert):
            os.environ["SSL_CERT_FILE"] = cacert
            os.environ["SSL_CERT_DIR"]  = os.path.dirname(cacert)
            # `requests` honors REQUESTS_CA_BUNDLE if anything in the
            # vendor tree ever switches to it; harmless to set even when
            # only stdlib is in use.
            os.environ["REQUESTS_CA_BUNDLE"] = cacert
            print(f"[demon_ext]   + SSL_CERT_FILE={cacert}")
        else:
            print(f"[demon_ext] WARNING: certifi bundle not at {cacert} "
                  f"-- HTTPS to music.daydream.live will fail with "
                  f"CERTIFICATE_VERIFY_FAILED")
    except Exception as e:
        print(f"[demon_ext] _prepend_vendor_paths failed: {e}")

_prepend_vendor_paths()


def _portaudio_dylib_path(vendor_root: str | None,
                          dat_file: str | None = None,
                          sysname: str | None = None) -> str | None:
    """Locate the vendored PortAudio binary.

    Resolution order:
      1. `vendor_root` — the vendor/ directory _prepend_vendor_paths
         discovered. This is the path that works in a freshly built
         .tox, where every DAT's file par is cleared by the build.
      2. `dat_file` — the demon_ext DAT's file-sync path (the dev
         hot-reload workflow where par.file points at <repo>/src/...);
         vendor/ is its sibling-of-parent.

    Pure function (testable outside TD). Returns None when the binary
    isn't found — SpeakerOut then falls back to system PortAudio paths.
    """
    if sysname is None:
        import platform as _platform
        sysname = _platform.system().lower()
    if sysname == "darwin":
        libname = "libportaudio.dylib"
    elif sysname == "windows":
        libname = "libportaudio64bit.dll"
    else:
        libname = "libportaudio.so"
    roots: list[str] = []
    if vendor_root:
        roots.append(vendor_root)
    if dat_file:
        roots.append(os.path.abspath(os.path.join(
            os.path.dirname(dat_file), os.pardir, "vendor")))
    for root in roots:
        p = os.path.join(root, "sounddevice", "_sounddevice_data",
                         "portaudio-binaries", libname)
        if os.path.isfile(p):
            return p
    return None


try:
    import zstandard as zstd
    _ZSTD_DEC = zstd.ZstdDecompressor()
except Exception as _zstd_err:
    # If we can't load zstd, we'll send compression:"none" in SessionConfig
    # so the server emits raw float16 slices instead of zstd-compressed.
    # ~1.5× more bandwidth but doesn't require a working binary in vendor/.
    print(f"[demon_ext] zstandard load failed: "
          f"{type(_zstd_err).__name__}: {_zstd_err} -- will request "
          f"compression:none from server")
    _ZSTD_DEC = None

import numpy as np

# Sibling-module imports. Two environments:
#
#   1. TD: this file is the text of a Text DAT named `demon_ext` inside a
#      Base COMP. Sibling DATs (params, wire, etc.) are imported via the
#      TD-global `mod()` function — there is no real Python package.
#
#   2. Outside TD (unit tests): everything lives in src/ on sys.path, so
#      regular `import params` works.
#
# We pick the right one by checking whether `mod` is defined as a global.
try:
    _mod = mod  # type: ignore[name-defined]  # noqa: F821
    P = _mod('params')
    wire = _mod('wire')
    queue_mod = _mod('queue_client')
    oauth = _mod('oauth')
    audio_mod = _mod('audio')
    ws_client_mod = _mod('ws_client')
    lora_triggers = _mod('lora_triggers')
    telemetry_mod = _mod('telemetry')
    queue_worker_mod = _mod('queue_worker')
    params_pacer_mod = _mod('params_pacer')
    binary_router_mod = _mod('binary_router')
    param_glide_mod = _mod('param_glide')
except NameError:
    import params as P  # type: ignore
    import wire  # type: ignore
    import queue_client as queue_mod  # type: ignore
    import oauth  # type: ignore
    import audio as audio_mod  # type: ignore
    import lora_triggers  # type: ignore
    import ws_client as ws_client_mod  # type: ignore
    import telemetry as telemetry_mod  # type: ignore
    import queue_worker as queue_worker_mod  # type: ignore
    import params_pacer as params_pacer_mod  # type: ignore
    import binary_router as binary_router_mod  # type: ignore
    import param_glide as param_glide_mod  # type: ignore


# Scheduled-curve helpers. Module-level so they're testable without TD
# globals (tests/test_curves.py imports them). The DemonExt class wraps
# these with the per-curve enable + cache + manual-override logic.

def parse_curve_spec(spec: str) -> list[tuple[float, float]] | None:
    """Parse a curve-spec JSON string into a list of (x, y) control
    points suitable for `eval_curve_linear`.

    Accepts strings of the form `{"points": [[x, y], [x, y], ...]}`
    where x and y are floats. Returns None on any parse / validation
    failure — the sampler then treats this curve as disabled.

    Behavior on parse:
      * x and y are coerced to float; non-numeric entries reject the
        whole curve.
      * Points are sorted by x (ascending).
      * First point's x is clamped to 0.0 and last to 1.0 so the
        [0, 1] sample domain is always covered regardless of what the
        user typed.
      * At least 2 points required (one isn't a curve, it's a
        constant — but we keep `eval_curve_linear`'s behavior simple
        and reject the single-point case explicitly here).
    """
    if not spec:
        return None
    try:
        data = json.loads(spec)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    raw_points = data.get("points")
    if not isinstance(raw_points, list) or len(raw_points) < 2:
        return None
    pts: list[tuple[float, float]] = []
    for p in raw_points:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            return None
        try:
            pts.append((float(p[0]), float(p[1])))
        except (TypeError, ValueError):
            return None
    pts.sort(key=lambda xy: xy[0])
    # Clamp endpoints to cover [0, 1] exactly.
    pts[0] = (0.0, pts[0][1])
    pts[-1] = (1.0, pts[-1][1])
    return pts


def eval_curve_linear(pts: list[tuple[float, float]], t: float) -> float:
    """Sample a piecewise-linear curve `pts` at position `t`.

    `pts` must be a list of >= 2 (x, y) tuples with monotonic non-
    decreasing x, x[0] == 0.0, x[-1] == 1.0 (as produced by
    `parse_curve_spec`). `t` outside [0, 1] is clamped — we don't
    extrapolate.
    """
    if t <= 0.0:
        return pts[0][1]
    if t >= 1.0:
        return pts[-1][1]
    # Bisect (small N — usually 2-10 points — linear scan is fine).
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        if t <= x1:
            if x1 == x0:
                return y0
            u = (t - x0) / (x1 - x0)
            return y0 + (y1 - y0) * u
    # Fall-through (shouldn't reach here given t < 1.0 check above).
    return pts[-1][1]


# Bump this on every meaningful change so the user can confirm at boot
# which build is actually loaded. Visible on the "DemonExt initialized" line.
BUILD_MARKER = "v0.2.16-audio-smoothness+hb-worker+params-pacer+binary-router+cb-hygiene+bug-sweep"

# Hosted-mode pod failover cap. When a hosted WS opens but never reaches
# `ready` (1011 keepalive / overloaded pod / etc.), we leave the dead
# session and re-queue for a DIFFERENT pod. 3 matches the rtmg-vst PR #4
# value. Reset to 0 on successful `ready` or on Connect.
MAX_FAILOVER_ATTEMPTS = 3

# Blend-interpolation menu pars → the `path` field of the
# set_interp_method message. Mirrors demon-public-demo's INTERP_PATHS.
_INTERP_PAR_TO_PATH = {
    "Interpprompt":    "prompt",
    "Interptimbre":    "timbre",
    "Interpstructure": "structure",
    "Interpfeedback":  "feedback",
}

# Manual-override grace window after the user moves a curve-bound param
# by hand (in seconds). The curve yields for this long so the operator's
# adjustment isn't immediately stomped on the next tick. Mirrors
# demon-public-demo's `isManualOverrideActive` 500 ms.
CURVE_OVERRIDE_SECONDS = 0.5

# Hard upper bound on source-audio duration. Matches the web client's
# `engine.max_source_duration_s = 120` cap from
# `demon-public-demo/vendor/demon-ui/lib/config.ts`. Anything longer is
# cropped to the first 120 s on load. The pod's VAE encoder times out
# on longer sources; the WS closes once encode blows its deadline,
# manifesting as a server-side "server sent close" or "Connection to
# remote host was lost" right after `ready`. (Pre-v0.2.5 demonTD
# capped at 240 s — the old, looser server limit.)
MAX_SOURCE_SECONDS = 120

# Server's VAE latent-pool size in frames. Source buffers MUST be a
# multiple of this so the VAE encode pass aligns to its tile boundary.
# Mirrors `SAMPLE_POOL` in
# `demon-public-demo/vendor/demon-ui/lib/audio/trimAudioBuffer.ts`.
# 9600 frames @ 48 kHz = 0.2 s — every cap-aligned source ends on a
# clean multiple.
SAMPLE_POOL_FRAMES = 9600

# Debug-only: where to dump WAV snapshots of decoded audio for offline
# inspection. Used by BUILD=diag-dump-* builds to isolate which side of
# the wire is corrupting bytes when playback comes out as static.
DEBUG_DUMP_DIR = "/tmp/demon-debug"


# -----------------------------------------------------------------------------
# DemonExt
# -----------------------------------------------------------------------------
class DemonExt:
    """The brain of the DEMON operator.

    All public methods are PascalCase (TD convention). Internal state and
    callbacks are snake_case.
    """

    # -------- lifecycle ------------------------------------------------------

    def __init__(self, ownerComp):
        self.ownerComp = ownerComp
        self._lock = threading.RLock()

        # Session state
        self._connected: bool = False
        self._session_id: str | None = None
        self._ws_url: str | None = None
        self._expires_at_ms: int | None = None
        self._extensions_used: int = 0
        self._playback_pos: int = 0  # samples

        # Heartbeat-driver state. v0.2.6 fixed the long-standing bug
        # where the Timer CHOP callback name was wrong and OnHeartbeat
        # NEVER fired. To prevent silent regression: (a) bump a counter
        # on each call so we can log "still alive" periodically, (b)
        # track wall-clock of the last call so onFrameStart's fallback
        # driver can no-op when the Timer CHOP is already feeding.
        self._heartbeat_count: int = 0
        self._last_heartbeat_t: float = 0.0
        # Auto-extend gate. Set to a future time.time() when an extend
        # attempt fails (network blip = 5 s back-off; MAX_EXTENSIONS
        # reached = effectively-forever back-off). Reset on Connect.
        self._auto_extend_backoff_until: float = 0.0

        # Hosted-mode pod failover state. When a WS opens but never
        # reaches `ready` (1011 keepalive, pod overloaded, VAE encode
        # hang), we leave the dead session and re-queue for a different
        # pod, up to MAX_FAILOVER_ATTEMPTS. Reset on Connect() and on
        # the `ready` server message. Matches rtmg-vst PR #4 b2e1953.
        self._saw_ready: bool = False
        self._failover_attempts: int = 0

        # Auth
        self._api_key: str = ""
        self._device_id: str = ""        # populated by _load_auth (UUID4)

        # Param fanout
        self._dirty: dict[str, Any] = {}
        self._last_init_values: dict[str, Any] = {}
        # Running snapshot of all continuous params ever set. The web
        # client sends the FULL current param state every ~8 ms after
        # `ready` (not just deltas) — that continuous traffic is the only
        # thing keeping the pod's WS alive (no separate keepalive). We
        # accumulate dirty changes here and re-send the snapshot every
        # tick so the pod never idle-times-out. See OnTick.
        self._params_snapshot: dict[str, Any] = {}
        # Wall-clock of the last OnTick run. The frame_exec fallback
        # (MaybeTickFromFrame) uses it to avoid double-driving when the
        # Timer CHOP is actually firing.
        self._last_tick_t: float = 0.0
        # Consecutive WS send failures. After a few in a row the
        # connection is dead (e.g. SSL stream corrupted by a timed-out
        # binary write); we tear down ONCE instead of retrying every
        # frame forever (which floods the textport + pegs CPU).
        self._send_fail_streak: int = 0

        # Scheduled-curve state. Each entry tracks the parsed control
        # points for a curve param (so we don't re-parse JSON every
        # tick) keyed by the spec STRING — when the user edits the JSON,
        # the cache key changes and we re-parse on next sample. Set in
        # _sample_curves.
        self._curve_cache: dict[str, list[tuple[float, float]] | None] = {}
        # Per-wire-name "manual override active until" timestamp.
        # When the user moves Denoise (or another schedulable param)
        # directly, the curve yields for 500 ms so the user's
        # adjustment isn't immediately stomped. Mirrors the web client's
        # `isManualOverrideActive` window.
        self._manual_override_until: dict[str, float] = {}
        # Last value the curve sampler wrote into each wire param.
        # OnParChange compares against this to distinguish a user-
        # initiated change (manual override) from a curve-initiated
        # change (no override).
        self._last_curve_write: dict[str, float] = {}

        # LoRA catalog (mirrors the Table DAT)
        self._lora_ids: list[str] = []
        # id -> primary_trigger_word from the server's lora_catalog
        # metadata. Used by SendPrompt to inject the trigger words into
        # `tags` / `tags_b` at send time (see src/lora_triggers.py and
        # demon-public-demo/vendor/demon-ui/lib/loraTriggers.ts).
        self._lora_triggers: dict[str, str] = {}

        # Audio buffer — DEMON's audio model is a LOOP, not a stream.
        # Server sends an initial buffer (typically 24s) that becomes the
        # full loop. Subsequent slices PATCH specific positions in the loop
        # via their `start_sample` field. Playback wraps continuously.
        # See src/audio.py for the LoopBuffer implementation.
        self._ring = audio_mod.LoopBuffer(
            channels=2, sample_rate=wire.SAMPLE_RATE,
        )
        # (No slice-epoch tracking needed: TCP FIFO + recv-thread
        # routing means no old-track slice can arrive after swap_ready;
        # the write-only _epoch counter was deleted.)

        # Python-side audio playback (bypasses TD's CHOP audio chain via
        # ctypes -> bundled PortAudio binary). Lifecycle is start()'d when
        # initial buffer arrives and stop()'d on Disconnect.
        # The vendored binary resolves from the vendor/ root discovered
        # at import time (_VENDOR_ROOT), with the DAT's file-sync path
        # as the dev-workflow fallback. It must NOT rely on me.par.file
        # alone: the build clears every DAT's file par, so in a freshly
        # built .tox that path is garbage — that was the "could not
        # load PortAudio binary: dlopen(libportaudio.dylib...)" no-audio
        # failure after a rebuild.
        dylib_path = None
        try:
            dat_file = None
            try:
                dat_file = me.par.file.eval()  # type: ignore[name-defined]  # noqa: F821
            except Exception:
                pass
            dylib_path = _portaudio_dylib_path(_VENDOR_ROOT,
                                               dat_file or None)
        except Exception:
            pass
        self.log(f"PortAudio dylib: "
                 f"{dylib_path or 'NOT FOUND in vendor/ (will try system paths)'}")
        self._speaker_out = audio_mod.SpeakerOut(
            self._ring,
            sample_rate=wire.SAMPLE_RATE,
            channels=2,
            log=self.log,
            dylib_path=dylib_path,
        )

        # WS client (Python thread; replaces TD's broken WebSocket DAT)
        self._wsc = None  # ws_client_mod.WSClient | None

        # Smoothness telemetry — cross-thread counters (params keepalive
        # cadence, slice patch lead, main-thread hitches, heartbeat HTTP
        # duration). Drained + logged as a [health] line from
        # _drain_inbound. See src/telemetry.py.
        self._stats = telemetry_mod.SmoothnessStats()
        self._last_drain_t: float = 0.0
        self._last_health_log: float = 0.0

        # Queue-heartbeat worker (hosted mode). Does the /api/queue/status
        # + /extend HTTP on a background thread; results come back through
        # _inbound as hb-status / hb-error / hb-extend events. The worker
        # reads ONLY these plain attributes (never TD pars):
        self._queue_base: str = ""          # snapshotted at join time
        self._hb_worker = None              # queue_worker_mod.QueueHeartbeatWorker
        self._hb_extend_requested: bool = False
        # ("auto", pre_extensions_used) | ("user", None) — set when the
        # extend flag is raised, consumed by _apply_extend_result to run
        # the denied-auto-extend backoff only for auto requests.
        self._hb_extend_ctx: tuple = ("user", None)
        self._last_hb_ensure_t: float = 0.0

        # Params pacer — the dedicated thread that sends the continuous
        # params stream (the WS keepalive AND the server's pacing
        # signal). See src/params_pacer.py for why this must not run on
        # the TD frame loop. The pacer's build_message reads the
        # enabled-LoRA CACHE below (a thread can't read TD pars).
        self._pacer = None  # params_pacer_mod.ParamsPacer
        self._enabled_loras_cache: frozenset = frozenset()
        self._last_pacer_warn_t: float = 0.0

        # Send shaping (src/param_glide.py — VST parity, always-on):
        # lora_str_* trailing-edge debounce + blend glide. The post-2026-06
        # backend re-fits LoRA weights per >0.02 strength delta; raw
        # per-UI-event values = refit storm = pipeline stall = SOURCE
        # AUDIO BLEEDS THROUGH. The engine is owned by the pacer thread
        # (only step() mutates it) and recreated per Connect.
        self._glide = param_glide_mod.GlideEngine()
        # prompt_blend / timbre_strength ride dedicated WS messages (the
        # params handler rejects them). Targets written by the main
        # thread under self._lock; the PACER glides + sends them at most
        # every 40 ms (was: immediate send per UI event — a click storm
        # post-deploy).
        self._blend_targets: dict[str, float] = {}
        self._blend_senders = {
            "prompt_blend": param_glide_mod.ThrottledSender(),
            "timbre_strength": param_glide_mod.ThrottledSender(),
        }

        # Inbound message queue — populated by the WS recv thread, drained
        # on the main TD thread (OnTick / 8 ms timer). TD forbids touching
        # any operator from a non-main thread, so we MUST marshal here.
        self._inbound: "queue.Queue[tuple[str, Any]]" = queue.Queue()

        # Binary-frame router — decodes + patches slices ON the WS recv
        # thread (see src/binary_router.py). One per connection, created
        # in _open_ws; binary routing state (expecting-initial, stem
        # skip counts) lives inside it, fed by recv-thread text sniffing.
        self._router = None  # binary_router_mod.BinaryRouter | None

        # Connection generation. Each _open_ws bumps this; the WS
        # callbacks stamp their events with the generation they were
        # created under, and _drain_inbound DROPS events from older
        # generations. Why: WSClient.close() joins its recv thread for
        # only 2 s — a thread stuck in a long send (30 s timeout)
        # lingers past that, and its late ("close", ...) event would
        # otherwise set _connected=False and run close handling against
        # the NEW session (spurious teardown / failover loop).
        self._ws_gen: int = 0

        # Cached state of the `Debug` toggle on the Session page. When True,
        # verbose diagnostics are logged + WAV dumps go to /tmp/demon-debug/.
        # Refreshed via OnParChange("Debug", ...). Cached so we don't read
        # a par on every log call.
        self._debug_enabled: bool = bool(self._read_par("Debug", False))

        # Auth state: load persisted apiKey + deviceId from
        # <prefs>/daydream_auth.json (created lazily on first persist). This
        # populates self._device_id (UUID4) and may pre-fill self._api_key
        # when the user already signed in on a previous TD launch.
        try:
            self._load_auth()
        except Exception as e:
            self.log(f"_load_auth failed (continuing): {type(e).__name__}: {e}")
            if not self._device_id:
                self._device_id = str(uuid.uuid4())

        # Reflect persisted Mode into the visible/greyed-out hosted pars.
        try:
            self._apply_mode_visibility(self._read_par("Mode", "direct"))
        except Exception as e:
            self.log(f"_apply_mode_visibility failed (continuing): {e}")

        self.log(f"DemonExt initialized — BUILD={BUILD_MARKER}")

    # -------- properties (public read-only) ----------------------------------

    @property
    def IsConnected(self) -> bool:
        return self._connected

    @property
    def Status(self) -> dict:
        with self._lock:
            return {
                "connected": self._connected,
                "session_id": self._session_id,
                "queue_position": self._read_par("Queueposition", 0),
                "expires_in": self._read_par("Expiresin", 0.0),
                "extensions_used": self._extensions_used,
                "buffered_samples": self._ring.available,
            }

    # -------- session lifecycle ----------------------------------------------

    def Connect(self, mode: str | None = None) -> bool:
        """Open a session.

        Two modes (selected by the `Mode` par):
          * `direct` — open WS straight at `Server URL` (scheme normalised
            to ws://). Same path demonTD has shipped since v0.1.
          * `hosted` — POST /api/queue/join against the Hosted Base URL
            (default: music.daydream.live), wait for `active`, open the
            returned signed wss:// URL. Calls /api/queue/claim once active
            so the server cancels the reservation-eviction timer (matches
            rtmg-vst's RTMGSession::applyResult).
        """
        with self._lock:
            if self._connected:
                self.log("Connect: already connected")
                return True

            if mode is None:
                mode = self._read_par("Mode", "direct")
            mode = (mode or "direct").lower()

            # Direct-mode endpoint.
            direct_url = self._read_par("Serverurl", "ws://localhost:8765/")
            # Hosted-mode endpoint.
            hosted_base = self._read_par(
                "Baseurl", "https://music.daydream.live")
            api_key = (self._read_par("Apikey", "") or "").strip() or None
            self._api_key = api_key or ""

            # Pre-flight: source audio is required. Bail loudly BEFORE any
            # WS work so the user gets immediate feedback (status text + a
            # popup dialog) instead of a status string buried below a
            # half-completed connect.
            if not self._has_source_audio():
                msg = ("Set Source Audio File on the Session page "
                       "(WAV / MP3 / M4A), then pulse Connect.")
                self._set_status(msg)
                try:
                    ui.messageBox("DEMON: source audio required", msg)  # noqa: F821
                except Exception:
                    pass
                return False

            # Reset failover + ready state so the new attempt starts
            # from a clean slate.
            self._saw_ready = False
            self._failover_attempts = 0
            # Reset auto-extend backoff. A previous session may have hit
            # MAX_EXTENSIONS server-side and parked the backoff a day
            # forward; new session = fresh allowance.
            self._auto_extend_backoff_until = 0.0
            # Reset heartbeat counter so the periodic "still alive" log
            # restarts at #1 each Connect (more useful for diagnosing
            # session-lifetime issues than a global monotonic counter).
            self._heartbeat_count = 0
            self._last_heartbeat_t = 0.0
            # Reset the param-stream keepalive state for the new session.
            self._params_snapshot = {}
            self._last_tick_t = 0.0
            self._send_fail_streak = 0
            # Sync the pacer thread's LoRA-filter cache to the current
            # toggle state before any params flow.
            self._refresh_enabled_loras_cache()
            # Fresh send-shaping state: first-seen keys snap (the
            # post-ready re-assert must go out verbatim, not glide from
            # a previous session's values).
            self._glide = param_glide_mod.GlideEngine()
            self._blend_targets.clear()
            for sender in self._blend_senders.values():
                sender.reset()

            # --- Direct mode --------------------------------------------------
            if mode == "direct":
                ws_url = self._http_to_ws(direct_url)
                self._session_id = None
                self._ws_url = ws_url
                self._expires_at_ms = None
                self._extensions_used = 0
                self._write_par("Queueposition", 0)
                self._write_par("Expiresin", 0.0)
                self._write_par("Denyreason", "")
                self._set_status(f"Connecting to {ws_url}...")
                self._open_ws(ws_url)
                return True

            # --- Hosted mode --------------------------------------------------
            return self._hosted_join_and_open(
                base=hosted_base, api_key=api_key, is_retry=False)

    def _handle_ws_close(self, reason: Any) -> None:
        """Branching on WS close: friendly status + queue/leave for the
        terminal case; failover dispatch for the pre-`ready` hosted
        case.

        Pre-`ready` close in hosted mode is the failover-eligible
        signal — the pod opened a socket but never made it to the
        application handshake, which is exactly the 1011-keepalive /
        VAE-encode-hang / pod-overloaded scenario the VST handles. We
        leave the dead session, increment the failover counter, and
        ask `_drain_inbound` (via a `failover-tick` event marshalled
        from a worker thread) to re-call `_hosted_join_and_open` on
        the main thread.
        """
        mode = (self._read_par("Mode", "direct") or "direct").lower()
        prev_sid = self._session_id

        # Always release the dead queue session in hosted mode. The
        # leave() call is HTTP (fresh TLS, up to 10 s timeout) — run it
        # on a fire-and-forget thread so a slow/unreachable queue API
        # can't stall the main thread while the LoopBuffer is still
        # playing. Same pattern as the queue-claim thread below.
        base = self._read_par("Baseurl", "https://music.daydream.live")
        api_key = self._api_key or None
        if mode == "hosted" and prev_sid:
            def _leave_worker(b=base, k=api_key, sid=prev_sid):
                try:
                    queue_mod.QueueClient(b, api_key=k).leave(sid)
                except Exception:
                    pass
            threading.Thread(
                target=_leave_worker,
                name=f"queue-leave-{prev_sid[:8]}",
                daemon=True,
            ).start()
            self._session_id = None
            self._write_par("Queueposition", 0)
            self._write_par("Expiresin", 0.0)

        # Failover decision: hosted + close-before-ready + room to retry.
        if (mode == "hosted"
                and not self._saw_ready
                and self._failover_attempts < MAX_FAILOVER_ATTEMPTS):
            self._failover_attempts += 1
            self.log(
                f"[failover] pod {self._failover_attempts}/"
                f"{MAX_FAILOVER_ATTEMPTS} closed before ready "
                f"({reason!r}); requeueing"
            )
            self._set_status(
                f"Pod failover {self._failover_attempts}/"
                f"{MAX_FAILOVER_ATTEMPTS} — closed before ready; "
                f"will rejoin queue in 1.5s..."
            )

            # 1.5 s backoff in a worker so we don't hammer the queue.
            # The worker just sleeps and then marshals a
            # `failover-tick` event back to the main thread; the
            # actual queue/join + WS open runs on the main thread
            # in `_drain_inbound`.
            def _failover_worker(b=base, k=api_key):
                try:
                    time.sleep(1.5)
                    self._inbound.put(("failover-tick", (b, k)))
                except Exception as e:
                    self.log(f"[failover] worker raised: {e}")

            threading.Thread(
                target=_failover_worker,
                name=f"failover-{self._failover_attempts}",
                daemon=True,
            ).start()
            return

        # Terminal close: friendly status + (already-done) queue/leave.
        friendly = self._friendly_close_reason(reason)
        if (mode == "hosted"
                and not self._saw_ready
                and self._failover_attempts >= MAX_FAILOVER_ATTEMPTS):
            # Failover budget exhausted — surface that explicitly.
            self._set_status(
                f"Pod failover exhausted ({MAX_FAILOVER_ATTEMPTS} tries). "
                f"Try Connect again later or switch to Direct mode."
            )
        else:
            self._set_status(f"Disconnected ({friendly})")

    def _hosted_join_and_open(self, *, base: str, api_key: str | None,
                              is_retry: bool) -> bool:
        """Run the hosted-mode `/api/queue/join` → poll → open-WS flow.

        Extracted from `Connect()` so the pod-failover path can call it
        again on its own after a close-before-ready (without re-running
        Connect's pre-flight or duplicating the queue/WS plumbing).

        `is_retry=True` means we're being called by the failover path;
        we keep the existing `_pending_audio` (so the second WS sends
        the source on open without re-resolving PCM) and we don't reset
        `_failover_attempts` (the close handler already incremented).

        Returns True on successful WS-open dispatch (caller can rely on
        the WS thread to fire on_open → flush → ready). False on any
        queue-side failure (join refused, queued-timeout, paywall,
        missing wsUrl). The caller updates Status; this method handles
        only the queue plumbing.
        """
        self._write_par("Denyreason", "")
        # Snapshot the queue base for the heartbeat worker (which must
        # never read TD pars). Main-thread write, GIL-atomic reads.
        self._queue_base = base
        if is_retry:
            self._set_status(
                f"Pod failover {self._failover_attempts}/{MAX_FAILOVER_ATTEMPTS} — "
                f"rejoining queue...")
        else:
            self._set_status("Joining queue...")
        client = queue_mod.QueueClient(base, api_key=api_key)
        try:
            resp = client.join(device_id=self._device_id)
        except queue_mod.QueueError as e:
            self._set_status(f"Join failed: {e}")
            self.log(f"_hosted_join_and_open: {e}")
            return False

        self._session_id = resp.session_id

        poll_start = time.time()
        while resp.status == "queued":
            self._set_status(f"Queued (position {resp.position or '?'})")
            self._write_par("Queueposition", resp.position or 0)
            if time.time() - poll_start > 300:
                self._set_status("Queue timeout")
                return False
            time.sleep(1.5)
            try:
                resp = client.status(self._session_id or "")
            except queue_mod.QueueError as e:
                self._set_status(f"Status poll failed: {e}")
                return False

        if resp.status == "over_budget":
            deny = resp.deny_reason or "(no reason)"
            self._set_status(f"Paywall: {deny}")
            self._write_par("Denyreason", deny)
            return False

        if resp.status != "active":
            self._set_status(f"Unexpected status: {resp.status}")
            return False

        self._ws_url = resp.ws_url
        self._expires_at_ms = resp.expires_at
        self._extensions_used = resp.extensions_used or 0
        self._write_par("Queueposition", 0)
        if resp.expires_at:
            now_ms = time.time() * 1000.0
            self._write_par(
                "Expiresin",
                max(0.0, (resp.expires_at - now_ms) / 1000.0),
            )

        if not self._ws_url:
            self._set_status("No wsUrl from server")
            return False

        # Fire-and-forget claim in parallel with WS open. Cancels the
        # server-side reservation eviction so we don't race the WS
        # handshake against it.
        sid = self._session_id or ""
        threading.Thread(
            target=lambda: client.claim(sid),
            name=f"queue-claim-{sid[:8]}",
            daemon=True,
        ).start()

        self._set_status("Connecting to hosted pod...")
        self._open_ws(self._ws_url)
        # Start the heartbeat worker now (it idles until _connected
        # flips True on WS open, then polls every 5 s).
        self._hb_extend_requested = False
        self._ensure_hb_worker()
        return True

    @staticmethod
    def _http_to_ws(url: str) -> str:
        """http://h:p → ws://h:p, https://h:p → wss://h:p, otherwise verbatim."""
        if url.startswith("http://"):
            return "ws://" + url[len("http://"):]
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):]
        return url

    def Disconnect(self) -> None:
        """Close the WS cleanly (status 1000) and free the server-side session.

        Sends a proper WebSocket close frame so DEMON's handle_client returns
        promptly and frees its GPU memory. Without this the server's session
        lingers until the TCP connection times out (~30s+), and rapid
        reconnects pile up sessions and OOM the GPU.
        """
        with self._lock:
            if not self._connected and not self._session_id and self._wsc is None:
                return
            self._set_status("Disconnecting...")
            wsc = self._wsc
            self._wsc = None
            session_id = self._session_id
            self._connected = False
            self._session_id = None
            self._ws_url = None
            self._expires_at_ms = None
            self._dirty.clear()
            self._blend_targets.clear()
            # Detach the router BEFORE clearing the ring — a recv
            # thread still draining frames must not patch (or re-init)
            # the ring after we wipe it.
            r = self._router
            if r is not None:
                r.detach()
            self._ring.clear()

        # Stop Python-side audio playback. Idempotent.
        try:
            self._speaker_out.stop()
        except Exception as e:
            self.log(f"speaker_out stop raised: {e}")

        # Stop the heartbeat worker — the session it polls is gone. It
        # would idle harmlessly anyway (get_state returns None), but a
        # clean stop keeps the thread count honest. _ensure_hb_worker
        # recreates it on the next hosted Connect.
        w = self._hb_worker
        if w is not None:
            try:
                w.stop()
            except Exception:
                pass

        # Stop the params pacer — same deal; recreated by _ensure_pacer
        # on the next _open_ws.
        p = self._pacer
        if p is not None:
            try:
                p.stop()
            except Exception:
                pass

        # Outside the lock: blocking I/O.
        if wsc is not None:
            try:
                # status=1000 is "normal closure" — tells DEMON's websockets
                # lib we're done and to clean up the session.
                wsc.close(code=1000, reason="client disconnect")
                self.log("Disconnect: WS closed cleanly")
            except Exception as e:
                self.log(f"Disconnect: close failed: {e}")

        # In hosted mode we have a server-side session row that needs to
        # be released so the pod is returned to the pool. (Direct mode has
        # no session_id so this is skipped.) Use Baseurl, not Serverurl —
        # Serverurl is the local-pod ws:// in direct mode and has no
        # /api/queue/* surface. Fire-and-forget thread: leave() is HTTP
        # with a 10 s timeout and must not freeze the TD UI.
        if session_id:
            base = self._read_par("Baseurl", "https://music.daydream.live")

            def _leave_worker(b=base, k=(self._api_key or None),
                              sid=session_id):
                try:
                    queue_mod.QueueClient(b, api_key=k).leave(sid)
                except Exception:
                    pass
            threading.Thread(
                target=_leave_worker,
                name=f"queue-leave-{session_id[:8]}",
                daemon=True,
            ).start()

        self._write_par("Queueposition", 0)
        self._write_par("Expiresin", 0.0)
        self._set_status("Idle")

    # --- TD lifecycle hooks ---------------------------------------------------

    def Cleanup(self) -> None:
        """Called when the COMP is deleted or the project is closing.

        Forces a Disconnect so the GPU session on DEMON is freed. Safe to
        call from multiple paths (TD project exit, COMP delete, __del__).
        """
        try:
            self.log("Cleanup: tearing down session")
        except Exception:
            pass
        try:
            self.Disconnect()
        except Exception:
            pass

    def __del__(self):
        # Best-effort: when the extension instance is garbage-collected, send
        # a close frame. Python doesn't guarantee __del__ runs in interpreter
        # shutdown but TD's typical COMP-delete path should hit it.
        try:
            wsc = getattr(self, "_wsc", None)
            if wsc is not None:
                wsc.close(code=1000, reason="extension teardown")
        except Exception:
            pass

    # -------- auth -----------------------------------------------------------

    def _auth_file(self) -> Path:
        """Per-user persistence path for the Daydream apiKey + deviceId.

        macOS:    ~/Library/Application Support/derivative/daydream_auth.json
        Windows:  %APPDATA%/Derivative/daydream_auth.json
        Linux:    ~/.local/share/derivative/daydream_auth.json

        Storing OUTSIDE the .toe project matches the rtmg-vst's PropertiesFile
        approach and avoids leaking the apiKey when a user shares the .toe.
        """
        if sys.platform == "win32":
            base = Path(os.environ.get("APPDATA", str(Path.home()))) / "Derivative"
        elif sys.platform.startswith("linux"):
            base = Path.home() / ".local" / "share" / "derivative"
        else:  # darwin or unknown — fall through to macOS path
            base = Path.home() / "Library" / "Application Support" / "derivative"
        base.mkdir(parents=True, exist_ok=True)
        return base / "daydream_auth.json"

    def _load_auth(self) -> None:
        """Populate self._device_id and (optionally) self._api_key from disk.

        Always sets self._device_id — minting a fresh UUID4 if the file is
        absent or corrupt. The apiKey + display name only land if a prior
        sign-in persisted them.
        """
        try:
            data = json.loads(self._auth_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}

        self._device_id = data.get("deviceId") or str(uuid.uuid4())

        api_key = data.get("apiKey") or ""
        if api_key:
            self._api_key = api_key
            self._write_par("Apikey", api_key)

        # First-boot: persist the freshly-minted deviceId so the next run
        # sees it. Wrapping in try keeps boot resilient if the fs is
        # read-only (rare TD installs from a network drive).
        if "deviceId" not in data:
            try:
                self._persist_auth(api_key, data if isinstance(data, dict) else {})
            except Exception as e:
                self.log(f"_load_auth: persist new deviceId failed: {e}")

    def _persist_auth(self, api_key: str, profile: dict) -> None:
        """Write apiKey + profile + deviceId to <prefs>/daydream_auth.json.

        Profile dict shape matches what /users/profile returns:
            { id|userId, email, name|username, isAdmin, cohortParticipant, ... }

        We keep the same key names that rtmg-vst uses so manual inspection of
        the file is consistent across plugins.
        """
        if not self._device_id:
            self._device_id = str(uuid.uuid4())
        blob = {
            "apiKey": api_key or "",
            "deviceId": self._device_id,
            "userId": (profile.get("id") or profile.get("userId")
                       or profile.get("user_id") or ""),
            "email": profile.get("email") or "",
            "displayName": (profile.get("email") or profile.get("name")
                            or profile.get("username") or ""),
            "isAdmin": bool(profile.get("isAdmin")),
            "cohortParticipant": bool(profile.get("cohortParticipant")),
        }
        self._auth_file().write_text(
            json.dumps(blob, indent=2), encoding="utf-8")

    def SetApiKey(self, key: str) -> None:
        """Store a Daydream API key without validation. Used for the
        callback-from-OAuth path and the legacy Pasteapikey caller.

        For the user-facing paste flow, prefer PromptForApiKey which
        validates against /users/profile before persisting.
        """
        with self._lock:
            self._api_key = key or ""
        self._write_par("Apikey", self._api_key)
        # Keep the on-disk blob in sync (preserves deviceId etc).
        try:
            self._persist_auth(self._api_key, {})
        except Exception as e:
            self.log(f"SetApiKey: persist failed: {e}")

    def PromptForApiKey(self) -> None:
        """Open a modal asking the user to paste an API key, validate it
        against Daydream, then persist on success.

        Validation: GET https://api.daydream.live/users/profile with the
        pasted key as Bearer. 401/403 -> reject with a clear message.
        Anything else with a userId in the response body -> accept.
        Mirrors rtmg-vst's RTMGAuth::signInWithApiKey.
        """
        try:
            import ui  # type: ignore[name-defined]  # noqa: F401
        except Exception:
            self.log("PromptForApiKey: 'ui' unavailable; paste into the API Key par directly")
            return

        # Nudge the user to the API-keys page so they can create / copy a
        # key. This deep-links past the dashboard so it's one click less.
        try:
            webbrowser.open("https://app.daydream.live/dashboard/api-keys")
        except Exception:
            pass

        try:
            value = ui.messageBox(  # type: ignore[name-defined]
                "Paste Daydream API key",
                "Copy a key from app.daydream.live/dashboard/api-keys, "
                "then paste it below:",
                buttons=["OK", "Cancel"],
            )
        except Exception as e:
            self.log(f"PromptForApiKey: dialog failed: {e}")
            return
        if not value:
            return
        key = (value or "").strip()
        if not key:
            return

        self._set_status("Validating API key...")
        try:
            profile = oauth.fetch_profile(key)
        except oauth.OAuthError as e:
            msg = str(e)
            self._set_status(f"Key rejected: {msg}")
            try:
                ui.messageBox("Sign-in failed", msg)  # type: ignore[name-defined]
            except Exception:
                pass
            return

        if not profile or not (profile.get("id") or profile.get("userId")
                               or profile.get("user_id")):
            self._set_status("Key rejected by Daydream")
            try:
                ui.messageBox(  # type: ignore[name-defined]
                    "Sign-in failed",
                    "That API key was rejected. Check it and try again.",
                )
            except Exception:
                pass
            return

        # Accepted — persist + reflect into the UI. The Status par echoes
        # whose key we just accepted; the API Key par itself stays the
        # source of truth (and is secret=True in params.py so it's not
        # printed in plaintext on the Session page).
        with self._lock:
            self._api_key = key
        self._persist_auth(key, profile)
        self._write_par("Apikey", key)
        display = (profile.get("email") or profile.get("name")
                   or profile.get("username") or "")
        self._set_status(f"Signed in as {display or '(unknown)'}")

    # -------- continuous param push ------------------------------------------

    def SetParam(self, name: str, value: Any) -> None:
        """One-shot: send a single param update immediately, bypassing the
        8ms batch. Use for events you want immediate response on.

        name : either a TD par name (e.g. 'Denoise') or a wire name (e.g. 'denoise')

        Note: callers should NOT use this for `prompt_blend` / `timbre_strength`
        / `lora_blend` (the server's params handler rejects them). Use
        SetPromptBlend / SetTimbreStrength / etc. for those.
        """
        wire_name = self._resolve_wire_name(name)
        if not wire_name:
            self.log(f"SetParam: unknown param {name}")
            return
        raw = self._filter_params_for_wire({wire_name: value})
        if not raw:
            self.log(
                f"SetParam: {wire_name!r} is server-rejected on params; "
                f"use the dedicated message instead.")
            return
        playback_sec = self._playback_pos / wire.SAMPLE_RATE
        self._send_text(wire.encode_params(raw, playback_sec))

    def SetParams(self, d: dict[str, Any]) -> None:
        """Batch send a dict of param values (mixed TD-names and wire-names)."""
        raw: dict[str, Any] = {}
        for k, v in d.items():
            wn = self._resolve_wire_name(k)
            if wn:
                raw[wn] = v
        raw = self._filter_params_for_wire(raw)
        if raw:
            playback_sec = self._playback_pos / wire.SAMPLE_RATE
            self._send_text(wire.encode_params(raw, playback_sec))

    # -------- discrete messages ---------------------------------------------

    def SendPrompt(self, tags: str | None = None, key: str | None = None,
                   time_signature: str | None = None,
                   tags_b: str | None = None) -> None:
        tags = tags if tags is not None else (self._read_par("Prompt", "") or "")
        key = key if key is not None else (self._read_par("Key", "auto") or "auto")
        time_signature = (time_signature if time_signature is not None
                          else (self._read_par("Timesignature", "auto") or "auto"))
        # Tags B for prompt blending. `Promptblend` slider lerps between
        # tags (A, value=0) and tags_b (B, value=1) at the server. We
        # only include `tags_b` on the wire when non-empty — matches
        # demon-public-demo's protocol.ts sendPrompt.
        tags_b = tags_b if tags_b is not None else (self._read_par("Promptb", "") or "")

        # Inject LoRA trigger words. Each enabled LoRA's primary trigger
        # is prepended to both tags and tags_b so the model's text
        # encoder actually fires the LoRA style. Gated by the
        # Autoprependloratriggers toggle (default On). See
        # src/lora_triggers.py + demon-public-demo's loraTriggers.ts.
        catalog_rows = self._lora_catalog_rows_for_triggers()
        enabled_ids = self._enabled_loras()
        auto_prepend = bool(self._read_par("Autoprependloratriggers", True))
        tags = lora_triggers.inject(tags, catalog_rows, enabled_ids,
                                    auto_prepend=auto_prepend)
        tags_b_out: str | None
        if tags_b:
            tags_b_out = lora_triggers.inject(tags_b, catalog_rows, enabled_ids,
                                              auto_prepend=auto_prepend)
        else:
            tags_b_out = None

        self._send_text(wire.encode_prompt(tags, key=key,
                                           time_signature=time_signature,
                                           tags_b=tags_b_out))
        if tags_b_out is not None:
            self.log(f"prompt: tags={tags!r} tags_b={tags_b_out!r} "
                     f"key={key} ts={time_signature}")
        else:
            self.log(f"prompt: {tags!r} key={key} ts={time_signature}")

    def _lora_catalog_rows_for_triggers(self) -> list[dict]:
        """Build the catalog-rows list expected by lora_triggers helpers.

        Pulls from the in-memory `_lora_triggers` dict (kept in lockstep
        with the server's lora_catalog by `_apply_lora_catalog`). The
        result is fresh on every call — toggling a LoRA enable
        immediately changes what the next SendPrompt sees.
        """
        with self._lock:
            return [{"id": lid, "trigger_word": self._lora_triggers.get(lid, "")}
                    for lid in self._lora_ids]

    def _resend_prompt_for_lora_change(self) -> None:
        """Re-push the current prompt after a LoRA enable/disable.

        ``enable_lora`` loads the LoRA on the server, but the text
        encoder keeps running the previous prompt — so the new LoRA's
        trigger word isn't on the wire and its style doesn't fire
        until the next prompt send. We trigger that send here so a
        single click of the LoRA toggle has the audible effect the
        user expects, instead of requiring them to also touch a
        strength slider or pulse Sendprompt.

        Logged at a lower level than the user-driven Sendprompt
        pulse so the textport doesn't double-log on every toggle —
        the ``enable_lora`` / ``disable_lora`` line already names the
        triggering event.
        """
        if not self._connected:
            return
        try:
            tags = (self._read_par("Prompt", "") or "")
            key = (self._read_par("Key", "auto") or "auto")
            time_signature = (self._read_par("Timesignature", "auto") or "auto")
            tags_b = (self._read_par("Promptb", "") or "")

            catalog_rows = self._lora_catalog_rows_for_triggers()
            enabled_ids = self._enabled_loras()
            auto_prepend = bool(self._read_par("Autoprependloratriggers", True))
            tags = lora_triggers.inject(tags, catalog_rows, enabled_ids,
                                        auto_prepend=auto_prepend)
            tags_b_out: str | None
            if tags_b:
                tags_b_out = lora_triggers.inject(
                    tags_b, catalog_rows, enabled_ids,
                    auto_prepend=auto_prepend)
            else:
                tags_b_out = None
            self._send_text(wire.encode_prompt(
                tags, key=key, time_signature=time_signature,
                tags_b=tags_b_out))
            if self._debug_enabled:
                self.log(f"  (re-sent prompt for LoRA change)")
        except Exception as e:
            self.log(f"_resend_prompt_for_lora_change failed: {e}")

    def SetPromptBlend(self, value: float | None = None) -> None:
        """Set the prompt A/B blend target. Lands on the wire within
        ~one pacer tick + glide (the pacer thread owns the dedicated
        set_prompt_blend message; see src/param_glide.py) — no longer an
        immediate synchronous send."""
        v = value if value is not None else float(self._read_par("Promptblend", 0.4))
        with self._lock:
            self._blend_targets["prompt_blend"] = float(v)

    def EnableLora(self, id: str, strength: float = 1.0) -> None:
        self._send_text(wire.encode_enable_lora(id, strength=strength))

    def DisableLora(self, id: str) -> None:
        self._send_text(wire.encode_disable_lora(id))

    def SetTimbreStrength(self, value: float) -> None:
        """Set the timbre-strength target (paced like SetPromptBlend)."""
        with self._lock:
            self._blend_targets["timbre_strength"] = float(value)

    def SetTimbreSource(self, chop: Any = None, name: str = "td_timbre",
                        file_path: str | None = None) -> None:
        """Upload audio as a timbre reference.

        Resolution order (matching the main Connect source):
          1. `file_path` arg if provided
          2. Timbre Source File par (if set)
          3. Wired CHOP input's .par.file (if upstream is an Audio File In)
          4. Snapshot of audio_in samples (last resort)
        """
        pcm = self._resolve_source_pcm(
            file_par_name="Timbresourcefile",
            file_path=file_path,
            chop_arg=chop,
        )
        if pcm is None:
            self.log("SetTimbreSource: no audio available")
            return
        self._send_text(wire.encode_set_timbre_source(name))
        self._send_bytes(wire.encode_audio_frame(pcm, channels=2))
        self.log(f"timbre source sent: {pcm.shape[1]} samples "
                 f"({pcm.shape[1] / wire.SAMPLE_RATE:.2f}s)")

    def SetTimbreFixture(self, name: str | None = None) -> None:
        n = name if name is not None else (self._read_par("Timbrefixture", "") or "")
        if not n:
            return
        self._send_text(wire.encode_set_timbre_fixture(n))

    def ClearTimbreSource(self) -> None:
        self._send_text(wire.encode_clear_timbre_source())

    def SetStructureSource(self, chop: Any = None, fixture: str | None = None,
                           name: str = "td_structure",
                           file_path: str | None = None) -> None:
        """Upload audio (or a fixture name) as a structure reference.

        Resolution: explicit fixture → file_path arg → Structure Source File
        par → wired CHOP file → CHOP snapshot.
        """
        if fixture:
            self._send_text(wire.encode_set_structure_fixture(fixture))
            return
        pcm = self._resolve_source_pcm(
            file_par_name="Structuresourcefile",
            file_path=file_path,
            chop_arg=chop,
        )
        if pcm is None:
            self.log("SetStructureSource: no audio available")
            return
        self._send_text(wire.encode_set_structure_source(name))
        self._send_bytes(wire.encode_audio_frame(pcm, channels=2))
        self.log(f"structure source sent: {pcm.shape[1]} samples "
                 f"({pcm.shape[1] / wire.SAMPLE_RATE:.2f}s)")

    def SetStructureFixture(self, name: str | None = None) -> None:
        n = name if name is not None else (self._read_par("Structurefixture", "") or "")
        if not n:
            return
        self._send_text(wire.encode_set_structure_fixture(n))

    def ClearStructureSource(self) -> None:
        self._send_text(wire.encode_clear_structure_source())

    def SwapSource(self, chop: Any = None, tags: str | None = None,
                   key: str | None = None,
                   time_signature: str | None = None,
                   fixture: str | None = None,
                   file_path: str | None = None) -> None:
        """Replace the current source track. Resolution: fixture → file_path
        arg → Swap Source File par → wired CHOP file → CHOP snapshot."""
        tags = tags if tags is not None else (self._read_par("Swaptags", "") or None)
        key = key if key is not None else (self._read_par("Key", "auto") or "auto")
        time_signature = (time_signature if time_signature is not None
                          else (self._read_par("Timesignature", "auto") or "auto"))

        header = wire.encode_swap_source(
            tags=tags, key=key, time_signature=time_signature,
            fixture_name=fixture,
        )
        self._send_text(header)
        if fixture:
            return
        pcm = self._resolve_source_pcm(
            file_par_name="Swapsourcefile",
            file_path=file_path,
            chop_arg=chop,
        )
        if pcm is not None:
            self._send_bytes(wire.encode_audio_frame(pcm, channels=2))
            self.log(f"swap source sent: {pcm.shape[1]} samples "
                     f"({pcm.shape[1] / wire.SAMPLE_RATE:.2f}s)")

    # -------- TD callbacks ---------------------------------------------------

    def OnParChange(self, par) -> None:
        """Called by param_exec1 when any custom par changes.

        Routes:
          - pulse with discrete kind -> dispatch handler
          - init par while connected -> revert + warn
          - continuous par -> drop into _dirty
          - session/local par -> ignored
        """
        name = par.name

        # Dynamic LoRA pars (created in _apply_lora_catalog, not in
        # PARAMS) — handle them BEFORE the schema lookup since they'd
        # otherwise fall into the `if not schema: return` early-out and
        # the user's toggle would never reach the wire. Pattern-match
        # by name prefix and look up the original LoRA id in the
        # _lora_par_to_id reverse map that _apply_lora_catalog
        # maintains.
        lora_par_map = getattr(self, "_lora_par_to_id", None) or {}
        if name in lora_par_map:
            lora_id = lora_par_map[name]
            try:
                if name.startswith("Loraenable"):
                    on = bool(par.eval())
                    # Keep the pacer thread's filter view current — it
                    # can't read TD pars, only this cache.
                    self._refresh_enabled_loras_cache()
                    if on:
                        # Read the matching strength so the server
                        # loads the LoRA at the user's chosen weight,
                        # not the default 1.0.
                        safe = self._lora_par_safe(lora_id)
                        sp = self._par_by_name(f"Lorastr{safe}")
                        strength = float(sp.eval()) if sp is not None else 1.0
                        if self._connected:
                            self._send_text(wire.encode_enable_lora(
                                lora_id, strength=strength))
                            self.log(
                                f"enable_lora({lora_id!r}, strength={strength})")
                            # Re-push the prompt so the now-enabled
                            # LoRA's trigger word reaches the text
                            # encoder on the next generation. Without
                            # this, enable_lora loads the LoRA
                            # server-side but the encoder keeps running
                            # the stale (pre-toggle) prompt and the
                            # LoRA's style doesn't fire until the next
                            # manual SendPrompt or strength touch.
                            # Mirrors how demon-public-demo re-runs
                            # sendPrompt whenever enabled LoRAs change.
                            self._resend_prompt_for_lora_change()
                    else:
                        if self._connected:
                            self._send_text(wire.encode_disable_lora(lora_id))
                            self.log(f"disable_lora({lora_id!r})")
                            # Same logic on disable: refresh the
                            # encoder's trigger prefix so the disabled
                            # LoRA's trigger is no longer in the prompt.
                            self._resend_prompt_for_lora_change()
                elif name.startswith("Lorastr"):
                    # Strength change. Only forward if the LoRA is
                    # currently enabled; otherwise the filter would
                    # strip it from the params message anyway and the
                    # server would have no LoRA to apply it to.
                    enabled_set = set(self._enabled_loras())
                    if lora_id in enabled_set:
                        value = float(par.eval())
                        with self._lock:
                            self._dirty[f"lora_str_{lora_id}"] = value
            except Exception as e:
                self.log(f"OnParChange({name}) lora-route raised: {e}")
            return

        schema = P.PARAM_BY_NAME.get(name)
        if not schema:
            return

        # 0. Debug toggle — cache the new value so log call sites can
        # check a fast bool instead of evaluating a par every time.
        if name == "Debug":
            try:
                self._debug_enabled = bool(par.eval())
            except Exception:
                self._debug_enabled = False
            self.log(f"Debug logging {'enabled' if self._debug_enabled else 'disabled'}")
            return

        # 0a. Mode toggle — grey out the irrelevant set of hosted/direct pars
        # so the Session page makes visual sense. We can't conditionally
        # hide pars in TD; greying them is the closest equivalent.
        if name == "Mode":
            try:
                self._apply_mode_visibility(par.eval())
            except Exception as e:
                self.log(f"OnParChange(Mode) visibility update failed: {e}")
            return

        # Audio output device picker. Apply live: if we're connected and
        # playing through Python Audio Out, restart the speaker stream on
        # the newly-selected device so the switch is immediate; otherwise it
        # just takes effect on the next Connect.
        if name == "Audiodevice":
            self._apply_audio_device_selection(restart_if_live=True)
            return

        # Per-path blend interpolation method — discrete set_interp_method
        # (slerp/linear). Applied immediately; also re-pushed on `ready`.
        path = _INTERP_PAR_TO_PATH.get(name)
        if path is not None:
            if self._connected:
                try:
                    method = str(par.eval())
                except Exception:
                    method = "slerp"
                try:
                    self._send_text(
                        wire.encode_set_interp_method(path, method))
                    self.log(f"set_interp_method({path!r}, {method!r})")
                except Exception as e:
                    self.log(f"set_interp_method send failed: {e}")
            return

        # 1. Pulse actions
        if schema.type == "Pulse":
            self._handle_pulse(name)
            return

        # 2. Init param edited mid-session -> revert + warn
        if name in P.INIT_PARAM_NAMES and self._connected:
            prior = self._last_init_values.get(name, schema.default)
            try:
                par.val = prior
            except Exception:
                pass
            # Setting par.val above fires another OnParChange, which fires
            # this status set again, which logs `status:` to textport.
            # And the user may touch several Init pars in rapid succession.
            # Dedupe by current Status value so we don't spam the same line.
            msg = "Reconnect to apply Init changes"
            try:
                if (self._read_par("Status", "") or "") != msg:
                    self._set_status(msg)
            except Exception:
                self._set_status(msg)
            return

        # 3. Continuous param -> batch
        if name in P.CONTINUOUS_PARAM_NAMES and schema.wire_name:
            value = self._coerce_par_value(par, schema)
            wire_name = schema.wire_name

            # Special-case the three params whose engine handler isn't
            # the generic `params` route — each has a dedicated WS
            # message. Sending them inside a `params` raw dict gets the
            # WS closed (the empirical "disconnects when messing with
            # prompts and LoRAs" failure mode). Source: web client's
            # useParamSync.ts deletes the same three from `raw` before
            # sending.
            # prompt_blend / timbre_strength: write the TARGET; the
            # pacer thread glides toward it (~250 ms) and sends the
            # dedicated message at most every 40 ms. The old immediate
            # per-UI-event send was a conditioning-mutation storm during
            # drags on the post-2026-06 backend (audible clicks + source
            # bleed — same failure the VST fixed the day of the deploy).
            if wire_name in ("prompt_blend", "timbre_strength"):
                with self._lock:
                    self._blend_targets[wire_name] = float(value)
                return
            if wire_name == "lora_blend":
                # UI-only knob in the web client too — it fans out into
                # per-LoRA strengths via useEdgeLoraBinding. We haven't
                # implemented that fan-out yet; the slider exists but
                # does nothing on the wire until we do. Log once so
                # users aren't confused, then suppress.
                if not getattr(self, "_lora_blend_warned", False):
                    self._lora_blend_warned = True
                    self.log(
                        "Lorablend slider is UI-only — engine doesn't "
                        "accept `lora_blend` as a params key. Move the "
                        "per-LoRA strength sliders directly instead.")
                return

            # If this param is bound to a scheduled curve AND the new
            # value differs from what the curve sampler just wrote, the
            # user touched the slider manually. Trip the manual-override
            # window so the curve yields for CURVE_OVERRIDE_SECONDS
            # before stomping the user's adjustment.
            curve_value = self._last_curve_write.get(wire_name)
            if curve_value is not None:
                # If the values match (within float epsilon) it's a
                # curve-initiated write echoing back through OnParChange
                # — leave the override window alone.
                if abs(value - curve_value) > 1e-6:
                    self._manual_override_until[wire_name] = (
                        time.monotonic() + CURVE_OVERRIDE_SECONDS)
            with self._lock:
                self._dirty[wire_name] = value

    def OnTick(self) -> None:
        """Called by tick8ms Timer CHOP every ~50ms (MAIN THREAD).

        Jobs (params sending is NOT one of them anymore — the pacer
        thread owns that; see src/params_pacer.py):
          1. Drain the WS recv thread's inbound message queue (so server
             messages can safely touch TD operators).
          2. Sample scheduled curves into _dirty (TD par reads — main
             thread only); the pacer picks them up within ~16 ms.
          3. Debug telemetry.
        """
        # First-tick beacon so we can confirm the timer is firing. Gated:
        # only printed when Debug is on.
        if self._debug_enabled and not getattr(self, "_ticked_once", False):
            self._ticked_once = True
            self.log("OnTick: timer is running (first tick)")
        # Mark that OnTick ran THIS instant so MaybeTickFromFrame (the
        # frame_exec fallback) can tell whether the Timer CHOP is alive
        # and avoid double-driving.
        self._last_tick_t = time.time()
        # 1. Drain inbound from WS thread FIRST so connect/open/text events
        #    process before any param sends try to use the connection.
        self._drain_inbound()

        # 1a. Sample scheduled curves into _dirty so the params flush
        #     below picks up the new values alongside any user-edited
        #     params. Cheap (returns immediately if no curves enabled).
        if self._connected:
            try:
                self._sample_curves()
            except Exception as e:
                # Curve sampling must never break the cook tick. Log
                # once, then suppress.
                if not getattr(self, "_curve_err_logged", False):
                    self._curve_err_logged = True
                    self.log(
                        f"_sample_curves raised (suppressing further "
                        f"logs): {type(e).__name__}: {e}")

        # Periodic ring-buffer telemetry (~2 s cadence). Gated behind Debug;
        # operationally not useful once we know the chain works.
        if self._debug_enabled and self._connected:
            now = time.time()
            last = getattr(self, "_last_buf_log", 0.0)
            if now - last > 2.0:
                self._last_buf_log = now
                buffered = self._ring.available
                buf_s = buffered / wire.SAMPLE_RATE
                self.log(f"buffered={buffered} samples ({buf_s:.2f}s)")

        # Audio-thread latency telemetry (~1 s cadence). The drain itself
        # is ALWAYS on (cheap: a few int reads + resets, tolerates a tiny
        # race with the audio thread) because the audio callback no
        # longer logs underruns itself — this is the only place they
        # surface. Underruns get an always-on throttled warn; the
        # verbose latency line stays Debug-gated.
        if self._connected:
            now = time.time()
            last_lat = getattr(self, "_last_lat_log", 0.0)
            if now - last_lat > 1.0:
                self._last_lat_log = now
                try:
                    stats = self._speaker_out.drain_latency_stats()
                except Exception:
                    stats = None
                if stats:
                    new_underruns = stats.get("underruns_since_drain", 0)
                    # Accumulate for the [health] line (drained there).
                    self._health_underruns_accum = getattr(
                        self, "_health_underruns_accum", 0) + new_underruns
                    if new_underruns > 0 and (
                            now - getattr(self, "_last_underrun_warn", 0.0)
                            > 5.0):
                        self._last_underrun_warn = now
                        self.log(
                            f"[speaker_out] {new_underruns} underrun(s) "
                            f"in the last ~1s "
                            f"(total={stats['underruns_total']}, "
                            f"cb_max={stats['max_ms']:.2f}ms)"
                        )
                    if self._debug_enabled:
                        warn = " (OVER max_block_frames!)" if stats[
                            "over_max_block"] else ""
                        self.log(
                            f"[speaker_out] cb_latency "
                            f"n={stats['n']} "
                            f"mean={stats['mean_ms']:.2f}ms "
                            f"max={stats['max_ms']:.2f}ms "
                            f"underruns_total={stats['underruns_total']}{warn}"
                        )

        # Slice-coverage telemetry. Diagnostic for the "random source
        # flashes during playback" reports. If coverage stays below
        # 100% for long stretches AND the playhead is reading from
        # un-patched chunks at the moment the user hears a flash, the
        # server simply isn't keeping every region of the loop fresh —
        # that's a server-side scheduler concern, but we'll know.
        if self._debug_enabled and self._connected:
            now = time.time()
            last_cov = getattr(self, "_last_cov_log", 0.0)
            if now - last_cov > 1.0:
                self._last_cov_log = now
                try:
                    pct = self._ring.coverage_fraction() * 100.0
                    pos = self._ring.position
                    in_patched = self._ring.is_patched_at(pos)
                    pos_s = pos / wire.SAMPLE_RATE
                    r = self._router
                    n_slices = r.n_slices if r is not None else 0
                    self.log(
                        f"[coverage] {pct:.1f}% patched "
                        f"(slices_recv={n_slices}) "
                        f"playhead@{pos_s:.1f}s "
                        f"in_patched={in_patched}"
                    )
                except Exception as e:
                    if not getattr(self, "_cov_log_err_done", False):
                        self._cov_log_err_done = True
                        self.log(f"coverage telemetry raised: {e}")

        # NOTE: OnTick no longer sends params. The dedicated pacer
        # THREAD (src/params_pacer.py) owns the continuous params
        # stream — the keepalive — at a steady ~16 ms cadence that
        # survives TD main-thread hitches. OnTick's remaining jobs are
        # the queue drain, curve sampling (TD pars — main thread only),
        # and the telemetry above. The dirty→snapshot merge moved into
        # _build_params_message (on the pacer thread, under _lock).

    def OnHeartbeat(self) -> None:
        """Timer CHOP compat shim. Heartbeat HTTP now runs on a
        background worker (src/queue_worker.py) — the synchronous
        /api/queue/status poll used to block the TD main thread for an
        HTTPS round-trip (fresh TLS handshake!) every 5 s, stalling the
        params keepalive AND slice patching: the periodic "occasionally
        choppy" audio. This shim just makes sure the worker is alive."""
        self._last_heartbeat_t = time.time()
        self._ensure_hb_worker()

    def _ensure_hb_worker(self) -> None:
        """Create/start the heartbeat worker if it isn't running.
        Idempotent + cheap (an is_alive check); the frame driver calls
        this on a ~1 s throttle as belt-and-suspenders, same philosophy
        as MaybeTickFromFrame. The worker itself idles (0.5 s poll of
        get_state) while there's no hosted session, so it's safe to
        keep alive across reconnects."""
        w = self._hb_worker
        if w is not None and w.is_alive:
            return

        # Closures read ONLY plain attributes — the worker thread must
        # never touch TD pars. _queue_base/_api_key/_session_id are
        # written on the main thread; reads are GIL-atomic.
        def _get_state():
            if not self._connected or not self._session_id:
                return None
            return (self._queue_base or "https://music.daydream.live",
                    self._api_key or None,
                    self._session_id)

        def _pop_extend():
            # Only the main thread sets True; only the worker clears.
            if self._hb_extend_requested:
                self._hb_extend_requested = False
                return True
            return False

        self._hb_worker = queue_worker_mod.QueueHeartbeatWorker(
            get_state=_get_state,
            pop_extend_flag=_pop_extend,
            post_event=lambda kind, payload: self._inbound.put(
                (kind, payload)),
            client_factory=lambda base, key: queue_mod.QueueClient(
                base, api_key=key),
            stats=self._stats,
            log=self.log,
        )
        self._hb_worker.start()
        if self._debug_enabled:
            self.log("[hb-worker] started")

    def _request_extend(self, auto: bool = False) -> None:
        """Ask the heartbeat worker to POST /api/queue/extend on its next
        cycle (≤5 s away — fine against the 60 s auto-extend threshold).
        Replaces the old synchronous _extend_session (main-thread HTTP)."""
        if not self._session_id:
            self.log("extend: no session id (not in hosted mode?)")
            return
        self._hb_extend_ctx = (
            ("auto", self._extensions_used) if auto else ("user", None))
        self._hb_extend_requested = True
        if not auto:
            self._set_status("Extending session...")
        self._ensure_hb_worker()

    def _apply_queue_status(self, resp, dur_ms: float) -> None:
        """Main-thread handler for an `hb-status` event. All the TD par
        writes / status transitions from the old OnHeartbeat live here,
        unchanged in behavior — only the HTTP moved off-thread."""
        if not self._connected:
            return  # stale poll result raced a disconnect — drop it
        self._last_heartbeat_t = time.time()
        self._heartbeat_count += 1

        # Periodic "still alive" log so a regression where heartbeats
        # stop is visible in textport instead of silently letting
        # sessions die. Debug-gated, 1st + every 6th (~30 s at 5 s).
        if self._debug_enabled and (
            self._heartbeat_count == 1 or self._heartbeat_count % 6 == 0
        ):
            try:
                expires_in = float(self._read_par("Expiresin", 0.0) or 0.0)
            except Exception:
                expires_in = 0.0
            self.log(
                f"[heartbeat] #{self._heartbeat_count} ok "
                f"status={resp.status} expires_in={expires_in:.0f}s "
                f"extensions={self._extensions_used} "
                f"http={dur_ms:.0f}ms"
            )

        if resp.status == "active":
            expires_in_s = 0.0
            if resp.expires_at:
                self._expires_at_ms = resp.expires_at
                now_ms = time.time() * 1000
                expires_in_s = max(0.0, (resp.expires_at - now_ms) / 1000)
                self._write_par("Expiresin", expires_in_s)
            self._extensions_used = (
                resp.extensions_used or self._extensions_used
            )

            # Auto-extend: pre-emptively extend before the lease expires.
            # Threshold is 60 s — plenty of margin for the worker's ≤5 s
            # pickup plus the request itself. The denied-extend backoff
            # (set in _apply_extend_result) prevents looping once the
            # server stops granting extensions.
            try:
                auto_extend = bool(self._read_par("Autoextend", True))
            except Exception:
                auto_extend = True
            if (
                auto_extend
                and expires_in_s > 0.0
                and expires_in_s < 60.0
                and not self._hb_extend_requested
                and time.time() >= getattr(
                    self, "_auto_extend_backoff_until", 0.0)
            ):
                if self._debug_enabled:
                    self.log(
                        f"[auto-extend] expires_in={expires_in_s:.0f}s "
                        f"< 60s — requesting extend "
                        f"(extensions_used={self._extensions_used})"
                    )
                self._request_extend(auto=True)
        elif resp.status == "queued":
            # Reservation lost server-side. Surface for awareness; leave
            # the WS alone — the server will close it if it actually
            # evicted us. (Same defensive posture as the RTMG VST.)
            self._write_par("Queueposition", resp.position or 0)
            self._set_status(
                f"Server requeued us (position {resp.position or '?'})"
            )
        elif resp.status == "over_budget":
            deny = resp.deny_reason or "(no reason)"
            self._write_par("Denyreason", deny)
            self._set_status(f"Paywall: {deny}")
        else:
            # Unexpected / unparseable status — most often a transient or
            # slightly-malformed status-poll response that QueueResponse
            # defaults to "unknown". Do NOT tear down on this alone: the
            # authoritative "session ended" signal is the WS closing,
            # which we handle in the close callback. A single odd poll
            # was killing otherwise-healthy live sessions, so treat it as
            # transient and keep the WS — the next poll usually recovers.
            self.log(
                f"Heartbeat saw status={resp.status!r}; ignoring (transient — "
                f"WS close is the terminal signal, not a status poll)"
            )

    def _apply_extend_result(self, payload) -> None:
        """Main-thread handler for an `hb-extend` event:
        ("ok", QueueResponse) | ("err", message)."""
        kind, data = payload
        ctx_kind, pre_used = self._hb_extend_ctx
        auto = (ctx_kind == "auto")
        if kind == "err":
            self.log(f"Extend failed: {data}")
            if auto:
                # Network blip — retry on a later heartbeat.
                self._auto_extend_backoff_until = time.time() + 5.0
            else:
                self._set_status(f"Extend failed: {data}")
            return
        resp = data
        if resp.expires_at:
            self._expires_at_ms = resp.expires_at
            now_ms = time.time() * 1000.0
            self._write_par(
                "Expiresin",
                max(0.0, (resp.expires_at - now_ms) / 1000.0),
            )
        self._extensions_used = resp.extensions_used or self._extensions_used
        if auto:
            if pre_used is not None and self._extensions_used == pre_used:
                # Extend didn't bump the counter — server rejected
                # (likely MAX_EXTENSIONS). Back off for the rest of
                # this session to avoid log spam.
                self.log(
                    "[auto-extend] extend didn't increment "
                    "extensions_used — backing off until next session"
                )
                self._auto_extend_backoff_until = time.time() + 24 * 3600
        else:
            self._set_status("Extended")

    # -------- params pacer ----------------------------------------------------

    def _build_params_message(self) -> str | None:
        """Build one params keepalive message. Runs on the PACER THREAD —
        must stay TD-free: plain attributes, DemonExt._lock, the
        LoopBuffer's own lock, and the enabled-LoRA cache only.

        Merges `_dirty` into `_params_snapshot` first (dirty wins, the
        snapshot stays current — this also fixes the old starvation bug
        where _drain_inbound consumed `_dirty` before OnTick could fold
        it into the snapshot, so the snapshot never saw user edits).
        An EMPTY raw dict still produces a message: that's the
        keepalive. Gated on `_connected`, NOT `_saw_ready` — the
        pre-ready params traffic is empirically load-bearing (pods
        1011-idle-close during the initial VAE encode without it).
        """
        if not self._connected or self._wsc is None:
            return None
        with self._lock:
            if self._dirty:
                self._params_snapshot.update(self._dirty)
                self._dirty.clear()
            raw = dict(self._params_snapshot)
            blend_targets = dict(self._blend_targets)
        # Filter BEFORE the glide step so disabled-LoRA keys vanish from
        # the target set and the engine drops their debounce state.
        raw = P.filter_params_for_wire(raw, self._enabled_loras_cache)
        # Send shaping: lora_str_* debounce (one refit per gesture) +
        # blend glide. Blends ride along through the same engine, then
        # get popped — they must NEVER stay in the params raw dict (the
        # server's params handler rejects them and closes the WS).
        shaped = self._glide.step({**raw, **blend_targets})
        for key, sender in self._blend_senders.items():
            if key not in blend_targets:
                continue
            v = shaped.pop(key, None)
            if v is None:
                continue
            if sender.poll(float(v)):
                try:
                    if key == "prompt_blend":
                        self._pacer_send(
                            wire.encode_set_prompt_blend(float(v)))
                    else:
                        self._pacer_send(
                            wire.encode_set_timbre_strength(float(v)))
                except Exception:
                    # Blend sends are best-effort; the params stream's
                    # fail streak is the dead-socket detector.
                    pass
        # Belt-and-suspenders: strip any blend key that slipped through.
        for key in self._blend_senders:
            shaped.pop(key, None)
        # The LoopBuffer's actual read position (in seconds) — mirrors
        # demon-public-demo's session.player.positionSec.
        playback_sec = self._ring.position / wire.SAMPLE_RATE
        return wire.encode_params(shaped, playback_sec)

    def _pacer_send(self, msg: str) -> bool:
        """Enqueue-only send for the pacer thread. NEVER route through
        _send_text — its failure handling calls Disconnect() (TD par
        writes, main-thread only). Failures count in the pacer's
        send_fail_streak, polled by the main thread."""
        w = self._wsc
        if w is None:
            return False
        try:
            return bool(w.send_text(msg))
        except Exception:
            return False

    def _ensure_pacer(self) -> None:
        """Create/start the params pacer if it isn't running. Idempotent;
        the main thread calls this at connect and from the per-frame
        watchdog (belt-and-suspenders — this stream is the keepalive,
        we never trust a single driver; see MaybeTickFromFrame's
        history)."""
        p = self._pacer
        if p is not None and p.is_alive:
            return
        self._pacer = params_pacer_mod.ParamsPacer(
            build_message=self._build_params_message,
            send=self._pacer_send,
            stats=self._stats,
            log=self.log,
        )
        self._pacer.start()
        if self._debug_enabled:
            self.log("[pacer] params pacer thread started")

    def _refresh_enabled_loras_cache(self) -> None:
        """Re-read the Loraenable* pars (main thread!) into the plain
        frozenset the pacer thread filters against. Call after anything
        that changes LoRA enable state: catalog apply, Loraenable
        toggles, Connect."""
        try:
            self._enabled_loras_cache = frozenset(self._enabled_loras())
        except Exception as e:
            self.log(f"_refresh_enabled_loras_cache raised: {e}")

    def MaybeHeartbeatFromFrame(self) -> None:
        """Belt-and-suspenders heartbeat-WORKER keeper, called from
        frame_exec's onFrameStart every TD frame (~16 ms).

        The heartbeat HTTP itself runs on the background worker; this
        just makes sure that worker thread is alive (it could die only
        to a catastrophic failure, but the params stream taught us to
        never trust a single driver). Throttled to ~1 s; cheap no-op
        when there's no hosted session.
        """
        # No hosted session = nothing to keep alive. Don't even check
        # the throttle window; spammy no-ops here happen 60×/sec.
        if not self._connected or not self._session_id:
            return
        mode = (self._read_par("Mode", "direct") or "direct").lower()
        if mode != "hosted":
            return
        now = time.time()
        if now - self._last_hb_ensure_t < 1.0:
            return
        self._last_hb_ensure_t = now
        try:
            self._ensure_hb_worker()
        except Exception as e:
            self.log(f"MaybeHeartbeatFromFrame: _ensure_hb_worker "
                     f"raised: {e}")

    def MaybeTickFromFrame(self) -> None:
        """Drive OnTick from frame_exec's onFrameStart when the Timer CHOP
        is silent.

        WHY THIS IS CRITICAL: OnTick flushes the continuous-param stream.
        After `ready`, that stream is the ONLY traffic keeping the pod's
        WS alive (the pod has no separate keepalive — confirmed against
        demon-public-demo's useParamSync, which sends params every 8 ms).
        The tick8ms Timer CHOP has been non-firing in practice (same
        TD-callback gremlin that kept OnHeartbeat silent), so OnTick never
        ran → demonTD went dead-quiet after `ready` → the pod idle-timed-
        out and closed before streaming a single generation slice. That's
        the "connects, gets the loop, then drops, no Daydream output" bug.

        frame_exec is the reliable driver (verified firing). We run OnTick
        from it on a ~33 ms floor (≈2 frames @ 60 fps → ~30 params/s,
        plenty to keep the pod alive and stay responsive). No-op if the
        Timer CHOP is actually feeding (gate on `_last_tick_t`) or if the
        WS isn't up yet.
        """
        if not self._connected:
            return
        now = time.time()
        # If OnTick ran very recently (live Timer CHOP, or already this
        # frame), skip — don't double-drive the param stream.
        if now - self._last_tick_t < 0.033:
            return
        if self._last_tick_t == 0.0 and self._debug_enabled:
            self.log(
                "[tick] Timer CHOP appears silent — "
                "driving OnTick from frame_exec fallback"
            )
        try:
            self.OnTick()
        except Exception as e:
            self.log(f"MaybeTickFromFrame: OnTick raised: {e}")

    def OnPlayStateChange(self, state) -> None:
        """TD timeline play-state change. Routed from frame_exec's
        `onPlayStateChange(state)` callback.

        When the user pauses TD's timeline, pause SpeakerOut so the
        audio thread emits silence and the LoopBuffer playhead freezes;
        on un-pause, audio resumes from the same sample. The WS + queue
        heartbeats keep running throughout — pausing the timeline is a
        "stop hearing audio" gesture, not a session teardown (that's
        Disconnect).

        Does NOT touch source resolution or the WS connection path, so
        it cannot affect whether Connect succeeds.

        `state` is truthy when playing, falsy when paused; coerced
        defensively because the exact type has varied across TD builds.
        """
        playing = bool(state)
        if self._debug_enabled:
            self.log(f"OnPlayStateChange: state={state!r} → "
                     f"{'PLAY' if playing else 'PAUSE'}")
        try:
            self._speaker_out.set_paused(not playing)
        except Exception as e:
            self.log(f"OnPlayStateChange: set_paused failed: {e}")

    def OnReceive(self, dat, rowIndex=None, message=None,
                  contents=None, peer=None) -> None:
        """WebSocket DAT callback for incoming messages.

        TD's onReceiveText passes a string in `message`. onReceiveBinary
        passes raw bytes in `contents`. (Older versions passed it as `bytes`
        — see callbacks DAT shim.)

        We log every entry so we can diagnose if/why the server's `ready`
        message doesn't arrive.
        """
        try:
            self.log(f"OnReceive: message={'<text len=' + str(len(message)) + '>' if isinstance(message, str) else None} "
                     f"contents={'<binary len=' + str(len(contents)) + '>' if isinstance(contents, (bytes, bytearray)) else None}")
            if isinstance(contents, (bytes, bytearray)) and len(contents) > 0:
                self._on_binary(contents)
            elif isinstance(message, str) and message:
                self._on_text(message)
        except Exception as e:
            self.log(f"OnReceive error: {type(e).__name__}: {e}")

    # -------- WS open + I/O --------------------------------------------------

    def _open_ws(self, ws_url: str) -> None:
        """Open a Python WebSocket to DEMON.

        We do NOT use TD's built-in WebSocket DAT — its sendBinary silently
        fails on payloads above ~few MB. Instead we run a `websocket-client`
        connection in a background thread (see ws_client.py).

        Resolution order:
          1. Snapshot init params for the revert-on-mid-session-edit guard.
          2. Resolve source audio (afconvert may take seconds).
          3. Stash _pending_* so the on_open callback can flush them.
          4. Construct WSClient and connect.
        """
        # 1. Snapshot init params for revert-on-mid-session-edit
        self._last_init_values = self._collect_init_params()

        # 2. Resolve source audio (slow)
        self._set_status("Loading source audio...")
        cfg = self._build_session_config()
        pcm = self._resolve_source_pcm()
        if pcm is None:
            self._set_status(
                "Set Source Audio File or wire an Audio File In CHOP, then reconnect"
            )
            return

        # 3. Stash for the on_open callback.
        sf = self._read_par("Sourcefile", "")
        if sf:
            source_label = os.path.basename(sf)
        else:
            wired = self._wired_chop_file_path()
            source_label = (f"wired CHOP file: {os.path.basename(wired)}"
                            if wired else "wired CHOP snapshot")
        self._pending_config = wire.encode_config(cfg)
        self._pending_audio = wire.encode_audio_frame(pcm, channels=2)
        self._pending_source_label = source_label
        self._pending_audio_samples = pcm.shape[1]
        self.log(f"_open_ws: pending {pcm.shape[1]} samples "
                 f"({pcm.shape[1] / wire.SAMPLE_RATE:.2f}s) from {source_label}")
        # Optional debug: dump the EXACT PCM we're about to encode + send
        # so a user can verify on disk what's leaving the client. Behind
        # the Debug toggle so the .tox doesn't write to /tmp every Connect.
        if self._debug_enabled:
            try:
                peak = float(np.max(np.abs(pcm))) if pcm.size > 0 else 0.0
                mabs = float(np.mean(np.abs(pcm))) if pcm.size > 0 else 0.0
                self.log(f"[DIAG sent_to_server] shape={pcm.shape} "
                         f"dtype={pcm.dtype} peak={peak:.4f} mean_abs={mabs:.4f}")
            except Exception:
                pass
            self._dump_wav(
                os.path.join(DEBUG_DUMP_DIR, "sent_to_server.wav"),
                pcm, channels=2,
            )

        # 4. Close any prior client, build a new one, connect.
        if self._wsc is not None:
            try:
                self._wsc.close()
            except Exception:
                pass
            self._wsc = None

        # Detach the old router FIRST: WSClient.close() joins its recv
        # thread for only 2 s, and a thread stuck in a long send (30 s
        # timeout) lingers past that — detached, it can no longer patch
        # the NEW session's ring or post stale loop-initialized events.
        old_router = self._router
        if old_router is not None:
            old_router.detach()
        self._router = binary_router_mod.BinaryRouter(
            ring=self._ring,
            post_event=lambda kind, payload: self._inbound.put(
                (kind, payload)),
            stats=self._stats,
            # Per-router decompressor: ZstdDecompressor is not
            # concurrency-safe and recv threads can briefly overlap
            # across reconnects. _ZSTD_DEC stays as the capability
            # probe only (drives compression: none in SessionConfig).
            zstd_dec=(zstd.ZstdDecompressor()
                      if _ZSTD_DEC is not None else None),
            log=self.log,
            is_debug=lambda: self._debug_enabled,
            debug_dump=lambda name, pcm, ch: self._dump_wav(
                os.path.join(DEBUG_DUMP_DIR, name), pcm, ch),
        )

        self._set_status(f"Opening {ws_url}...")
        # New connection generation — events from older generations'
        # recv threads are dropped in _drain_inbound (see _ws_gen).
        self._ws_gen += 1
        gen = self._ws_gen
        try:
            self._wsc = ws_client_mod.WSClient(
                url=ws_url,
                on_open=lambda g=gen: self._on_ws_open(g),
                on_text=lambda m, g=gen: self._on_ws_text(m, g),
                on_binary=lambda b, g=gen: self._on_ws_binary(b, g),
                on_close=lambda c, r, g=gen: self._on_ws_close(c, r, g),
                log=self.log,
                timeout=30.0,
            )
            self._wsc.connect()
            self.log(f"_open_ws: WSClient.connect() scheduled (thread starting)")
            # Start the params pacer — it no-ops (build_message → None)
            # until _connected flips True on open, then streams the
            # keepalive every ~16 ms regardless of TD frame hitches.
            self._ensure_pacer()
        except Exception as e:
            self.log(f"_open_ws: WSClient construct/connect failed: {e}")
            self._set_status(f"WS open failed: {e}")
            self._wsc = None

    # --- WSClient callbacks (background recv thread) -------------------------
    #
    # CRITICAL: these run on the websocket-client recv thread. TD forbids
    # touching any operator from a non-main thread (raises a modal dialog,
    # may even crash). All we do here is enqueue the event. The main thread
    # drains the queue from OnTick().

    def _on_ws_open(self, gen: int | None = None) -> None:
        self._inbound.put(("open", None, gen))

    def _on_ws_text(self, msg: str, gen: int | None = None) -> None:
        # Sniff FIRST (recv thread): ready/swap_ready/stem_assets set
        # the router's binary-routing state before the next binary frame
        # arrives on this same thread. The full text processing still
        # happens on the main thread via the queue.
        r = self._router
        if r is not None:
            r.sniff_text(msg)
        self._inbound.put(("text", msg, gen))

    def _on_ws_binary(self, payload: bytes, gen: int | None = None) -> None:
        # Decode + patch INLINE on the recv thread (TD-free; the
        # LoopBuffer has its own lock) — binary frames are NOT queued to
        # the main thread anymore, so slice patching survives TD
        # main-thread hitches. See src/binary_router.py.
        r = self._router
        if r is not None:
            r.handle_binary(payload)
        else:
            self._inbound.put(("binary", payload, gen))

    def _on_ws_close(self, code, reason, gen: int | None = None) -> None:
        self._inbound.put(("close", (code, reason), gen))

    @staticmethod
    def event_is_stale(event_gen: int | None, current_gen: int) -> bool:
        """True iff an _inbound event came from a previous connection's
        recv thread and must be dropped. gen=None marks events that are
        not connection-scoped (heartbeat results, failover ticks,
        loop-initialized) — always processed."""
        return event_gen is not None and event_gen != current_gen

    def _drain_inbound(self) -> None:
        """Main-thread per-frame work. Called by frame_exec every frame:
           1. Drain WS recv-thread events into TD-safe handlers.
           2. Supervise the params-pacer thread (which owns the
              continuous params keepalive — see src/params_pacer.py).
           3. Periodic telemetry log.
        """
        # 0. Main-thread cadence telemetry: a long gap between
        # _drain_inbound calls IS a TD main-thread hitch (heavy cook,
        # UI interaction, blocking I/O). These are the events that used
        # to stall the params keepalive and slice patching.
        now_mono = time.monotonic()
        if self._last_drain_t > 0.0:
            self._stats.note_drain_gap((now_mono - self._last_drain_t)
                                       * 1000.0)
        self._last_drain_t = now_mono

        # 1. Drain inbound queue
        max_per_tick = 64
        for _ in range(max_per_tick):
            try:
                item = self._inbound.get_nowait()
            except queue.Empty:
                break
            kind, payload = item[0], item[1]
            ev_gen = item[2] if len(item) > 2 else None
            if self.event_is_stale(ev_gen, self._ws_gen):
                # A previous connection's recv thread limped past its
                # 2 s close-join and enqueued late. Processing its
                # "close" here would tear down the CURRENT session.
                if self._debug_enabled:
                    self.log(f"[ws] dropping stale {kind!r} event "
                             f"(gen {ev_gen} != {self._ws_gen})")
                continue
            try:
                if kind == "open":
                    self.log("[ws_client] open — flushing config + audio")
                    self._flush_pending()
                elif kind == "text":
                    if self._debug_enabled:
                        self.log(f"[ws_client] <- text {len(payload)}B: {payload[:120]!r}")
                    self._on_text(payload)
                elif kind == "binary":
                    self._on_binary(payload)
                elif kind == "loop-initialized":
                    self._on_loop_initialized(payload)
                elif kind == "close":
                    code, reason = payload
                    self.log(f"[ws_client] closed code={code} reason={reason!r}")
                    self._connected = False
                    self._handle_ws_close(reason)
                elif kind == "hb-status":
                    resp, dur_ms = payload
                    self._apply_queue_status(resp, dur_ms)
                elif kind == "hb-error":
                    msg, dur_ms = payload
                    # Transient — keep the WS alive; the worker retries
                    # on its next 5 s cycle. Mark the attempt so the
                    # "still alive" bookkeeping doesn't pile on.
                    self._last_heartbeat_t = time.time()
                    self.log(f"Heartbeat poll failed: {msg}")
                elif kind == "hb-extend":
                    self._apply_extend_result(payload)
                elif kind == "failover-tick":
                    # Failover worker (spawned by _handle_ws_close) is
                    # back on the main thread asking us to re-call the
                    # hosted-join flow. payload is (base, api_key) as
                    # captured at close time — but prefer the CURRENT
                    # api key: the user may have pasted a fresh one
                    # during the backoff (retrying with the stale
                    # captured key would fail auth).
                    base, api_key = payload
                    ok = self._hosted_join_and_open(
                        base=base, api_key=(self._api_key or api_key),
                        is_retry=True)
                    if not ok:
                        # Join refused / paywall / etc. — give up
                        # rather than retry-stormulously. Status is
                        # already set by _hosted_join_and_open.
                        self.log(
                            f"[failover] _hosted_join_and_open returned "
                            f"False on retry {self._failover_attempts}; "
                            f"stopping.")
            except Exception as e:
                self.log(f"_drain_inbound({kind}) error: {type(e).__name__}: {e}")

        # 2. Params-pacer watchdog. The pacer THREAD owns the continuous
        # params stream now (src/params_pacer.py) — sending from here
        # (the frame loop) meant any main-thread hitch silenced the
        # keepalive AND froze playback_pos, the server's pacing signal.
        # The main thread's remaining job is supervision:
        #   * thread died → recreate (belt-and-suspenders, house style)
        #   * sends failing repeatedly → socket is dead; tear down once
        #     (the pacer itself must never call Disconnect — TD pars).
        if self._connected:
            pacer = self._pacer
            if pacer is None or not pacer.is_alive:
                self.log("[pacer] thread not running — restarting")
                self._ensure_pacer()
            elif pacer.send_fail_streak >= self._SEND_FAIL_LIMIT:
                self._teardown_dead_connection(
                    f"params stream: {pacer.send_fail_streak} consecutive "
                    f"send failures")
            elif (self._saw_ready
                    and pacer.last_send_age() > 1.0):
                now = time.time()
                if now - self._last_pacer_warn_t > 5.0:
                    self._last_pacer_warn_t = now
                    self.log(
                        f"[pacer] params stream stalled "
                        f"({pacer.last_send_age():.1f}s since last send) — "
                        f"watchdog poking")
                self._ensure_pacer()

        # 3. Telemetry (~every 2 s) — gated behind Debug. Once we know the
        # chain works, these counters are pure noise.
        if self._debug_enabled and self._connected:
            now = time.time()
            last = getattr(self, "_last_telem_log", 0.0)
            if now - last > 2.0:
                self._last_telem_log = now
                buffered = self._ring.available
                buf_s = buffered / wire.SAMPLE_RATE
                r = self._router
                n_bin = r.n_binary_frames if r is not None else 0
                n_cook = getattr(self, "_n_cook_recv", 0)
                self.log(
                    f"telemetry: buffered={buffered} ({buf_s:.2f}s)  "
                    f"binary_frames_recv={n_bin}  audio_out_cooks={n_cook}"
                )

        # 4. Smoothness [health] line (~every 5 s while connected).
        # Debug mode gets the full line every period; otherwise we only
        # speak up when something actually degraded (late patches,
        # underruns, or a main-thread hitch >250 ms — the server's lead
        # floor is 0.25 s, so a hitch that size is exactly when slices
        # start landing behind the playhead).
        if self._connected:
            now = time.time()
            if now - self._last_health_log > 5.0:
                self._last_health_log = now
                try:
                    snap = self._stats.drain()
                    underruns = getattr(self, "_health_underruns_accum", 0)
                    self._health_underruns_accum = 0
                    degraded = (
                        snap["patches_late"] > 0
                        or underruns > 0
                        or snap["drain_gap_max_ms"] > 250.0
                    )
                    if self._debug_enabled or degraded:
                        line = telemetry_mod.SmoothnessStats.format_line(
                            snap, underruns_since=underruns)
                        self.log(f"[health] {line}")
                except Exception as e:
                    if not getattr(self, "_health_log_err_done", False):
                        self._health_log_err_done = True
                        self.log(f"[health] telemetry raised: {e}")

    @staticmethod
    def _parse_ws_url(url: str) -> tuple[str | None, int]:
        """ws://host:port/path → ('host', port). Defaults port 80/443."""
        try:
            from urllib.parse import urlparse
            u = urlparse(url)
            if u.scheme not in ("ws", "wss") or not u.hostname:
                return None, 0
            default_port = 443 if u.scheme == "wss" else 80
            return u.hostname, u.port or default_port
        except Exception:
            return None, 0

    def OnWsConnect(self, dat) -> None:
        """Called by the callbacks DAT's onConnect. We held back the config
        + source-audio frames until the socket was actually open."""
        try:
            self.log(f"OnWsConnect: ws connected ({dat.par.netaddress.eval()})")
        except Exception:
            self.log("OnWsConnect: ws connected")
        self._flush_pending()

    def _flush_pending(self) -> None:
        cfg = getattr(self, "_pending_config", None)
        audio = getattr(self, "_pending_audio", None)
        if cfg is None or audio is None:
            return
        # Reset session-state counters at the start of every successful flush.
        # (Binary-frame/slice counters live in the per-connection
        # BinaryRouter now — fresh instance every _open_ws.)
        self._playback_pos = 0
        self._n_cook_recv = 0
        # `_auto_enable_done` was the gate for the v0.1.x bach
        # auto-enable. v0.2.4 removed that — user LoRA toggles are now
        # the source of truth via SessionConfig.enabled_loras. No reset
        # needed.
        self._lora_catalog_sig = None
        self.log("_flush_pending: sending config + audio")
        try:
            self.log(f"_flush_pending: config = {cfg}")
        except Exception:
            pass
        self._send_text(cfg)
        self._send_bytes(audio)
        self._connected = True
        self._set_status("Connected")
        self.log(
            f"sent {self._pending_audio_samples} samples "
            f"({self._pending_audio_samples / wire.SAMPLE_RATE:.2f}s) "
            f"from {self._pending_source_label}"
        )
        # `_pending_config` is one-shot (server only takes one config
        # per WS lifetime). `_pending_audio` is NOT cleared here — the
        # pod-failover path may need to re-send the same source on the
        # next WS open without re-resolving PCM. The `ready` handler
        # clears `_pending_audio` once the server has acknowledged the
        # session, which is the actual "we don't need this anymore"
        # signal.
        self._pending_config = None

    def _convert_to_wav(self, src_path: str) -> str | None:
        """Convert any audio file to a 16-bit 48 kHz stereo WAV in a temp file.

        Tries `afconvert` first (built into macOS), then `ffmpeg` (often
        installed on Mac via brew and standard on Linux/Windows).

        Returns the temp .wav path on success, or None.
        Caller is responsible for unlink-ing the temp file.
        """
        import shutil
        import subprocess
        import tempfile

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        out = tmp.name

        # macOS afconvert.
        # IMPORTANT: do NOT pass --channellayout. It makes afconvert write
        # WAVE_FORMAT_EXTENSIBLE (format code 65534), which Python's stdlib
        # `wave` module rejects with 'unknown format: 65534'. Plain
        # LEI16@48000 produces vanilla PCM WAV (format 1).
        if shutil.which("afconvert"):
            try:
                r = subprocess.run(
                    ["afconvert", "-f", "WAVE", "-d", "LEI16@48000",
                     src_path, out],
                    capture_output=True, timeout=120,
                )
                if r.returncode == 0 and os.path.getsize(out) > 44:
                    self.log(f"_convert_to_wav: afconvert -> {os.path.basename(out)}")
                    return out
                else:
                    err = (r.stderr or b"").decode("utf-8", "replace").strip()
                    if err:
                        self.log(f"_convert_to_wav: afconvert rc={r.returncode}: {err[:200]}")
            except Exception as e:
                self.log(f"_convert_to_wav: afconvert failed: {e}")

        # ffmpeg
        if shutil.which("ffmpeg"):
            try:
                r = subprocess.run(
                    ["ffmpeg", "-y", "-i", src_path,
                     "-acodec", "pcm_s16le", "-ar", "48000", "-ac", "2", out],
                    capture_output=True, timeout=120,
                )
                if r.returncode == 0 and os.path.getsize(out) > 44:
                    self.log(f"_convert_to_wav: ffmpeg -> {os.path.basename(out)}")
                    return out
            except Exception as e:
                self.log(f"_convert_to_wav: ffmpeg failed: {e}")

        try:
            os.unlink(out)
        except Exception:
            pass
        return None

    def _resolve_source_pcm(self,
                            file_par_name: str | None = None,
                            file_path: str | None = None,
                            chop_arg: Any = None) -> "np.ndarray | None":
        """Shared source-audio resolution used by Connect, Swap, Timbre, Structure.

        Order of preference:
          1. explicit file_path arg
          2. file path from `file_par_name` (e.g. 'Timbresourcefile')
          3. wired CHOP input's .par.file (if upstream is an Audio File In)
          4. snapshot of audio_in samples (last resort, may be too short)
        """
        # 1. explicit arg
        if file_path:
            pcm = self._load_source_wav(file_path)
            if pcm is not None:
                return pcm

        # 2. par-driven file path
        if file_par_name:
            par_path = self._read_par(file_par_name, "") or ""
            if par_path:
                pcm = self._load_source_wav(par_path)
                if pcm is not None:
                    return pcm

        # 3. wired CHOP's file
        wired = self._wired_chop_file_path()
        if wired:
            pcm = self._load_source_wav(wired)
            if pcm is not None:
                return pcm

        # 4. snapshot
        return self._snapshot_input_chop()

    def _has_source_audio(self) -> bool:
        """Quick pre-flight check: did the user provide a source?
        Either a Source Audio File par or a wired CHOP with a file par.
        Used by Connect() to bail with a clear error before any WS work."""
        try:
            if (self._read_par("Sourcefile", "") or "").strip():
                return True
        except Exception:
            pass
        return self._wired_chop_file_path() is not None

    def _wired_chop_file_path(self) -> str | None:
        """If an upstream CHOP (e.g. Audio File In) is wired into the COMP's
        first input, return its `par.file` value. Otherwise None.

        TD's Audio File In CHOP exposes a `file` par with the WAV path.
        """
        try:
            upstream_ops = self.ownerComp.inputs or []
        except Exception:
            return None
        for up in upstream_ops:
            if up is None:
                continue
            try:
                file_par = getattr(up.par, "file", None)
                if file_par is None:
                    continue
                path = file_par.eval()
                if path:
                    return path
            except Exception:
                continue
        return None

    def _snapshot_input_chop(self) -> "np.ndarray | None":
        """Snapshot the COMP's wired CHOP input as (2, samples) float32 at 48k.

        Reads from the `audio_in` In CHOP (the COMP's CHOP input port). If
        nothing is wired, or the upstream produces zero samples, returns None.

        This is a one-shot snapshot at Connect time — not continuous streaming.
        """
        try:
            src = self.ownerComp.op("audio_in")
        except Exception:
            return None
        if src is None:
            return None
        try:
            n = int(src.numSamples)
            ch_count = int(src.numChans)
        except Exception:
            return None
        if n <= 0 or ch_count <= 0:
            return None
        try:
            ch_count = min(2, ch_count)
            pcm = np.empty((ch_count, n), dtype=np.float32)
            for i in range(ch_count):
                pcm[i] = np.fromiter(src[i].vals, dtype=np.float32, count=n)
            try:
                src_rate = int(src.rate) if src.rate else wire.SAMPLE_RATE
            except Exception:
                src_rate = wire.SAMPLE_RATE
            if src_rate != wire.SAMPLE_RATE:
                pcm = audio_mod.linear_resample(pcm, src_rate, wire.SAMPLE_RATE)
            pcm = audio_mod.to_stereo(pcm)
            pcm = self._crop_to_max_duration(pcm)
            return pcm.astype(np.float32, copy=False)
        except Exception as e:
            self.log(f"_snapshot_input_chop failed: {e}")
            return None

    def _crop_to_max_duration(self, pcm: np.ndarray) -> np.ndarray:
        """Crop a (channels, frames) PCM array to the first
        `MAX_SOURCE_SECONDS`, aligned to `SAMPLE_POOL_FRAMES`.

        Logs + updates Status when the crop actually trims something.
        Idempotent on already-short input.

        Matches the web client's `engine.max_source_duration_s = 120`
        cap + `SAMPLE_POOL = 9600` pool alignment from
        `demon-public-demo/vendor/demon-ui/lib/audio/trimAudioBuffer.ts`.
        Sources longer than the cap caused "server sent close" right
        after `ready` — the pod's VAE encoder times out on longer
        inputs.
        """
        max_samples = MAX_SOURCE_SECONDS * wire.SAMPLE_RATE
        # Floor-align to pool boundary so the VAE encode constraint
        # holds. 120 s × 48000 = 5_760_000 is already a multiple of
        # 9600, but keep the floor-align in case MAX_SOURCE_SECONDS
        # ever changes to a non-pool-aligned value.
        max_samples = (max_samples // SAMPLE_POOL_FRAMES) * SAMPLE_POOL_FRAMES
        if pcm.shape[1] <= max_samples:
            return pcm
        orig_s = pcm.shape[1] / wire.SAMPLE_RATE
        cropped = pcm[:, :max_samples]
        new_s = cropped.shape[1] / wire.SAMPLE_RATE
        self.log(
            f"source is {orig_s:.1f}s — cropping to {new_s:.1f}s "
            f"(MAX_SOURCE_SECONDS={MAX_SOURCE_SECONDS}, "
            f"pool-aligned to {SAMPLE_POOL_FRAMES} frames). "
            f"Trim your file manually for a different window."
        )
        try:
            self._set_status(
                f"Source cropped to {new_s:.0f}s (max for hosted)")
        except Exception:
            pass
        return cropped

    def _load_source_wav(self, path: str) -> "np.ndarray | None":
        """Load an audio file off disk → (2, samples) float32 at 48 kHz.

        Primary loader is stdlib `wave` (RIFF/WAV, 8/16/32-bit). If that
        fails — typically because the file is MP3 / AAC / M4A / AIFF /
        FLAC — we transparently convert it to a temp WAV via the platform
        converter (`afconvert` on macOS, `ffmpeg` elsewhere) and reload.

        Mono is duplicated to stereo. Source rate is linearly resampled
        to 48 kHz if needed.

        Returns None if the file can't be opened or decoded by any path.
        """
        if not path:
            self.log("_load_source_wav: no Source Audio File set")
            return None
        if not os.path.exists(path):
            self.log(f"_load_source_wav: file not found: {path}")
            return None

        try:
            import wave
            with wave.open(path, "rb") as wf:
                nchannels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                framerate = wf.getframerate()
                nframes = wf.getnframes()
                raw = wf.readframes(nframes)
        except Exception as e:
            # Not a WAV — try converting to WAV first.
            self.log(f"_load_source_wav: {os.path.basename(path)} is not a WAV "
                     f"({e}); attempting auto-conversion...")
            converted = self._convert_to_wav(path)
            if converted is None:
                self.log("_load_source_wav: conversion failed; convert your "
                         "source to a WAV manually (Audacity, QuickTime export, "
                         "ffmpeg) and set Source Audio File.")
                return None
            try:
                import wave
                with wave.open(converted, "rb") as wf:
                    nchannels = wf.getnchannels()
                    sampwidth = wf.getsampwidth()
                    framerate = wf.getframerate()
                    nframes = wf.getnframes()
                    raw = wf.readframes(nframes)
            except Exception as e2:
                self.log(f"_load_source_wav: post-conversion decode failed: {e2}")
                return None
            finally:
                try:
                    os.unlink(converted)
                except Exception:
                    pass

        # Decode raw bytes by sample width.
        try:
            if sampwidth == 2:
                pcm_i16 = np.frombuffer(raw, dtype=np.int16)
                pcm = pcm_i16.astype(np.float32) / 32768.0
            elif sampwidth == 3:
                # 24-bit packed — uncommon path.
                self.log("_load_source_wav: 24-bit WAV not supported; convert to 16-bit or 32-bit float")
                return None
            elif sampwidth == 4:
                # Either int32 or float32. wave doesn't tell us; assume float32.
                pcm = np.frombuffer(raw, dtype=np.float32).copy()
            elif sampwidth == 1:
                pcm_u8 = np.frombuffer(raw, dtype=np.uint8)
                pcm = (pcm_u8.astype(np.float32) - 128.0) / 128.0
            else:
                self.log(f"_load_source_wav: unsupported sample width: {sampwidth}")
                return None
        except Exception as e:
            self.log(f"_load_source_wav: decode failed: {e}")
            return None

        # De-interleave to (channels, samples)
        if nchannels > 1:
            try:
                pcm = pcm.reshape(-1, nchannels).T
            except Exception as e:
                self.log(f"_load_source_wav: de-interleave failed: {e}")
                return None
        else:
            pcm = pcm.reshape(1, -1)

        # Resample to 48 kHz if needed
        if framerate != wire.SAMPLE_RATE:
            pcm = audio_mod.linear_resample(pcm, framerate, wire.SAMPLE_RATE)

        # Force stereo (mono → duplicated L→R; >2 channels → first two)
        pcm = audio_mod.to_stereo(pcm)

        # Cap to MAX_SOURCE_SECONDS (120 s, web-client parity) with
        # pool alignment. See `_crop_to_max_duration` for the full
        # rationale.
        pcm = self._crop_to_max_duration(pcm)

        return pcm.astype(np.float32, copy=False)

    def _build_session_config(self) -> dict[str, Any]:
        """Build the SessionConfig JSON to send right after WS open.

        Matches demon-public-demo's useStartSession.ts buildConfig() exactly,
        in the same field order. Sends all 13 fields every time (the JS
        client does too); the server type allows extras but we don't add
        any to minimize chance of a strict-parser rejection.
        """
        def init_val(td_name: str, default: Any) -> Any:
            return self._read_par(td_name, default)

        # Fallback defaults below match demon-public-demo's
        # useStartSession.ts buildConfig() — only used if the par read
        # somehow fails (par missing, type error, etc.).
        cfg: dict[str, Any] = {
            "sde":          bool(init_val("Sde", False)),
            "lora":         bool(init_val("Lora", True)),
            "depth":        int(init_val("Depth", 4)),
            "vae_window":   float(init_val("Vaewindow", 0.36)),
            "crop":         float(init_val("Crop", 0.0)),
            "steps":        int(init_val("Steps", 8)),
            "fast_vae":     bool(init_val("Fastvae", False)),
            "walk_window":  bool(init_val("Walkwindow", False)),
            "walk_window_s": float(init_val("Walkwindows", 60.0)),
            "enabled_loras": self._enabled_loras(),
            "prompt":       str(init_val("Initprompt",
                "heavy dubstep, deathstep, afxdump, growl heavy bass distortion")),
            # Secondary prompt for A/B blending. The Promptblend continuous
            # param interpolates between `prompt` (A) and `prompt_b` (B).
            # Empty string = no B side, equivalent to always-A. We source
            # `prompt_b` from the LIVE `Promptb` par on the Prompt+LoRA
            # page so it tracks whatever the user has typed at session
            # start — one source of truth, editable mid-session via
            # SendPrompt (matches demon-public-demo's `prompt_b: perf.promptB`).
            "prompt_b":     str(init_val("Promptb", "") or ""),
            "lora_strengths": self._lora_strengths(),
            "fixture_name": str(init_val("Fixturename", "")),
            # Playback-lead tuning (server-side decode buffer). Optional in
            # the protocol ("omit to use server default") but the web client
            # sends them from its config.json defaults, so we do too for
            # parity. Sourced from the Init-page Lead* pars.
            "lead_floor_s":   float(init_val("Leadfloor", 0.25)),
            "lead_ceiling_s": float(init_val("Leadceiling", 1.35)),
            "lead_release_tau_s": float(init_val("Leadreleasetau", 1.5)),
            # Capability gate — when True the server loads the fixture from
            # its own /fixtures cache and the client skips the audio frame
            # upload. The JS client capability-probes via /api/server-info
            # before flipping this to True; we send False unconditionally
            # so the unchanged upload path is used. Sending the field
            # explicitly (vs omitting) makes our intent clear to log
            # readers and keeps demon-public-demo + demonTD on the same
            # SessionConfig surface.
            "use_server_fixture": False,
            # Per-machine identifier the server stashes into loguru contextvars
            # so every pod-side log record on this WS carries it. Makes it
            # possible to grep pod logs by demonTD instance when triaging.
            # demon-public-demo uses PostHog's distinct_id; we reuse the
            # deviceId we already generated for hosted-mode queue joins.
            # `or None` makes encode_config drop the field on the wire when
            # _load_auth somehow didn't populate it.
            "client_id":    self._device_id or None,
        }
        # If we don't have a working zstd decompressor (TD's bundled Python
        # can't load our vendored zstandard binary, etc.), ask the server
        # to emit raw float16 slices instead. Without this, every slice
        # would land with flags=SLICE_FLAG_DELTA and be rejected by
        # decode_slice → no generated audio plays. Trade-off is ~1.5×
        # more bandwidth on the receive path.
        if _ZSTD_DEC is None:
            cfg["compression"] = "none"
        # Saved .toe files keep whatever Vaewindow value the user last had
        # — including the old 6.0 default that the post-2026-06 backend
        # turns into multi-second param-application lag. Warn loudly so
        # the "params are slow" failure mode is self-diagnosing.
        if cfg["vae_window"] > 1.0:
            self.log(
                f"WARNING: vae_window={cfg['vae_window']:.2f}s is much "
                f"larger than the canonical 0.36s — param changes will "
                f"apply SLOWLY. Set the Init-page 'VAE Window' par to "
                f"0.36 and reconnect."
            )
        return cfg

    @staticmethod
    def _lora_par_safe(lid: str) -> str:
        """Sanitize a LoRA id into a TD-legal par-name suffix.

        TD rules: custom par name must begin uppercase, then lowercase
        letters and digits only (no underscores), and a 'sequence parameter'
        cannot end with a digit. We strip non-alphanumerics, lowercase,
        and append 'x' if trailing-digit.
        """
        safe = "".join(c for c in lid if c.isalnum()).lower()
        if safe and safe[-1].isdigit():
            safe += "x"
        return safe or "unnamed"

    def _enabled_loras(self) -> list[str]:
        """Read which LoRAs are currently enabled. Toggle pars use the
        sanitized name (e.g. 'Loraenablebach' for id='bach')."""
        out: list[str] = []
        for lora_id in self._lora_ids:
            safe = self._lora_par_safe(lora_id)
            par = self._par_by_name(f"Loraenable{safe}")
            if par and par.eval():
                out.append(lora_id)
        return out

    def _lora_strengths(self) -> dict[str, float]:
        """Read LoRA strengths to send in the SessionConfig handshake.

        Only includes ENABLED LoRAs — matches demon-public-demo's
        useStartSession.ts `buildConfig()`:
            for (const id of enabledLoras) {
                const v = lora.strengths[id];
                if (typeof v === "number") loraStrengths[id] = v;
            }
        Sending strengths for LoRAs the server hasn't loaded was causing
        disconnects (server-side state mismatch on `params` apply).
        """
        out: dict[str, float] = {}
        enabled = set(self._enabled_loras())
        for lora_id in self._lora_ids:
            if lora_id not in enabled:
                continue
            safe = self._lora_par_safe(lora_id)
            par = self._par_by_name(f"Lorastr{safe}")
            if par:
                out[lora_id] = float(par.eval())
        return out

    # Moved to params.filter_params_for_wire (pure function) so the
    # params-pacer THREAD can call it with the enabled-LoRA cache
    # instead of live TD par reads. Alias kept for any external callers.
    _PARAMS_NOT_FOR_WIRE = P.PARAMS_NOT_FOR_WIRE

    def _filter_params_for_wire(
            self, raw: dict[str, Any]) -> dict[str, Any]:
        """Main-thread wrapper around params.filter_params_for_wire.
        Uses the enabled-LoRA cache (refreshed on every enable-state
        change) so behavior matches the pacer thread exactly."""
        return P.filter_params_for_wire(raw, self._enabled_loras_cache)

    # -------- WS message handlers --------------------------------------------

    def _on_text(self, msg: str) -> None:
        try:
            data = wire.decode_control(msg)
        except Exception as e:
            self.log(f"Bad WS text: {e}")
            return

        kind = data.get("type", "")
        if kind == "ready":
            self.log(f"server ready: ch={data.get('channels')} sr={data.get('sample_rate')}")
            # NOTE: the expecting-initial-buffer flag lives in the
            # BinaryRouter now, set by recv-thread text sniffing
            # (_on_ws_text → router.sniff_text) — by the time we drain
            # this event, the recv thread may already have processed the
            # initial buffer. Nothing binary-routing-related here.
            #
            # Phase-2 contract surface (post-2026-06 backend): ready may
            # carry geometry / capabilities / knob_manifest /
            # lora_pending_enable, and ALSO a `session_id`. Do NOT adopt
            # that session_id — hosted-mode extend/leave key off the
            # QUEUE's session id, not the pod's.
            caps = data.get("capabilities")
            if caps:
                self.log(f"server capabilities: {caps}")
            if self._debug_enabled:
                for k in ("geometry", "knob_manifest",
                          "lora_pending_enable"):
                    if data.get(k) is not None:
                        self.log(f"[ready] {k}={data.get(k)}")
            cat = data.get("lora_catalog") or []
            self._apply_lora_catalog(cat)
            self._seed_dirty_from_current_pars()
            # Pod made it past handshake — failover path is no longer
            # eligible. Reset the failover counters so a future
            # close-after-ready is treated as a genuine disconnect
            # (not "let's try another pod"), and drop _pending_audio
            # since the server has it now. (We hold it across the WS
            # cycle so failover retries can re-send without resolving
            # PCM again — but once we've successfully reached `ready`
            # there's no reason to keep it around.)
            self._saw_ready = True
            self._failover_attempts = 0
            self._pending_audio = None
            self._pending_audio_samples = 0
            # Re-push the per-path interpolation methods so the server
            # matches the menus even after a (re)connect. Mirrors the web
            # client's useInterpSync, which sends the full set on every
            # transition into "ready".
            self._push_interp_methods()
        elif kind == "lora_catalog":
            self._apply_lora_catalog(data.get("catalog") or [])
        elif kind == "params_update":
            # Server-echoed param values; could be displayed but we don't overwrite UI.
            pass
        elif kind == "prompt_applied":
            self.log(f"prompt applied: {data.get('tags')}")
        elif kind == "swap_ready":
            # Logging only. The ring clear + expecting-initial flag now
            # happen in the BinaryRouter's recv-thread sniffer — a clear
            # HERE could land AFTER the recv thread already init'd the
            # NEW track's loop and wipe it (main-thread drain lags the
            # recv thread by design).
            self.log(f"swap_ready ch={data.get('channels')}")
        elif kind in ("timbre_set", "timbre_cleared", "structure_set",
                      "structure_cleared"):
            self.log(kind)
        elif kind in ("timbre_failed", "structure_failed", "swap_failed", "error"):
            self.log(f"server {kind}: {data.get('error') or data.get('message')}")
            self._set_status(f"Error: {kind}")
        elif kind == "command_failed":
            # Post-2026-06 backend: a command was rejected because the
            # pod's capabilities mask gates it (e.g. timbre=false). Make
            # the rejection VISIBLE — silently-ignored rejections look
            # like 'my knob does nothing'.
            cmd = data.get("command") or "(unknown)"
            req = data.get("requires") or "(unknown capability)"
            err = data.get("error") or ""
            self.log(f"server command_failed: command={cmd!r} "
                     f"requires={req!r} error={err!r}")
            self._set_status(f"Server rejected {cmd}: requires {req}")
        elif kind in ("stem_assets", "stem_ready"):
            # Server's stem-separation feature. Two big binary blobs
            # follow (~13 MB each, flag bits we don't decode). The skip
            # counter lives in the BinaryRouter (recv-thread sniffer);
            # this is informational only.
            if self._debug_enabled:
                self.log(f"stem_assets (router skipping "
                         f"{int(data.get('count', 2) or 2)} blobs)")
        elif kind == "stem_failed":
            # Server-side stem extraction failed for an uploaded track.
            # Since we don't have a stems UI, this is informational only —
            # log it visibly so it doesn't hide behind the unknown-kind
            # dedupe.
            err = data.get("error") or "(no reason)"
            fixture = data.get("fixture_name") or "(unknown)"
            self.log(f"server stem_failed: fixture={fixture} error={err}")
        elif kind == "depth_applied":
            # Server ack of a set_depth request, carrying the actually-
            # applied (server-side-clamped) value. We don't send set_depth
            # from TD yet — depth is Init-only — so we'd only see this
            # echo if an MCP client tweaked depth on a shared session.
            # Log for visibility; no par to update.
            self.log(f"server depth_applied: value={data.get('value')}")
        elif kind == "params_echo":
            # MCP-driven param updates. The server emits these when a
            # control bus (not the browser/TD) changes continuous params,
            # so the UI can mirror them. TD has no MCP integration today,
            # so this is decorative — log under Debug only.
            if self._debug_enabled:
                raw = data.get("raw") or {}
                self.log(f"params_echo: {len(raw)} key(s) mirrored from MCP")
        elif kind == "prompt_blend_echo":
            # MCP-driven prompt_blend slider update. Mirror back into the
            # `Promptblend` continuous par so the TD UI reflects whatever
            # an external control bus set. Cheap and useful.
            try:
                value = float(data.get("value", 0.0))
                self._write_par("Promptblend", max(0.0, min(1.0, value)))
                if self._debug_enabled:
                    self.log(f"prompt_blend_echo: mirrored value={value}")
            except Exception as e:
                self.log(f"prompt_blend_echo apply failed: {e}")
        else:
            # Other unrecognized message types — known-unknowns the server
            # may emit but we don't yet handle. Logged once per kind so
            # the textport doesn't spam.
            seen = getattr(self, "_unknown_kinds_seen", set())
            if kind not in seen:
                self.log(f"unknown server message: {kind}")
                seen.add(kind)
                self._unknown_kinds_seen = seen

    def _dump_wav(self, path: str, pcm: np.ndarray, channels: int,
                  sample_rate: int = 48000) -> None:
        """Diagnostic: write a (channels, frames) float32 ndarray as int16
        WAV. Best-effort — failures log but don't propagate."""
        try:
            import wave
            os.makedirs(os.path.dirname(path), exist_ok=True)
            pcm = np.asarray(pcm, dtype=np.float32)
            # Normalize shape to (channels, frames).
            if pcm.ndim == 1:
                # Assume interleaved.
                frames = pcm.shape[0] // channels
                pcm = pcm[: frames * channels].reshape(frames, channels).T
            elif pcm.ndim == 2 and pcm.shape[0] != channels and pcm.shape[1] == channels:
                pcm = pcm.T
            frames = pcm.shape[1]
            # Re-interleave for WAV.
            interleaved = pcm.T.reshape(-1)
            clipped = np.clip(interleaved, -1.0, 1.0)
            i16 = np.int16(clipped * 32767.0)
            with wave.open(path, "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(i16.tobytes())
            self.log(f"_dump_wav: wrote {path} ({frames} frames, ch={channels})")
        except Exception as e:
            self.log(f"_dump_wav failed for {path}: {e}")

    def _on_binary(self, buf: bytes) -> None:
        """Thin delegate to the BinaryRouter. The real path is
        _on_ws_binary → router.handle_binary directly on the recv
        thread; this remains only so the dormant WebSocket-DAT OnReceive
        path can't desync routing state if it's ever revived."""
        r = self._router
        if r is not None:
            r.handle_binary(buf)
        else:
            self.log(f"_on_binary: no router (dropped {len(buf)}B frame)")

    def _on_loop_initialized(self, info: dict) -> None:
        """Main-thread tail of initial-buffer handling: the router (recv
        thread) already decoded + ring.init'd; what's left is the bits
        that touch TD — the log line and starting SpeakerOut (reads the
        Speakerout/Audiodevice pars)."""
        n = int(info.get("frames", 0) or 0)
        ch = int(info.get("channels", 2) or 2)
        self.log(
            f"initial buffer: {n} frames ({n / wire.SAMPLE_RATE:.2f}s) "
            f"ch={ch} — loop initialized"
        )
        # Start Python-side audio playback if the user has it
        # enabled (default True). Bypasses TD's CHOP audio chain.
        #
        # CRITICAL: if speaker_out.start() returns False (PortAudio
        # rejected the device), we keep the WS alive. The user can
        # still get audio out via the COMP's out_chop port wired to
        # an external Audio Device Out CHOP, or fix their device
        # config and retry. Tearing down the hosted session
        # (Disconnect) would force a re-queue which is expensive
        # and doesn't fix the audio problem. start() is idempotent,
        # so the re-fire after a swap_ready re-init is harmless.
        try:
            if bool(self._read_par("Speakerout", True)):
                # Honor the user's output-device pick before opening.
                self._apply_audio_device_selection()
                ok = self._speaker_out.start()
                if not ok:
                    self._set_status(
                        "Audio output failed — try: save & fully "
                        "restart TD; or toggle 'Python Audio Out' "
                        "off and wire the COMP's out to your own "
                        "Audio Device Out CHOP. Details in textport."
                    )
        except Exception as e:
            self.log(f"speaker_out start raised: {e}")
            self._set_status(
                f"Audio output crashed: {type(e).__name__} — "
                f"see textport. Session still active."
            )

    # -------- LoRA catalog ---------------------------------------------------

    def _apply_lora_catalog(self, catalog: list[dict]) -> None:
        """Update Table DAT + dynamic per-LoRA params on the Prompt+LoRA page.

        The server echoes lora_catalog on every state change (e.g. when we
        send enable_lora). Skip redundant work if the catalog shape hasn't
        changed — otherwise we churn the UI 100x/second and starve the
        receive thread.
        """
        def _trig(e: dict) -> str:
            return str((e.get("metadata") or {}).get("primary_trigger_word") or "")

        # IMPORTANT: signature is keyed on the catalog's SHAPE (ids
        # only), NOT on trigger_word. The server may echo the catalog
        # with metadata on first sight and WITHOUT metadata on
        # subsequent state-change echoes — folding metadata into the
        # sig made every echo look different, forcing the Table DAT
        # rewrite + dynamic-par fan-out + UI redraw on every server
        # event. That manifested as severe per-parameter UI lag.
        # Keep the sig id-only so the expensive work runs once per
        # catalog-shape change.
        sig = tuple(sorted(e.get("id", "") for e in catalog))

        # Trigger words always update — but idempotently. An echo that
        # CARRIES a trigger word overwrites; a metadata-less echo
        # preserves whatever we already learned. This way we pick up
        # late-arriving trigger metadata without thrashing the UI.
        ids = [e.get("id", "") for e in catalog if e.get("id")]
        new_triggers = dict(getattr(self, "_lora_triggers", {}))
        for e in catalog:
            lid = e.get("id", "")
            if not lid:
                continue
            t = _trig(e)
            if t:
                new_triggers[lid] = t
            elif lid not in new_triggers:
                new_triggers[lid] = ""
        # Drop entries for LoRAs that left the catalog entirely.
        new_triggers = {k: v for k, v in new_triggers.items() if k in ids}
        with self._lock:
            self._lora_ids = ids
            self._lora_triggers = new_triggers

        if sig == getattr(self, "_lora_catalog_sig", None):
            # Same id list as last time — no Table DAT rewrite, no
            # dynamic-par work. Trigger dict was already refreshed
            # above, which is the cheap path we want every echo to take.
            self._refresh_enabled_loras_cache()
            return
        self._lora_catalog_sig = sig

        table = self.ownerComp.op("lora_catalog")
        if table is not None:
            try:
                table.clear()
                table.appendRow(["id", "name", "default_strength", "trigger_word"])
                for entry in catalog:
                    table.appendRow([
                        entry.get("id", ""),
                        entry.get("name") or entry.get("id", ""),
                        entry.get("strength", 1.0),
                        new_triggers.get(entry.get("id", ""), ""),
                    ])
            except Exception as e:
                self.log(f"lora_catalog write failed: {e}")

        # Dynamically add Toggle + Float par per LoRA on the Prompt+LoRA page.
        # Par names must be TD-legal: start uppercase, only lowercase/digits
        # afterwards, no underscores, no trailing digit. Use _lora_par_safe.
        #
        # NOTE: appendToggle/appendFloat return a ParGroup whose truthiness
        # is unsupported in TD — must check `is not None` not `if tp:`.
        #
        # No DEFAULT_ON / auto-enable in v0.2.4+. The user's saved
        # `Loraenable<id>` toggle values (TD persists custom pars in the
        # .toe) are the source of truth — they flow into
        # SessionConfig.enabled_loras via _enabled_loras() at Connect
        # time, and the server loads exactly those LoRAs. Separately
        # firing enable_lora during catalog refresh was overriding the
        # user's choice every connect (the "still getting bach-sounding
        # stuff even though jazz is on" bug).

        # Reverse map for OnParChange: par-name -> original LoRA id.
        # Both the Loraenable<safe> toggle and the Lorastr<safe>
        # strength fader resolve to the same id. Rebuilt fresh every
        # catalog refresh so stale entries from a removed LoRA can't
        # haunt us.
        self._lora_par_to_id = {}

        try:
            page = self._page_by_name("Prompt+LoRA")
            if page is None:
                self.log("LoRA page: 'Prompt+LoRA' not found")
                return
            existing = {p.name for p in page.pars}
            n_added = 0
            for entry in catalog:
                lid = entry.get("id", "")
                if not lid:
                    continue
                safe = self._lora_par_safe(lid)
                toggle_name = f"Loraenable{safe}"
                strength_name = f"Lorastr{safe}"
                # Wire both forms of the par name back to the original
                # LoRA id so OnParChange can route by name prefix
                # without having to re-sanitize.
                self._lora_par_to_id[toggle_name] = lid
                self._lora_par_to_id[strength_name] = lid

                if toggle_name not in existing:
                    try:
                        tp = page.appendToggle(
                            toggle_name,
                            label=f"{entry.get('name', lid)} on"
                        )
                        if tp is not None:
                            try:
                                # All new toggles default to OFF. User
                                # opts-in per LoRA; this is the only way
                                # to consistently respect user choice
                                # across sessions.
                                tp[0].default = False
                                tp[0].val = False
                            except Exception:
                                pass
                        n_added += 1
                    except Exception as e:
                        self.log(f"LoRA toggle {toggle_name} failed: "
                                 f"{type(e).__name__}: {e}")

                if strength_name not in existing:
                    try:
                        sp = page.appendFloat(
                            strength_name,
                            label=f"{entry.get('name', lid)} strength"
                        )
                        if sp is not None:
                            try:
                                sp[0].normMin = 0.0
                                sp[0].normMax = 1.8
                                sp[0].clampMin = True
                                sp[0].clampMax = True
                                # Honor the catalog's reported strength,
                                # falling back to 1.0. The server's
                                # "strength 0 before loaded" quirk no
                                # longer matters since we send strength
                                # explicitly with each enable_lora.
                                default_strength = float(
                                    entry.get("strength", 1.0))
                                sp[0].default = default_strength
                                sp[0].val = default_strength
                            except Exception:
                                pass
                        n_added += 1
                    except Exception as e:
                        self.log(f"LoRA float {strength_name} failed: "
                                 f"{type(e).__name__}: {e}")
            self.log(f"LoRA page: added {n_added} pars for "
                     f"{len(catalog)} LoRAs")
        except Exception as e:
            self.log(f"LoRA page update failed: {type(e).__name__}: {e}")
        # Now that the dynamic Loraenable* pars exist, sync the pacer
        # thread's filter cache.
        self._refresh_enabled_loras_cache()

    # -------- Pulse handlers -------------------------------------------------

    def _selected_audio_device_index(self) -> int:
        """Parse the Audiodevice menu value to an int device index.
        -1 (or any blank / unparseable value) = system default."""
        try:
            return int(str(self._read_par("Audiodevice", "-1")))
        except Exception:
            return -1

    def _apply_audio_device_selection(self, restart_if_live: bool = False
                                      ) -> None:
        """Push the Audiodevice selection to SpeakerOut. When
        restart_if_live and we're connected + playing through Python Audio
        Out, restart the stream so the change applies immediately."""
        so = getattr(self, "_speaker_out", None)
        if so is None:
            return
        idx = self._selected_audio_device_index()
        try:
            so.set_device_index(idx)
        except Exception as e:
            self.log(f"set audio device failed: {e}")
            return
        if not restart_if_live:
            return
        if self._connected and bool(self._read_par("Speakerout", True)):
            try:
                so.stop()
                ok = so.start()
                self.log(f"audio device → index={idx}: "
                         f"{'restarted' if ok else 'restart FAILED'}")
                if not ok:
                    self._set_status(
                        "Audio device switch failed — see textport. Try "
                        "another device or Refresh Audio Devices.")
            except Exception as e:
                self.log(f"audio device live-switch raised: {e}")

    def _refresh_audio_devices(self) -> None:
        """Enumerate output devices and repopulate the Audiodevice menu,
        preserving the current selection if it still exists. Bound to the
        Refresh Audio Devices pulse."""
        par = self._par_by_name("Audiodevice")
        if par is None:
            self.log("Refresh Audio Devices: Audiodevice par not found")
            return
        dylib = getattr(self._speaker_out, "_dylib_path", None)
        try:
            devices = audio_mod.SpeakerOut.list_output_devices(
                dylib_path=dylib, log=self.log)
        except Exception as e:
            self.log(f"Refresh Audio Devices failed: {e}")
            return
        names, labels = audio_mod.format_output_device_menu(devices)
        try:
            prev = str(par.eval())
        except Exception:
            prev = audio_mod.DEFAULT_DEVICE_TOKEN
        try:
            par.menuNames = names
            par.menuLabels = labels
            par.val = prev if prev in names else audio_mod.DEFAULT_DEVICE_TOKEN
        except Exception as e:
            self.log(f"Refresh Audio Devices: menu update failed: {e}")
            return
        summary = ", ".join(
            f"[{d['index']}] {d['name']} ({d['host_api']})"
            + ("*" if d.get("is_default") else "")
            for d in devices) or "(none found)"
        self.log(f"Audio output devices ({len(devices)}): {summary}")
        self._set_status(
            f"Found {len(devices)} audio output device(s) — pick one, then "
            f"Connect (switches live if already playing).")

    def _randomize_seed(self) -> None:
        """Set the Seed par to a random integer (the web client's dice
        button). Setting par.val fires OnParChange("Seed"), which routes it
        into the continuous params stream like any manual edit."""
        import random
        par = self._par_by_name("Seed")
        if par is None:
            self.log("Randomize Seed: Seed par not found")
            return
        val = random.randint(0, 2147483647)
        try:
            par.val = val
            self.log(f"seed → {val}")
        except Exception as e:
            self.log(f"randomize seed failed: {e}")

    def _handle_pulse(self, name: str) -> None:
        dispatch = {
            "Connect": lambda: self.Connect(),
            "Disconnect": lambda: self.Disconnect(),
            # Hosted-mode auth pulses. Sign-in is paste-only — we deep-link
            # the user to app.daydream.live/dashboard/api-keys and prompt
            # for the resulting key. The browser-OAuth flow was removed in
            # v0.2.5 (fewer moving parts; the dashboard URL is a one-click
            # copy anyway).
            "Pasteapikey": lambda: self.PromptForApiKey(),
            "Stillplaying": lambda: self._request_extend(auto=False),
            "Sendprompt": lambda: self.SendPrompt(),
            "Setpromptblend": lambda: self.SetPromptBlend(),
            "Swapsource": lambda: self.SwapSource(),
            "Settimbresource": lambda: self.SetTimbreSource(),
            "Cleartimbresource": lambda: self.ClearTimbreSource(),
            "Settimbrefixture": lambda: self.SetTimbreFixture(),
            "Setstructuresource": lambda: self.SetStructureSource(),
            "Clearstructuresource": lambda: self.ClearStructureSource(),
            "Setstructurefixture": lambda: self.SetStructureFixture(),
            "Refreshaudiodevices": lambda: self._refresh_audio_devices(),
            "Randomizeseed": lambda: self._randomize_seed(),
        }
        fn = dispatch.get(name)
        if fn:
            try:
                fn()
            except Exception as e:
                self.log(f"Pulse {name} failed: {e}")

    # NOTE: the old synchronous `_extend_session` (main-thread HTTP) was
    # replaced by `_request_extend` + the heartbeat worker; results land
    # in `_apply_extend_result` via the `hb-extend` event.

    # -------- helpers --------------------------------------------------------

    # Number of consecutive WS send failures after which we declare the
    # connection dead and tear down. A single failure can be a transient
    # blip; a run of them means the socket is gone (e.g. the SSL stream
    # got corrupted by a timed-out binary write → every subsequent send
    # raises SSL: BAD_LENGTH). Without this, a per-frame sender retries
    # forever, flooding the textport with thousands of errors + pegging
    # the CPU, and never triggers failover.
    _SEND_FAIL_LIMIT = 3

    def _note_send_result(self, ok: bool) -> None:
        """Track consecutive send failures and tear down once the
        connection is provably dead. Called by _send_text / _send_bytes."""
        if ok:
            self._send_fail_streak = 0
            return
        self._send_fail_streak += 1
        if self._send_fail_streak >= self._SEND_FAIL_LIMIT and self._connected:
            self._teardown_dead_connection(
                f"{self._send_fail_streak} consecutive send failures")

    def _teardown_dead_connection(self, reason: str) -> None:
        """Declare the connection dead and tear down ONCE (main thread).
        Shared by _note_send_result (discrete sends) and the pacer
        watchdog in _drain_inbound (continuous params stream)."""
        if not self._connected:
            return
        self.log(
            f"_send: {reason} — connection is dead; tearing down "
            f"(stops the retry flood)"
        )
        # Flip _connected first so any in-flight senders (pacer thread,
        # OnParChange) short-circuit immediately, then do a clean close.
        # Guarded by _connected inside Disconnect so this only runs once.
        self._connected = False
        try:
            self.Disconnect()
        except Exception as e:
            self.log(f"_teardown_dead_connection: Disconnect raised: {e}")
        self._set_status(
            "Connection lost (send failed) — re-try Connect.")

    def _send_text(self, payload: str) -> None:
        """Send a text frame via the Python WS client."""
        wsc = self._wsc
        if wsc is None:
            return
        ok = wsc.send_text(payload)
        # Only log on failure. The sampled-every-600 success log was just
        # confirmation during debugging and adds nothing operationally.
        if not ok:
            self.log(f"_send_text: {len(payload)} chars FAILED")
        self._note_send_result(ok)

    def _send_bytes(self, payload: bytes) -> None:
        """Send a binary frame via the Python WS client."""
        wsc = self._wsc
        if wsc is None:
            return
        ok = wsc.send_binary(payload)
        if not ok:
            self.log(f"_send_bytes: {len(payload)} B FAILED")
        elif self._debug_enabled:
            self.log(f"_send_bytes: {len(payload)} B ok")
        self._note_send_result(ok)

    def _snapshot_audio(self, chop) -> np.ndarray | None:
        """Grab the current samples from a CHOP (param-reference, op-path, or
        the resampled COMP input). Returns (channels, samples) float32 or None.
        """
        try:
            if chop is None:
                chop = self.ownerComp.op("resample_in")
            if isinstance(chop, str):
                chop = self.ownerComp.op(chop) or op(chop)  # noqa: F821
            if chop is None:
                return None
            # CHOP samples
            ch_count = chop.numChans
            if ch_count <= 0:
                return None
            samples = chop.numSamples
            pcm = np.empty((ch_count, samples), dtype=np.float32)
            for i in range(ch_count):
                pcm[i] = np.fromiter(chop[i].vals, dtype=np.float32, count=samples)
            return audio_mod.to_stereo(pcm)
        except Exception as e:
            self.log(f"_snapshot_audio failed: {e}")
            return None

    def _resolve_wire_name(self, name: str) -> str | None:
        if name in P.PARAM_BY_WIRE:
            return name
        p = P.PARAM_BY_NAME.get(name)
        return p.wire_name if p else None

    def _coerce_par_value(self, par, schema: P.Param) -> Any:
        if schema.type == "Toggle":
            return bool(par.eval())
        if schema.type == "Int":
            return int(par.eval())
        if schema.type == "Menu":
            return str(par.eval())
        if schema.type == "Str":
            v = par.eval()
            # Curves want JSON-parsed values, not strings.
            if schema.wire_name and schema.wire_name.endswith("_curve") and v:
                try:
                    return json.loads(v)
                except json.JSONDecodeError:
                    self.log(f"Bad JSON in {schema.name}; sending as string")
                    return v
            return v
        return float(par.eval())

    def _collect_init_params(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for p in P.PARAMS:
            if p.category == "init":
                out[p.name] = self._read_par(p.name, p.default)
        return out

    def _sample_curves(self) -> None:
        """Sample every enabled scheduled curve at the loop playhead
        and write the result into `_dirty`.

        Cheap fast path: if the master `Schedulecurves` toggle is off
        OR every per-curve enable is off, return without touching the
        cache or the lock. Most ticks should hit this fast path.

        For each enabled curve, the JSON spec is parsed lazily and
        cached in `self._curve_cache` keyed by the SPEC STRING — so
        editing the JSON invalidates the cache automatically (next
        tick sees a different key, re-parses). Bad JSON is cached as
        None so we don't spam errors every tick.

        Sample position: `t = (ring.position / ring.frames) % 1.0`,
        the same playhead we already report to the server in
        `playback_pos`. Pre-`ready`, ring.frames == 0; we skip those
        ticks because there's no buffer to wrap around yet.

        Manual override: if the user moved a curve-bound base param
        within the last `CURVE_OVERRIDE_SECONDS`, the curve yields for
        that param — we don't overwrite their adjustment. Reset when
        the override window elapses.
        """
        if not bool(self._read_par("Schedulecurves", False)):
            return
        # Need a live loop buffer for the playhead position.
        frames = self._ring.frames
        if frames <= 0:
            return
        pos = self._ring.position
        t = (pos % frames) / frames if frames > 0 else 0.0
        now = time.monotonic()

        # Iterate the static binding map (4 curves; cheap).
        for curve_par_name, (base_par_name, enable_par_name) in (
                P.CURVE_PARAM_BINDINGS.items()):
            if not bool(self._read_par(enable_par_name, False)):
                continue
            base_schema = P.PARAM_BY_NAME.get(base_par_name)
            if base_schema is None or not base_schema.wire_name:
                continue
            wire_name = base_schema.wire_name

            # Manual override gate.
            override_until = self._manual_override_until.get(wire_name, 0.0)
            if now < override_until:
                continue

            spec = self._read_par(curve_par_name, "") or ""
            # Cache hit by spec STRING — if the user edits the JSON,
            # the key changes and we re-parse next tick.
            cached_key = f"{curve_par_name}::{spec}"
            if cached_key not in self._curve_cache:
                self._curve_cache[cached_key] = parse_curve_spec(spec)
                # Cap cache growth (e.g., user scrubs the JSON a lot).
                if len(self._curve_cache) > 64:
                    # Drop the oldest entry (dicts preserve insert order).
                    self._curve_cache.pop(next(iter(self._curve_cache)))
            pts = self._curve_cache[cached_key]
            if pts is None:
                continue

            # Sample → map [0,1] to base param's [min, max].
            y_norm = eval_curve_linear(pts, t)
            lo = float(base_schema.min if base_schema.min is not None else 0.0)
            hi = float(base_schema.max if base_schema.max is not None else 1.0)
            value = lo + y_norm * (hi - lo)

            # Write into _dirty for the next params flush AND into
            # the underlying TD par so the user sees the slider move.
            # The TD par write fires OnParChange, which would normally
            # trigger the manual-override window; we set
            # `_last_curve_write[wire_name] = value` first so
            # OnParChange recognizes the echo and skips the override.
            self._last_curve_write[wire_name] = value
            with self._lock:
                self._dirty[wire_name] = value
            try:
                par = self._par_by_name(base_par_name)
                if par is not None:
                    par.val = value
            except Exception:
                # par write failure isn't fatal — the wire value is
                # already in _dirty.
                pass

    def _seed_dirty_from_current_pars(self) -> None:
        """Populate self._dirty with EVERY continuous-param's current value.

        Called once on `ready`. Without this, the server uses its internal
        defaults (notably denoise=0 = passthrough) until the user moves a
        slider — which is why "generated audio didn't kick in until I
        touched denoise". After this call, the next OnTick sends a full
        params message containing the user's current UI values, and the
        server starts generating immediately.

        Three continuous wire keys are special-cased OUT of the
        _dirty path:
          * prompt_blend     -> sent via encode_set_prompt_blend
          * timbre_strength  -> sent via encode_set_timbre_strength
          * lora_blend       -> UI-only (no engine equivalent)
        Putting these into the `params` raw dict gets the WS closed by
        the server, which was the empirical cause of disconnects users
        hit when fiddling with prompts / LoRAs.
        """
        seeded = 0
        n_blends = 0
        with self._lock:
            for p in P.PARAMS:
                if p.category != "continuous" or not p.wire_name:
                    continue
                par = self._par_by_name(p.name)
                if par is None:
                    continue
                try:
                    value = self._coerce_par_value(par, p)
                except Exception:
                    continue
                wn = p.wire_name
                if wn in self._PARAMS_NOT_FOR_WIRE:
                    if wn in ("prompt_blend", "timbre_strength"):
                        # Blend targets for the pacer thread. Connect()
                        # recreated the GlideEngine, so these are
                        # first-seen → snap → sent verbatim next tick.
                        self._blend_targets[wn] = float(value)
                        n_blends += 1
                    # lora_blend: UI-only, no engine route, skip.
                    continue
                self._dirty[wn] = value
                seeded += 1
        # Force the throttlers to re-send even if the value matches what
        # a previous session last sent — the re-assert after (re)connect
        # must not be epsilon-suppressed.
        for sender in self._blend_senders.values():
            sender.reset()
        self.log(
            f"seeded {seeded} continuous params into _dirty for first tick"
            f" (+{n_blends} blend targets)")

    def _push_interp_methods(self) -> None:
        """Send the current per-path interpolation method for all four
        blend paths. Called on `ready` so the server matches the menus
        even after a reconnect (mirrors demon-public-demo's useInterpSync
        sendAll). No-op if not connected."""
        if not self._connected:
            return
        for par_name, path in _INTERP_PAR_TO_PATH.items():
            par = self._par_by_name(par_name)
            if par is None:
                continue
            try:
                method = str(par.eval())
            except Exception:
                method = "slerp"
            try:
                self._send_text(wire.encode_set_interp_method(path, method))
            except Exception as e:
                self.log(f"_push_interp_methods({path}) failed: {e}")

    # -------- TD plumbing ----------------------------------------------------

    def _ws(self):
        try:
            return self.ownerComp.op("ws1")
        except Exception:
            return None

    # Pars that only make sense in one Mode. Used by _apply_mode_visibility
    # to grey out the unused set whenever the Mode menu changes.
    _DIRECT_ONLY_PARS = ("Serverurl",)
    _HOSTED_ONLY_PARS = (
        "Baseurl", "Apikey",
        "Pasteapikey",
        "Queueposition", "Expiresin", "Denyreason",
        "Stillplaying",
    )

    def _apply_mode_visibility(self, mode: Any) -> None:
        """Grey out the inactive-mode pars on the Session page.

        TD's custom pars don't support `display=False` based on another par;
        the next best thing is toggling `enable`. Greying preserves the
        ability for the user to *see* both layouts (so they know there's
        another mode to switch to) while making it obvious which pars are
        currently live.
        """
        mode_norm = (str(mode) or "direct").lower()
        is_hosted = (mode_norm == "hosted")
        for name in self._DIRECT_ONLY_PARS:
            p = self._par_by_name(name)
            if p is not None:
                try:
                    p.enable = not is_hosted
                except Exception:
                    pass
        for name in self._HOSTED_ONLY_PARS:
            p = self._par_by_name(name)
            if p is not None:
                try:
                    p.enable = is_hosted
                except Exception:
                    pass

    def _par_by_name(self, name: str):
        try:
            return getattr(self.ownerComp.par, name)
        except AttributeError:
            return None

    def _page_by_name(self, page_name: str):
        for page in self.ownerComp.customPages:
            if page.name == page_name:
                return page
        return None

    def _read_par(self, name: str, default: Any = None) -> Any:
        par = self._par_by_name(name)
        if par is None:
            return default
        try:
            return par.eval()
        except Exception:
            return default

    def _write_par(self, name: str, value: Any) -> None:
        par = self._par_by_name(name)
        if par is None:
            return
        try:
            par.val = value
        except Exception:
            pass

    def _set_status(self, msg: str) -> None:
        self._write_par("Status", msg)
        self.log(f"status: {msg}")

    @staticmethod
    def _friendly_close_reason(reason: Any) -> str:
        """Boil a websocket-client close-reason down to one line of UI.

        websocket-client's handshake failures stringify the entire HTTP
        response including headers + body, so a Cloudflare 502 turns
        into ~30 lines of JSON-y goo in the Status par. Pattern-match
        the common gateway failures into human text; fall through to a
        truncated raw for anything we don't recognize.
        """
        s = str(reason or "closed")
        low = s.lower()
        if "502 bad gateway" in low or "error code: 502" in low:
            return ("502 from hosted edge — the pod isn't responding. "
                    "Try Connect again in a few seconds.")
        if "503 service unavailable" in low or "error code: 503" in low:
            return "503 from hosted edge — service unavailable. Try again."
        if "504 gateway timeout" in low or "error code: 504" in low:
            return "504 from hosted edge — gateway timeout. Try again."
        if "handshake status 401" in low or "handshake status 403" in low:
            return "Authentication rejected — re-paste your API key."
        if "handshake status 429" in low:
            return "Rate-limited by the queue. Wait a moment and retry."
        if "name or service not known" in low or "nodename nor servname" in low:
            return "DNS lookup failed — check your Base URL."
        if "connection refused" in low:
            return "Connection refused — check Server URL is reachable."
        if "timed out" in low or "timeout" in low:
            return "Connection timed out."
        if "connection to remote host was lost" in low:
            return "Connection lost — re-try Connect."
        # Fall through. Trim aggressively so the Status par doesn't go
        # multi-line; the full reason is still in textport via the
        # [ws_client] closed line that fires before this.
        if len(s) > 120:
            s = s[:117] + "..."
        return s

    # -------- logging --------------------------------------------------------

    def log(self, msg: str) -> None:
        try:
            print(f"[demon] {msg}")
        except Exception:
            pass

    # -------- script CHOP cook hooks ----------------------------------------
    # These are called from the script_send / audio_out Script CHOPs.

    def OnCookSend(self, scriptOp) -> None:
        """script_send Script CHOP cook — no-op.

        This release does NOT stream live audio into DEMON. The source track
        is loaded once from the Source Audio File par at Connect time. This
        Script CHOP exists only because the .tox topology still includes it;
        we output a single silent sample so it cooks without errors.
        """
        scriptOp.clear()
        scriptOp.numSamples = 1
        scriptOp.appendChan("dummy")

    def OnCookRecv(self, scriptOp) -> None:
        """audio_out Script CHOP cook callback.

        IMPORTANT: this is NOT the audio playback path — SpeakerOut owns
        the actual play head via LoopBuffer.read(). This callback only
        exists to populate the Script CHOP for visual reactivity (waveform
        viewers, FFTs, anything users wire downstream of out_chop). It
        uses `peek()`, which does NOT advance the play head, so it can't
        race the audio thread.
        """
        self._n_cook_recv = getattr(self, "_n_cook_recv", 0) + 1
        if self._n_cook_recv == 1:
            try:
                self.log(f"OnCookRecv: FIRST cook — numSamples="
                         f"{scriptOp.numSamples} loop_frames={self._ring.frames}")
            except Exception:
                pass
        # Cook-rate diagnostic (Debug-gated, throttled). THE signal for
        # whether Audio Analyze will work: numSamples >= 64 → audio-rate
        # (the waveCHOP carrier is doing its job, Analyze populates);
        # numSamples < 64 → frame-rate (carrier not propagating, Analyze
        # sees nothing). Logged every ~600 cooks so it's visible without
        # spamming.
        if self._debug_enabled and self._n_cook_recv % 600 == 0:
            try:
                _ns = int(scriptOp.numSamples)
            except Exception:
                _ns = -1
            try:
                _nin = int(scriptOp.inputs[0].numSamples) if scriptOp.inputs else -1
            except Exception:
                _nin = -1
            _best = max(_ns, _nin)
            _rate = "AUDIO-rate ✓" if _best >= 64 else "FRAME-rate ✗"
            try:
                self.log(
                    f"OnCookRecv: cook #{self._n_cook_recv} "
                    f"out.numSamples={_ns} carrier.numSamples={_nin} → {_rate}"
                )
            except Exception:
                pass

        # Determine the audio-rate block size for this cook.
        #
        # Preference order:
        #   1. The audio_clock WAVE CHOP carrier wired as input 0. It's
        #      Time Slice + 48 kHz, so its numSamples IS the audio-rate
        #      block for this frame's time slice (~800 at 48k/60fps).
        #      This is the deterministic signal — read it directly.
        #   2. scriptOp.numSamples, if TD pre-set an audio-rate count.
        #   3. Fall back to one frame's worth (frame-rate) — useless for
        #      Audio Analyze, but never zero.
        n = 0
        try:
            if scriptOp.inputs:
                n_in = int(scriptOp.inputs[0].numSamples)
                if n_in >= 64:
                    n = n_in
        except Exception:
            n = 0
        if n < 64:
            try:
                n_td = int(scriptOp.numSamples)
            except Exception:
                n_td = 0
            if n_td >= 64:
                n = n_td
        if n < 64:
            try:
                fps = project.cookRate  # type: ignore[name-defined]  # noqa: F821
                if fps <= 0:
                    fps = 60.0
            except Exception:
                fps = 60.0
            n = max(1, int(wire.SAMPLE_RATE / fps))

        # peek() reads at the current play head WITHOUT advancing it.
        # SpeakerOut's audio thread advances the head; this is just a
        # snapshot for visual consumers.
        pcm = self._ring.peek(n)
        self._playback_pos = self._ring.position

        scriptOp.clear()
        try:
            scriptOp.rate = wire.SAMPLE_RATE
        except Exception:
            pass
        try:
            arr = np.ascontiguousarray(pcm, dtype=np.float32)
            scriptOp.copyNumpyArray(arr)
        except AttributeError:
            # Fallback for TD builds that lack copyNumpyArray.
            scriptOp.numSamples = n
            try:
                scriptOp.appendChan("chan1").vals = pcm[0].tolist()
                scriptOp.appendChan("chan2").vals = pcm[1].tolist()
            except Exception as e:
                self.log(f"OnCookRecv write failed (fallback): {e}")
        except Exception as e:
            self.log(f"OnCookRecv copyNumpyArray failed: {e}")

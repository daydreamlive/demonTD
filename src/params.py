"""
Declarative parameter schema for the DEMON TouchDesigner operator.

This is the SOURCE OF TRUTH. The build script (build/build_tox.py)
generates the COMP's custom parameter pages from this list, and the
extension (demon_ext.py) routes parameter changes by looking up entries
here.

Adding a new param = one entry in PARAMS.
Adding a new discrete message = one entry in DISCRETE_MESSAGES.

Param categories
----------------
- "init": session-start params (immutable while connected)
- "continuous": fanned-out at the 8ms tick as `{type:"params", raw:{...}}`
- "session": local-only (connection state, auth, status)
- "discrete": triggers a one-shot WS message (pulse / toggle)

TD parameter types
------------------
- "Float", "Int", "Toggle", "Str", "Menu", "Pulse", "Header"
- Menu pars carry `menu_names` and `menu_labels` (lists)
- Pulse pars carry `is_pulse=True`
- Header pars are layout-only, no value
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Param:
    name: str                       # TD par name (PascalCase preferred for visibility)
    wire_name: str | None           # The key sent on the wire (None for local/UI-only)
    page: str                       # TD parameter page label
    type: str                       # "Float" | "Int" | "Toggle" | "Str" | "Menu" | "Pulse" | "Header" | "File"
    category: str                   # "init" | "continuous" | "session" | "discrete"
    default: Any = None
    min: float | None = None
    max: float | None = None
    clamp_min: bool = False         # whether to hard-clamp at min
    clamp_max: bool = False         # whether to hard-clamp at max
    label: str | None = None        # display label (defaults to name)
    help: str = ""
    menu_names: tuple[str, ...] = ()
    menu_labels: tuple[str, ...] = ()
    multiline: bool = False
    secret: bool = False            # hide value in .tox serialization (for API keys)
    readonly: bool = False
    enable: bool = True             # False = par appears greyed-out / non-editable
    section_header: bool = False    # this is a Header par
    ui_hidden: bool = False         # True = kept in schema (so _read_par + lookups
                                    # still resolve) but NOT rendered as a visible
                                    # custom par. TD can't programmatically hide a
                                    # custom par (Par.hidden is read-only), so the
                                    # build just skips creating it.
    order: int = 0


# -----------------------------------------------------------------------------
# Page 1: Session (connection, auth, status)
# -----------------------------------------------------------------------------
SESSION_PARAMS: list[Param] = [
    Param("Connect", None, "Session", "Pulse", "session", order=10,
          help="Open a session against the configured server."),
    Param("Disconnect", None, "Session", "Pulse", "session", order=20,
          help="Close the active WebSocket session."),
    Param("Serverurl", None, "Session", "Str", "session",
          default="ws://localhost:8765/", order=30, label="Server URL",
          help="DEMON pod WebSocket URL. ws://localhost:8765/ is the default "
               "port used by DEMON's demos.realtime_motion_graph_web; for a "
               "Vast.ai-hosted pod use the Direct WS line printed by "
               "scripts/vast/launch.sh (e.g. ws://1.2.3.4:44105/)."),
    # Sourcefile is NOT shown in the op UI (ui_hidden) — it confused users
    # who expected the file picker to be the audio input, when the actual
    # source comes from the CHOP wired into the COMP. Kept in the schema so
    # _read_par("Sourcefile") and _has_source_audio still resolve safely
    # (returns its default ""); removing it outright regressed Connect.
    Param("Sourcefile", None, "Session", "File", "session",
          default="", order=40, label="Source Audio File", ui_hidden=True,
          help="(hidden) Legacy source-file path. The real source is the "
               "CHOP wired into the COMP's input; this is retained only for "
               "backward compatibility and is not shown in the UI."),
    Param("Status", None, "Session", "Str", "session", default="Idle",
          order=50, readonly=True,
          help="Current connection status."),
    Param("Speakerout", None, "Session", "Toggle", "session", default=True,
          order=55, label="Python Audio Out",
          help="Play generated audio directly via Python sounddevice / "
               "PortAudio to the system default output. Required because "
               "TD's CHOP audio chain doesn't pull a Script CHOP at audio "
               "rate across a Base COMP boundary. Toggle off if you want "
               "to route the audio only via the COMP's out_chop port to "
               "your own external Audio Device Out CHOP."),
    # Output device picker. Defaults to "system default", which is whatever
    # device PortAudio (and TD) was already using — the cause of the
    # "connected but no audio" reports when that default isn't the device
    # the user is actually listening on. Pulse "Refresh Audio Devices" to
    # populate the menu, pick a device, and (re-)Connect to apply.
    Param("Audiodevice", None, "Session", "Menu", "session", default="-1",
          order=56, label="Audio Output Device",
          menu_names=("-1",), menu_labels=("Default (system)",),
          help="Which output device 'Python Audio Out' plays through. "
               "'Default (system)' uses the OS default. Pulse Refresh Audio "
               "Devices to list devices; changing this while connected "
               "restarts playback on the new device."),
    Param("Refreshaudiodevices", None, "Session", "Pulse", "session",
          order=57, label="Refresh Audio Devices",
          help="Enumerate the system's audio output devices and populate "
               "the Audio Output Device menu. Run this if your device isn't "
               "listed or you just plugged one in."),
    # ---------- Hosted mode (v0.2+) ----------
    # Mode toggles between Direct (a pod URL the user supplies) and
    # Hosted (the Daydream queue at music.daydream.live). Connect()
    # branches on this. OnParChange("Mode") greys out the unused-mode
    # params for visual clarity.
    Param("Mode", None, "Session", "Menu", "session", default="direct",
          order=60, label="Mode",
          menu_names=("direct", "hosted"),
          menu_labels=("Direct (your pod)", "Hosted (Daydream queue)"),
          help="Direct: connect to your own DEMON pod via Server URL. "
               "Hosted: join the Daydream queue and play on a managed pod."),
    Param("Baseurl", None, "Session", "Str", "session",
          default="https://music.daydream.live", order=62, label="Hosted Base URL",
          help="Daydream queue API root. Override only if you're testing "
               "against a staging deployment."),
    Param("Apikey", None, "Session", "Str", "session", default="",
          order=64, label="API Key", secret=True,
          help="Daydream API key. Sent as Authorization: Bearer <key>. "
               "Set via the Paste API Key pulse; stored in "
               "<prefs>/daydream_auth.json, not in the .toe."),
    Param("Pasteapikey", None, "Session", "Pulse", "session", order=68,
          label="Paste API Key",
          help="Opens app.daydream.live/dashboard/api-keys in your browser "
               "and prompts you to paste an API key into TD. Validates the "
               "key against /users/profile before saving."),
    Param("Queueposition", None, "Session", "Int", "session", default=0,
          order=74, label="Queue Position", readonly=True,
          help="1-based queue position while waiting. 0 once active."),
    Param("Expiresin", None, "Session", "Float", "session", default=0.0,
          order=76, label="Expires in (s)", readonly=True,
          help="Seconds until the hosted session expires. Hit Still Playing "
               "to extend."),
    Param("Denyreason", None, "Session", "Str", "session", default="",
          order=78, label="Deny reason", readonly=True,
          help="Populated when the server returns over_budget / paywall."),
    Param("Stillplaying", None, "Session", "Pulse", "session", order=80,
          label="Still playing?",
          help="POST /api/queue/extend — bumps the session by one duration."),
    Param("Autoextend", None, "Session", "Toggle", "session", default=True,
          order=82, label="Auto-extend",
          help="When ON, the 5 s heartbeat auto-pulses Still Playing once "
               "Expires in (s) drops below 60 s, so the hosted session "
               "stays alive without user input. Server-side MAX_EXTENSIONS "
               "still caps total extends — at the cap, the session ends "
               "naturally. Toggle OFF for unattended performances where "
               "you want a hard time limit, or to match the web client's "
               "explicit 'Still playing?' UX exactly."),

    Param("Debug", None, "Session", "Toggle", "session", default=False,
          order=999, label="Debug Logging",
          help="When on, the extension prints verbose textport diagnostics "
               "(per-tick state, sample-decode hex dumps, slice/initial "
               "buffer WAV files to /tmp/demon-debug/, ws frame echoes). "
               "Off by default — keep off unless investigating a bug."),
]


# -----------------------------------------------------------------------------
# Page 2: Init (session-start params; immutable while connected)
# -----------------------------------------------------------------------------
INIT_PARAMS: list[Param] = [
    Param("Sde", "sde", "Init", "Toggle", "init", default=False, order=10,
          help="Use Score Distillation Energy mode instead of ODE."),
    Param("Lora", "lora", "Init", "Toggle", "init", default=True, order=20,
          help="Enable LoRA adapter support."),
    Param("Depth", "depth", "Init", "Int", "init", default=4,
          min=1, max=8, clamp_min=True, clamp_max=True, order=30,
          help="DiT pipeline depth (latency/quality tradeoff)."),
    Param("Vaewindow", "vae_window", "Init", "Float", "init", default=0.36,
          min=0.1, max=10.0, clamp_min=True, clamp_max=True, order=40,
          label="VAE Window", help="VAE decoder rolling-regen window in "
                                   "seconds. 0.36 matches demon-public-demo "
                                   "(and the post-2026-06 backend). Larger "
                                   "windows make param changes audibly SLOW "
                                   "to apply — the old 6.0 default was the "
                                   "'params take forever' bug."),
    Param("Crop", "crop", "Init", "Float", "init", default=0.0,
          min=0.0, max=120.0, clamp_min=True, order=50,
          help="Crop input audio to N seconds (0 = no crop)."),
    Param("Steps", "steps", "Init", "Int", "init", default=8,
          min=1, max=32, clamp_min=True, clamp_max=True, order=60,
          help="Generation steps per latent frame."),
    Param("Fastvae", "fast_vae", "Init", "Toggle", "init", default=False, order=70,
          label="Fast VAE", help="Use dreamvae distilled decoder (TensorRT only). "
                                  "Off matches demon-public-demo default."),
    Param("Walkwindow", "walk_window", "Init", "Toggle", "init", default=False, order=80,
          label="Walk Window", help="For long sources, use 60s engine at boundaries."),
    Param("Walkwindows", "walk_window_s", "Init", "Float", "init", default=60.0,
          min=1.0, max=240.0, clamp_min=True, order=90,
          label="Walk Window (s)", help="Walk window duration in seconds."),
    Param("Initprompt", "prompt", "Init", "Str", "init",
          default="heavy dubstep, deathstep, afxdump, growl heavy bass distortion",
          order=100, multiline=True, label="Initial Prompt",
          help="Text prompt at session start (changeable later via Send Prompt). "
               "Default matches demon-public-demo."),
    # NOTE: There is no `Initpromptb` companion to `Initprompt`. The
    # canonical has one source of truth for the secondary prompt — the
    # live `Promptb` on the Prompt+LoRA page. `_build_session_config`
    # reads it from there for the `prompt_b` field in SessionConfig.
    Param("Fixturename", "fixture_name", "Init", "Str", "init", default="",
          order=110, label="Fixture Name",
          help="Known fixture name for sidecar lookup (BPM/key/latents). Optional."),
    # Playback-lead tuning (server-side decode buffer). The "lead" is how
    # far ahead of the live playhead each freshly decoded slice is placed;
    # the server adapts it to observed per-tick compute. Defaults match
    # demon-public-demo's config.json. Higher floor = more robust to GPU
    # contention (screen capture, the WebGPU/visual display, a second
    # process) at the cost of latency. Sent in SessionConfig at Connect.
    Param("Leadfloor", "lead_floor_s", "Init", "Float", "init", default=0.25,
          min=0.0, max=2.0, clamp_min=True, order=120, label="Lead Floor (s)",
          help="Minimum baseline lead. ~0.05 is snappy on an idle GPU but "
               "sawtooths under contention; 0.5 reproduces old fixed "
               "behavior; 0.25 (default) is the midpoint."),
    Param("Leadceiling", "lead_ceiling_s", "Init", "Float", "init",
          default=1.35, min=0.1, max=5.0, clamp_min=True, order=122,
          label="Lead Ceiling (s)",
          help="Caps how far contention can inflate the lead. Keep >= ~1.1 "
               "to fully cover rebuild/refit stalls."),
    Param("Leadreleasetau", "lead_release_tau_s", "Init", "Float", "init",
          default=1.5, min=0.0, max=10.0, clamp_min=True, order=124,
          label="Lead Release Tau (s)",
          help="Decay time-constant for a contention spike — lower releases "
               "faster. The server clamps it up to Lead Ceiling if set lower."),
]


# -----------------------------------------------------------------------------
# Page 3: Prompt + LoRA
# -----------------------------------------------------------------------------

# Generate the 70-keyscale menu: {A..G} × {natural, #, b} × {major, minor}
_NOTES = ["A", "B", "C", "D", "E", "F", "G"]
_ACCIDENTALS = [("", ""), ("#", "♯"), ("b", "♭")]
_QUALITIES = ["major", "minor"]


def _build_keyscale_menu() -> tuple[tuple[str, ...], tuple[str, ...]]:
    names: list[str] = ["auto"]
    labels: list[str] = ["Auto (detect)"]
    for note in _NOTES:
        for acc_wire, acc_label in _ACCIDENTALS:
            for qual in _QUALITIES:
                names.append(f"{note}{acc_wire} {qual}")
                labels.append(f"{note}{acc_label} {qual}")
    return tuple(names), tuple(labels)


_KEYSCALE_NAMES, _KEYSCALE_LABELS = _build_keyscale_menu()


PROMPT_LORA_PARAMS: list[Param] = [
    Param("Sendprompt", None, "Prompt+LoRA", "Pulse", "discrete", order=10,
          label="Send Prompt",
          help="Send the current Prompt / Key / Time Signature to the server."),
    Param("Prompt", None, "Prompt+LoRA", "Str", "session", default="",
          order=20, multiline=True, label="Prompt (Tags A)",
          help="Tags or freeform text to apply on Send Prompt. With "
               "Prompt B set, Prompt Blend lerps between this (A, 0) "
               "and Prompt B (1)."),
    Param("Promptb", None, "Prompt+LoRA", "Str", "session", default="",
          order=22, multiline=True, label="Prompt B (Tags B)",
          help="Optional secondary prompt for blending. When set, "
               "Prompt Blend lerps between Prompt (A, 0) and this (B, 1). "
               "Empty = no B side, equivalent to always-A. "
               "Editable mid-session: changes apply on next Send Prompt."),
    Param("Key", None, "Prompt+LoRA", "Menu", "session", default="auto", order=30,
          menu_names=_KEYSCALE_NAMES, menu_labels=_KEYSCALE_LABELS,
          help="Musical key. 'Auto' lets the server detect."),
    Param("Timesignature", None, "Prompt+LoRA", "Menu", "session",
          default="auto", order=40, label="Time Signature",
          menu_names=("auto", "2", "3", "4", "6"),
          menu_labels=("Auto", "2", "3", "4", "6"),
          help="Time signature numerator."),
    Param("Setpromptblend", None, "Prompt+LoRA", "Pulse", "discrete", order=50,
          label="Apply Prompt Blend",
          help="Send the current Prompt Blend value to the server."),
    Param("Promptblend", "prompt_blend", "Prompt+LoRA", "Float", "continuous",
          default=0.4, min=0.0, max=1.0, clamp_min=True, clamp_max=True,
          order=60, label="Prompt Blend",
          help="Prompt A vs B blend (0=A, 1=B). Streamed continuously."),
    # Per-path blend interpolation method (discrete `set_interp_method`).
    # Controls how each blend sweep interpolates: "slerp" keeps the norm
    # constant across the sweep (server default); "linear" is a straight
    # average that dips at the midpoint. Applied immediately on change and
    # re-pushed on every (re)connect. Mirrors demon-public-demo's
    # useInterpSync. The four paths match the web client exactly.
    Param("Interpheader", None, "Prompt+LoRA", "Header", "session",
          order=62, section_header=True, label="Blend Interpolation"),
    Param("Interpprompt", None, "Prompt+LoRA", "Menu", "discrete",
          default="slerp", order=63, label="Prompt Interp",
          menu_names=("slerp", "linear"),
          menu_labels=("Slerp (norm-preserving)", "Linear"),
          help="Interpolation method for the prompt A/B blend."),
    Param("Interptimbre", None, "Prompt+LoRA", "Menu", "discrete",
          default="slerp", order=64, label="Timbre Interp",
          menu_names=("slerp", "linear"),
          menu_labels=("Slerp (norm-preserving)", "Linear"),
          help="Interpolation method for the timbre blend."),
    Param("Interpstructure", None, "Prompt+LoRA", "Menu", "discrete",
          default="slerp", order=65, label="Structure Interp",
          menu_names=("slerp", "linear"),
          menu_labels=("Slerp (norm-preserving)", "Linear"),
          help="Interpolation method for the structure blend."),
    Param("Interpfeedback", None, "Prompt+LoRA", "Menu", "discrete",
          default="slerp", order=66, label="Feedback Interp",
          menu_names=("slerp", "linear"),
          menu_labels=("Slerp (norm-preserving)", "Linear"),
          help="Interpolation method for the feedback blend."),
    Param("Loraheader", None, "Prompt+LoRA", "Header", "session",
          order=70, section_header=True, label="LoRAs"),
    # When On (default), each enabled LoRA's `primary_trigger_word` from
    # the server's catalog is prepended to `tags` AND `tags_b` on every
    # SendPrompt. Without it the model's text encoder never sees the
    # activation token and the LoRA's style barely fires. Off = manual
    # workflow, user includes triggers in Prompt/PromptB themselves.
    # Mirrors demon-public-demo's `engine.auto_prepend_lora_triggers`.
    Param("Autoprependloratriggers", None, "Prompt+LoRA", "Toggle", "session",
          default=True, order=75, label="Auto-Prepend LoRA Triggers",
          help="Inject each enabled LoRA's trigger word into the prompt "
               "at send-time. Required for LoRAs to actually fire. "
               "Turn off only if you're managing trigger words manually."),
    Param("Lorablend", "lora_blend", "Prompt+LoRA", "Float", "continuous",
          default=0.5, min=0.0, max=1.0, clamp_min=True, clamp_max=True,
          order=80, label="LoRA Blend",
          help="UI-level A/B LoRA blend. Edge LoRA binding fans this to "
               "paired lora_str_<id> values."),
    # Dynamic per-LoRA rows are appended at runtime by DemonExt
    # once the server's lora_catalog is received.
]


# -----------------------------------------------------------------------------
# Page 4: Synthesis (the hot continuous params)
# -----------------------------------------------------------------------------
SYNTHESIS_PARAMS: list[Param] = [
    # Wire key is `denoise`, but the canonical labels this "Strength"
    # — users perceive this knob as "how strong is the remix", not as
    # a diffusion-process knob. See DISPLAY_NAMES in
    # demon-public-demo/vendor/demon-ui/components/Performance/SliderTile.tsx.
    Param("Denoise", "denoise", "Synthesis", "Float", "continuous", default=0.85,
          min=0.0, max=1.0, clamp_min=True, clamp_max=True, order=10,
          label="Strength",
          help="How strong the remix is — at 0 the model echoes the source "
               "faithfully, at 1 it's free to depart fully. The canonical "
               "name is `denoise` (diffusion-process internal), but the user "
               "perceives this as 'strength of the transformation'."),
    # Generation seed. The reference web client uses an arbitrary uint32
    # (config.json default 42, with a "dice" button to randomize) — NOT a
    # normalized 0..1 value, which is what this used to (wrongly) be. We
    # cap at int32 max to stay safely inside TD's numeric-par range; that's
    # still ~2.1 billion seeds. Streamed continuously like the web client.
    Param("Seed", "seed", "Synthesis", "Int", "continuous", default=42,
          min=0, max=2147483647, clamp_min=True, clamp_max=True, order=20,
          label="Seed",
          help="Generation seed — an arbitrary integer. Pulse Randomize "
               "Seed for a fresh random value (like the web client's dice)."),
    Param("Randomizeseed", None, "Synthesis", "Pulse", "session", order=25,
          label="Randomize Seed",
          help="Set Seed to a random integer."),
    Param("Feedback", "feedback", "Synthesis", "Float", "continuous", default=0.0,
          min=0.0, max=1.0, clamp_min=True, clamp_max=True, order=30,
          help="Feedback loop (pro). Use with caution."),
    Param("Shift", "shift", "Synthesis", "Float", "continuous", default=0.5,
          min=0.0, max=1.0, clamp_min=True, clamp_max=True, order=40,
          help="Temporal phase alignment (pro)."),
    # Wire key is `hint_strength`, but the canonical's user-facing label
    # for this control is "Structure" (see demon-public-demo/vendor/demon-ui/
    # components/Performance/SliderTile.tsx and LiteControls.tsx). The
    # control governs how closely the model follows the source's section /
    # rhythm / dynamics — i.e. its structure. Keep the Param `name` as
    # `Hintstrength` so the wire mapping is unchanged.
    Param("Hintstrength", "hint_strength", "Synthesis", "Float", "continuous",
          default=1.4, min=0.0, max=2.0, clamp_min=True, clamp_max=True,
          order=50, label="Structure",
          help="How closely the model follows the original song's structure "
               "— sections, rhythm, dynamics. Crank it up to keep the "
               "arrangement intact; drop it to let the model rearrange "
               "more freely. (wire: hint_strength)"),
    Param("Timbrestrength", "timbre_strength", "Synthesis", "Float", "continuous",
          default=1.0, min=0.0, max=1.0, clamp_min=True, clamp_max=True,
          order=60, label="Timbre Strength",
          help="Source vs generation timbre blend (rides own WS message)."),
    Param("Guidancescale", "guidance_scale", "Synthesis", "Float", "continuous",
          default=7.0, min=0.0, max=15.0, clamp_min=True, clamp_max=True,
          order=70, label="Guidance Scale",
          help="RCFG guidance (pro)."),
    Param("Cfgrescale", "cfg_rescale", "Synthesis", "Float", "continuous",
          default=0.0, min=0.0, max=1.0, clamp_min=True, clamp_max=True,
          order=80, label="CFG Rescale",
          help="CFG saturation taming (pro)."),
    Param("Odenoise", "ode_noise", "Synthesis", "Float", "continuous",
          default=0.0, min=0.0, max=0.5, clamp_min=True, clamp_max=True,
          order=90, label="ODE Noise",
          help="ODE noise injection (pro)."),
    Param("Periodicity", "periodicity", "Synthesis", "Float", "continuous",
          default=0.0, min=0.0, max=12.5, clamp_min=True, clamp_max=True,
          order=100, help="Beat-grid periodicity for SDE (pro)."),

    Param("Channelsheader", None, "Synthesis", "Header", "session",
          order=110, section_header=True, label="Channels"),
] + [
    Param(f"Chg{i}", f"ch_g{i}", "Synthesis", "Float", "continuous", default=1.0,
          min=0.0, max=3.0, clamp_min=True, clamp_max=True,
          order=120 + i, label=f"ch_g{i}",
          help=f"Channel guidance group {i}.")
    for i in range(8)
] + [
    Param("Keystoneheader", None, "Synthesis", "Header", "session",
          order=140, section_header=True, label="Keystone Channels"),
] + [
    Param(f"Ch{n}", f"ch{n}", "Synthesis", "Float", "continuous", default=1.0,
          min=0.0, max=3.0, clamp_min=True, clamp_max=True,
          order=150 + i, label=f"ch{n}",
          help=f"Keystone channel {n} guidance.")
    for i, n in enumerate([13, 14, 19, 23, 29, 56])
]


# -----------------------------------------------------------------------------
# Page 5: RCFG + DCW
# -----------------------------------------------------------------------------
RCFG_DCW_PARAMS: list[Param] = [
    Param("Rcfgmode", "rcfg_mode", "RCFG+DCW", "Menu", "continuous",
          default="off", order=10, label="RCFG Mode",
          menu_names=("off", "initialize", "self"),
          menu_labels=("Off", "Initialize", "Self"),
          help="RCFG mode selection."),
    Param("Dcwheader", None, "RCFG+DCW", "Header", "session",
          order=20, section_header=True, label="DCW (Wavelet Domain Correction)"),
    Param("Dcwenabled", "dcw_enabled", "RCFG+DCW", "Toggle", "continuous",
          default=True, order=30, label="DCW Enabled",
          help="Enable wavelet domain correction."),
    Param("Dcwmode", "dcw_mode", "RCFG+DCW", "Menu", "continuous",
          default="double", order=40, label="DCW Mode",
          menu_names=("low", "high", "double", "pix"),
          menu_labels=("Low", "High", "Double", "Pix"),
          help="Correction bands (pro)."),
    # Canonical labels these "DCW low" / "DCW high" — band-pair naming
    # rather than the engine-internal "scaler" / "high_scaler". See
    # DISPLAY_NAMES in demon-public-demo's SliderTile.tsx.
    Param("Dcwscaler", "dcw_scaler", "RCFG+DCW", "Float", "continuous",
          default=0.05, min=0.0, max=0.5, clamp_min=True, clamp_max=True,
          order=50, label="DCW low"),
    Param("Dcwhighscaler", "dcw_high_scaler", "RCFG+DCW", "Float", "continuous",
          default=0.02, min=0.0, max=0.5, clamp_min=True, clamp_max=True,
          order=60, label="DCW high"),
    Param("Dcwwavelet", "dcw_wavelet", "RCFG+DCW", "Menu", "continuous",
          default="haar", order=70, label="DCW Wavelet",
          menu_names=("haar", "db4", "sym8", "db8"),
          menu_labels=("Haar", "DB4", "Sym8", "DB8"),
          help="Wavelet family (pro)."),
    Param("Dcwmultblend", "dcw_mult_blend", "RCFG+DCW", "Float", "continuous",
          default=0.0, min=0.0, max=1.0, clamp_min=True, clamp_max=True,
          order=80, label="DCW Mult Blend"),
    Param("Dcwmagphase", "dcw_mag_phase", "RCFG+DCW", "Float", "continuous",
          default=0.0, min=0.0, max=1.0, clamp_min=True, clamp_max=True,
          order=90, label="DCW Mag/Phase"),
    Param("Dcwsoftthresh", "dcw_soft_thresh", "RCFG+DCW", "Float", "continuous",
          default=0.0, min=0.0, max=0.3, clamp_min=True, clamp_max=True,
          order=100, label="DCW Soft Thresh"),
]


# -----------------------------------------------------------------------------
# Page 6: Curves (client-side scheduled curves)
# -----------------------------------------------------------------------------
# v0.2.2 rewrite. The previous Curves page sent JSON specs to wire keys
# `sde_denoise_curve`, `ode_noise_curve`, etc. that the DEMON server
# stopped applying — they were a static whole-buffer schedule the pod
# wasn't honoring. The web client moved to CLIENT-SIDE evaluation:
# every frame, sample each enabled curve at the loop playhead and
# write the SCALAR result into the regular continuous-param wire keys
# (`denoise`, `hint_strength`, `feedback`, `shift`). Matches
# demon-public-demo/vendor/demon-ui/hooks/useScheduledCurves.ts.
#
# Schedulable params come from demon-public-demo/types/curves.ts:
#   SCHEDULEABLE_PARAMS = ["denoise", "hint_strength", "feedback", "shift"]
#
# Per-LoRA strength curves (`lora_str_<id>`) are deferred — they need
# dynamic-param plumbing similar to the existing Lorastr<id> rows.
#
# All curve params are `session`-category (local-only). The SAMPLED
# values from `_sample_curves()` in demon_ext.py flow through the
# existing `denoise`/`hint_strength`/`feedback`/`shift` continuous-
# param wire keys, not through any new wire surface.
CURVE_PARAMS: list[Param] = [
    # Master enable. Off by default and intentionally NOT remembered
    # across sessions (the web client documents the rationale: a
    # persisted-on schedule silently re-applied stored curves to
    # `denoise`/`shift`/etc. on every fresh session, surprising users).
    Param("Schedulecurves", None, "Curves", "Toggle", "session",
          default=False, order=5, label="Schedule Curves",
          help="Master enable for client-side scheduled curves. When "
               "on, each enabled curve below is sampled at the loop "
               "playhead every tick and the value writes into the "
               "matching continuous param (Denoise / Hint Strength / "
               "Feedback / Shift). Off = no curve drives any param. "
               "Manual slider movements override the curve for 500 ms."),

    # Per-param triple: header, enable, JSON spec. Default JSON is a
    # constant 0.5 ramp so toggling Enable does something visible.
    # The (Param-name, wire-name, label, slider-min, slider-max,
    # default-mid) tuples mirror what's already on Synthesis.

    Param("Denoisecurveheader", None, "Curves", "Header", "session",
          order=10, section_header=True, label="Denoise"),
    Param("Denoisecurveenable", None, "Curves", "Toggle", "session",
          default=False, order=11, label="Enable",
          help="Enable curve scheduling for Denoise."),
    Param("Denoisecurve", None, "Curves", "Str", "session",
          default='{"points": [[0, 0.5], [1, 0.5]]}',
          order=12, multiline=True, label="Curve JSON",
          help='Linear-interp curve as {"points": [[x, y], ...]} with '
               'x and y in [0, 1]. First x=0, last x=1; sampled at t = '
               'playhead/duration each tick. Writes into the Denoise '
               'slider when enabled.'),

    Param("Hintstrengthcurveheader", None, "Curves", "Header", "session",
          order=20, section_header=True, label="Structure"),
    Param("Hintstrengthcurveenable", None, "Curves", "Toggle", "session",
          default=False, order=21, label="Enable",
          help="Enable curve scheduling for Structure (wire: hint_strength)."),
    Param("Hintstrengthcurve", None, "Curves", "Str", "session",
          default='{"points": [[0, 0.5], [1, 0.5]]}',
          order=22, multiline=True, label="Curve JSON",
          help="Same format as Denoise Curve JSON. y is normalized [0, 1]; "
               "mapped to the param's [min, max] range at sample time."),

    Param("Feedbackcurveheader", None, "Curves", "Header", "session",
          order=30, section_header=True, label="Feedback"),
    Param("Feedbackcurveenable", None, "Curves", "Toggle", "session",
          default=False, order=31, label="Enable",
          help="Enable curve scheduling for Feedback."),
    Param("Feedbackcurve", None, "Curves", "Str", "session",
          default='{"points": [[0, 0.0], [1, 0.0]]}',
          order=32, multiline=True, label="Curve JSON",
          help="Same format as Denoise Curve JSON."),

    Param("Shiftcurveheader", None, "Curves", "Header", "session",
          order=40, section_header=True, label="Shift"),
    Param("Shiftcurveenable", None, "Curves", "Toggle", "session",
          default=False, order=41, label="Enable",
          help="Enable curve scheduling for Shift."),
    Param("Shiftcurve", None, "Curves", "Str", "session",
          default='{"points": [[0, 0.5], [1, 0.5]]}',
          order=42, multiline=True, label="Curve JSON",
          help="Same format as Denoise Curve JSON."),
]


# Map of curve-param-name -> (base-param-name, enable-param-name)
# used by demon_ext._sample_curves() to find which slider to write into
# when each curve fires. Keyed by the JSON-spec param's TD name.
CURVE_PARAM_BINDINGS: dict[str, tuple[str, str]] = {
    "Denoisecurve":         ("Denoise",       "Denoisecurveenable"),
    "Hintstrengthcurve":    ("Hintstrength",  "Hintstrengthcurveenable"),
    "Feedbackcurve":        ("Feedback",      "Feedbackcurveenable"),
    "Shiftcurve":           ("Shift",         "Shiftcurveenable"),
}


# -----------------------------------------------------------------------------
# Page 7: Sources
# -----------------------------------------------------------------------------
SOURCES_PARAMS: list[Param] = [
    # ---------- Swap (replace the source track mid-session) ----------
    Param("Swapheader", None, "Sources", "Header", "session",
          order=5, section_header=True, label="Swap Source Track"),
    Param("Swapsourcefile", None, "Sources", "File", "session", default="",
          order=10, label="Swap Source File",
          help="WAV/MP3/M4A. If set, Swap Source uploads this file. "
               "Otherwise it uses the wired CHOP input."),
    Param("Swapsource", None, "Sources", "Pulse", "discrete", order=15,
          label="Swap Source",
          help="Swap to a new source track. Uses Swap Source File if set, "
               "else the wired CHOP input. Honors Swap Tags."),
    Param("Swaptags", None, "Sources", "Str", "session", default="", order=20,
          label="Swap Tags", multiline=True,
          help="Optional prompt tags override for Swap Source."),

    # ---------- Timbre reference ----------
    Param("Timbreheader", None, "Sources", "Header", "session",
          order=25, section_header=True, label="Timbre Reference"),
    Param("Timbresourcefile", None, "Sources", "File", "session", default="",
          order=30, label="Timbre Source File",
          help="WAV/MP3/M4A used as a timbre reference. If empty, the wired "
               "CHOP input is snapshotted instead."),
    Param("Settimbresource", None, "Sources", "Pulse", "discrete", order=35,
          label="Set Timbre Source",
          help="Upload the Timbre Source File (or wired CHOP) as a timbre reference."),
    Param("Cleartimbresource", None, "Sources", "Pulse", "discrete", order=40,
          label="Clear Timbre Source"),
    Param("Timbrefixture", None, "Sources", "Str", "session", default="", order=50,
          label="Timbre Fixture",
          help="Name of a server-side fixture to use as timbre reference."),
    Param("Settimbrefixture", None, "Sources", "Pulse", "discrete", order=55,
          label="Apply Timbre Fixture"),

    # ---------- Structure reference ----------
    Param("Structureheader", None, "Sources", "Header", "session",
          order=58, section_header=True, label="Structure Reference"),
    Param("Structuresourcefile", None, "Sources", "File", "session", default="",
          order=60, label="Structure Source File",
          help="WAV/MP3/M4A used as a structure reference. If empty, the wired "
               "CHOP input is snapshotted instead."),
    Param("Setstructuresource", None, "Sources", "Pulse", "discrete", order=65,
          label="Set Structure Source",
          help="Upload the Structure Source File (or wired CHOP) as a structure reference."),
    Param("Clearstructuresource", None, "Sources", "Pulse", "discrete", order=70,
          label="Clear Structure Source"),
    Param("Structurefixture", None, "Sources", "Str", "session", default="",
          order=80, label="Structure Fixture",
          help="Name of a server-side fixture to use as structure reference."),
    Param("Setstructurefixture", None, "Sources", "Pulse", "discrete", order=85,
          label="Apply Structure Fixture"),
]


# -----------------------------------------------------------------------------
# Aggregate
# -----------------------------------------------------------------------------
PARAMS: list[Param] = (
    SESSION_PARAMS
    + INIT_PARAMS
    + PROMPT_LORA_PARAMS
    + SYNTHESIS_PARAMS
    + RCFG_DCW_PARAMS
    + CURVE_PARAMS
    + SOURCES_PARAMS
)


# Pages, in display order
PAGES: list[str] = [
    "Session", "Init", "Prompt+LoRA", "Synthesis", "RCFG+DCW", "Curves", "Sources"
]


# -----------------------------------------------------------------------------
# Lookups
# -----------------------------------------------------------------------------

PARAM_BY_NAME: dict[str, Param] = {p.name: p for p in PARAMS}
PARAM_BY_WIRE: dict[str, Param] = {p.wire_name: p for p in PARAMS if p.wire_name}
INIT_PARAM_NAMES: frozenset[str] = frozenset(
    p.name for p in PARAMS if p.category == "init"
)
CONTINUOUS_PARAM_NAMES: frozenset[str] = frozenset(
    p.name for p in PARAMS if p.category == "continuous"
)


def session_config_defaults() -> dict[str, Any]:
    """Build the initial SessionConfig dict from default Init param values.

    The extension overrides these with actual param values at Connect() time.
    """
    cfg: dict[str, Any] = {}
    for p in PARAMS:
        if p.category == "init" and p.wire_name:
            cfg[p.wire_name] = p.default
    return cfg


def continuous_defaults() -> dict[str, Any]:
    """All continuous-param default values keyed by wire name."""
    return {p.wire_name: p.default for p in PARAMS if p.category == "continuous" and p.wire_name}


# -----------------------------------------------------------------------------
# Discrete message routing
# -----------------------------------------------------------------------------
# Maps pulse-par name → wire message kind, for OnParChange to dispatch on.

DISCRETE_PULSE_TO_KIND: dict[str, str] = {
    "Sendprompt": "prompt",
    "Setpromptblend": "set_prompt_blend",
    "Swapsource": "swap_source",
    "Settimbresource": "set_timbre_source",
    "Cleartimbresource": "clear_timbre_source",
    "Settimbrefixture": "set_timbre_fixture",
    "Setstructuresource": "set_structure_source",
    "Clearstructuresource": "clear_structure_source",
    "Setstructurefixture": "set_structure_fixture",
}


# -----------------------------------------------------------------------------
# Params-message wire filter
# -----------------------------------------------------------------------------
# Continuous-param wire keys the server's `params` handler does NOT
# accept. Each has a dedicated WS message instead:
#   prompt_blend     -> set_prompt_blend
#   timbre_strength  -> set_timbre_strength
#   lora_blend       -> UI-only (fans out into lora_str_<id>; the
#                       engine itself doesn't know `lora_blend`).
# Sending any of these in a `params` raw dict caused the server to
# close the WS — empirically the cause of the "disconnects when
# messing with prompts and LoRAs" reports.
# Source: demon-public-demo/vendor/demon-ui/hooks/useParamSync.ts
# lines 42–53.

PARAMS_NOT_FOR_WIRE: frozenset[str] = frozenset({
    "prompt_blend", "timbre_strength", "lora_blend",
})


def filter_params_for_wire(raw: dict[str, Any],
                           enabled_loras: "frozenset[str] | set[str]",
                           ) -> dict[str, Any]:
    """Strip wire keys the server's `params` handler rejects, and drop
    `lora_str_<id>` entries for non-enabled LoRAs.

    Pure function (no TD, no self) so the params-pacer THREAD can call
    it — `enabled_loras` must be a pre-computed set, not live par reads.
    Returns a NEW dict; never mutates `raw`.
    """
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k in PARAMS_NOT_FOR_WIRE:
            continue
        if k.startswith("lora_str_"):
            if k[len("lora_str_"):] not in enabled_loras:
                continue
        out[k] = v
    return out

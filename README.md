# demon-td

> **THIS OPERATOR IS A WORK-IN-PROGRESS.**
> Today, you may see slight differences in performance between DEMON and the TD op. Those are not intrinsic limitations and we're working to improve the op. We welcome feedback, ideas, and contributions.

TouchDesigner operator for **DEMON** — real-time AI music generation.

Drop the operator into any TouchDesigner project, point it at a DEMON server,
hit Connect, and you'll hear AI-generated audio playing through your speakers.
(The `.tox` loads a few bundled Python deps from a `vendor/` folder — grab the
**`demonTD-vX.Y.Z.zip`** bundle, not the bare `.tox`, and keep them together.
See [Quick start](#quick-start).)
Every public DEMON parameter is exposed through native TD parameter pages, and
the whole thing is scriptable from Python.

> **Status:** v0.2 — end-to-end audio playback working on macOS, both against
> your own DEMON pod and against the hosted Daydream queue at
> `music.daydream.live`. Windows build pending.

---

## What it does

- Connects to a DEMON backend over WebSocket. Two modes (`Mode` menu on the
  Session page):
  - **Direct** — your own pod URL (local or Vast.ai).
  - **Hosted** — joins the Daydream queue at `music.daydream.live` and plays on
    a managed pod. Paste your API key from
    [app.daydream.live/dashboard/api-keys](https://app.daydream.live/dashboard/api-keys),
    then hit Connect.
- Exposes ~70 parameters across 7 pages — every DEMON public param, plus
  session and operational controls.
- Plays generated audio out of your Mac's default audio device via Python +
  PortAudio (bundled). No need to wire an external `Audio Device Out CHOP` —
  it just works.
- Exposes the live audio as a `Script CHOP` inside the COMP for visual
  reactivity — wire an `Analyze CHOP`, `Audio Spectrum CHOP`, peak detector,
  envelope follower, anything that consumes audio samples at frame rate.
- **Python API** for driving the op from anywhere else in a TD project.
- Mirrors traditional TD vendor-op UX: pulse actions, menu enums, header
  dividers, status read-outs.

## Quick start

1. Download **`demonTD-vX.Y.Z.zip`** (the bundle — **not** the bare
   `demonTD.tox`) from the latest [GitHub release](https://github.com/daydreamlive/demonTD/releases),
   and extract it. You'll get `demonTD.tox` next to a `vendor/` folder.
   **Keep them together** — the operator loads its Python deps
   (websocket-client, certifi, zstandard, portaudio) from `vendor/` at
   runtime. If you move the `.tox` away from `vendor/`, the extension
   fails to load with `ModuleNotFoundError: No module named 'websocket'`.
2. Drag `demonTD.tox` (from the extracted folder) into any TouchDesigner
   project. A Base COMP named `demon` appears.
3. **One-time TD setup:** Edit → Preferences → Audio → Audio Device → **None**.
   This stops TouchDesigner from holding your Mac's audio device, which would
   otherwise prevent our Python audio thread from opening it. (Details in the
   [Audio output troubleshooting](#audio-output-troubleshooting) section.)
4. On the **Session** page, pick a mode:
   - **Hosted** (Daydream queue, no pod to manage — see
     [Quick start — Hosted mode](#quick-start--hosted-mode) below), OR
   - **Direct** (your own pod). Set **Server URL** to your DEMON pod
     (e.g. `ws://81.183.231.113:44105/` for a Vast.ai pod, or
     `ws://127.0.0.1:8765/` for a local pod).
5. Set **Source Audio File** to any audio file (WAV, MP3, M4A — the operator
   auto-converts via `afconvert` on Mac).
6. Pulse **Connect**.
7. Within ~3 seconds of `initial buffer: 1152000 frames` in the textport,
   you'll hear your source audio looping. After another few seconds, DEMON's
   generated audio begins patching the loop progressively.
8. Change **Prompt** on the Prompt+LoRA page and pulse **Send Prompt**. Move
   **Denoise**, channel gains, etc. — they stream to the server at the 8 ms
   tick.

## Quick start — Hosted mode

Hosted mode joins the Daydream queue at `music.daydream.live` and plays on a
managed pod. You don't need to spin up your own DEMON instance.

### 1. Get an API key

1. Open [app.daydream.live/dashboard/api-keys](https://app.daydream.live/dashboard/api-keys)
   in your browser.
2. Sign in (Google, GitHub, or email).
3. Create an API key on that page, give it a name like `demonTD`, and copy
   the key. It looks something like `dd_...` and is shown to you once — keep
   it somewhere safe.

### 2. Sign in from TouchDesigner

1. On the **Session** page, set **Mode** → **Hosted (Daydream queue)**.
2. Pulse **Paste API Key**. TD opens `app.daydream.live/dashboard/api-keys`
   in your browser (in case you don't have a key yet) and shows a paste
   dialog.
3. Paste your key, click OK.
4. The Status par should read `Signed in as <your email>`. The key is
   persisted to `~/Library/Application Support/derivative/daydream_auth.json`
   (not into the .toe), so you only do this once per machine.

### 3. Play

1. Pick a **Source Audio File** (any WAV / MP3 / M4A).
2. Pulse **Connect**. Status walks you through:
   - `Joining queue...`
   - `Queued (position N)` if there's a wait (rare during off-peak)
   - `Connecting to hosted pod...`
   - `server ready: ch=2 sr=48000`
   - audio plays.
3. After your session expires (`Expires in (s)` counts down), hit
   **Still playing?** to extend.

If `Connect` fails with `Paywall: …`, your Daydream account is out of trial
credits — check the API-keys dashboard for billing info.

## Parameter pages

| Page | What's there |
|---|---|
| **Session** | Connect / Disconnect, **Mode** (Direct / Hosted), Server URL (direct), Hosted Base URL + API Key + Paste API Key (hosted), Queue Position / Expires in / Deny reason (hosted readouts), Still playing? pulse (hosted), Source Audio File, Status, **Python Audio Out** toggle, **Debug Logging** toggle. |
| **Init** | Session-start params (`sde`, `lora`, `depth`, `vae_window`, `crop`, `steps`, `fast_vae`, `walk_window`, `walk_window_s`, Initial Prompt, Initial Prompt B, Fixture Name). Editing while connected reverts + prompts you to Reconnect. |
| **Prompt+LoRA** | Send Prompt pulse, Prompt (multiline), Key (70-keyscale menu), Time Signature, Prompt Blend, LoRA Blend, dynamic per-LoRA enable + strength rows populated from the server's `lora_catalog`. |
| **Synthesis** | Denoise, Seed, Feedback, Shift, Hint Strength, Timbre Strength, Guidance Scale, CFG Rescale, ODE Noise, Periodicity, 8× channel groups (`ch_g0..ch_g7`), 6× keystone channels (`ch13, ch14, ch19, ch23, ch29, ch56`). |
| **RCFG+DCW** | RCFG Mode menu, DCW block (`enabled`, `mode`, `scaler`, `high_scaler`, `wavelet`, `mult_blend`, `mag_phase`, `soft_thresh`). |
| **Curves** | JSON spec strings for `sde_denoise_curve`, `ode_noise_curve`, `x0_target_curve`, `velocity_scale_curve`, `initial_noise_curve`. |
| **Sources** | Swap Source, Set/Clear Timbre Source, Set/Clear Structure Source, fixture-name fields for both. |

All defaults are aligned with `demon-public-demo` so you get the same
out-of-the-box sound as the web client.

## Python API

From anywhere in your TD project:

```python
demon = op('/project1/demon')

demon.Connect()
demon.SendPrompt('uplifting techno', key='Am', time_signature='4')
demon.SetParams({'denoise': 0.8, 'guidance_scale': 7.5, 'ch_g0': 1.2})
demon.EnableLora('bach', strength=0.7)

# Status
print(demon.IsConnected, demon.Status)
```

Full public surface:

```text
# Properties
IsConnected -> bool
Status      -> dict

# Session
Connect()
Disconnect()

# Continuous (one-shot or batch)
SetParam(name, value)
SetParams(d: dict)

# Discrete
SendPrompt(tags=None, key=None, time_signature=None)
SetPromptBlend(value=None)
EnableLora(id, strength=1.0)
DisableLora(id)
SetTimbreStrength(value)
SetTimbreSource(chop=None, name="td_input")
SetTimbreFixture(name=None)
ClearTimbreSource()
SetStructureSource(chop=None, fixture=None, name="td_input")
SetStructureFixture(name=None)
ClearStructureSource()
SwapSource(chop=None, tags=None, key=None, time_signature=None, fixture=None)
```

## Audio routing — for power users

> **TL;DR**: speakers just work via `Python Audio Out`. For TD-native audio
> chains, use a `Select CHOP` that references `/project1/demon/audio_out`
> directly. Don't try to wire audio across the COMP output port.

This operator deliberately bypasses TD's CHOP audio chain for playback. Here's
why and what that means for your network:

### Why we bypass TD's audio chain

TouchDesigner evaluates COMP boundaries at cook rate, not at audio rate (44.1k
/ 48 kHz). Pulling an audio-rate signal out of a `Script CHOP` inside a Base
COMP, through the COMP's output port, into an external `Audio Device Out CHOP`
falls back to frame-rate sampling-and-hold — you get 60 Hz steps instead of
clean audio. This is intentional TD architecture, not a bug: boundaries are
designed for control-rate signals (envelopes, triggers, analysis values), and
audio-rate processing is expected to stay self-contained within a single COMP.

Source: [Audio CHOPs in TouchDesigner — sample rates and COMP boundaries](https://nvoid.gitbooks.io/introduction-to-touchdesigner/content/CHOPs/4-4-Sample-Rates.html)

### How playback actually works

A small Python audio thread (PortAudio, bundled — no install required) runs
inside the COMP and reads samples directly from the loop buffer that the
WebSocket recv thread writes into. The thread plays through your system
default audio device. This is functionally identical to how
`demon-public-demo`'s web client plays through `AudioContext` / `AudioWorklet`.

If you want playback to go somewhere other than the system default device:
change your Mac's audio output in System Settings → Sound. Or install
[BlackHole](https://github.com/ExistentialAudio/BlackHole) for virtual
device routing — once BlackHole is the system default, drop an
`Audio Device In CHOP` set to BlackHole and you get audio-rate samples inside
TD with full chain access (effects, mixing, multi-device routing, recording).

### Visual reactivity (Analyze CHOP, FFT, peak detector, envelope follower)

These are all **control-rate** consumers — they cook at frame rate, which is
exactly what visualizers want. Wire any of them off the demon COMP's output
port. The internal `audio_out Script CHOP` keeps an up-to-date snapshot of the
current play position via a non-mutating `peek()` on the loop buffer, so there's
no race with the audio playback thread.

```
[ demon COMP ] ─► Analyze CHOP ─► Composite/TOP shader/visualizer
              ─► Audio Spectrum CHOP ─► UI bars
              ─► Math CHOP RMS ─► global kick reactivity
```

### TD-native audio chain (Audio Filter, recording, multi-device)

Two patterns work:

**1. Select CHOP with explicit reference (recommended for occasional use)**

Drop a `Select CHOP` in your network, set its **CHOP** parameter to
`/project1/demon/audio_out`. This bypasses the wired-input cook propagation
issue described above. Wire the Select CHOP into any Audio CHOP chain you
like. Caveats: still subject to cook-rate sampling, so audio-rate fidelity
isn't perfect — use BlackHole if you need clean audio rate.

**2. BlackHole virtual device (recommended for production audio chains)**

`brew install --cask blackhole-2ch`, set Mac audio output to BlackHole, drop
`Audio Device In CHOP` set to BlackHole inside any COMP. You now have the
same audio that's playing, exposed as a native TD audio-rate stream. Full
chain access. Zero added latency on the loopback.

### Toggling the Python audio out

If you only want analysis (no playback), turn the **Python Audio Out** toggle
off on the Session page. The audio still flows into the LoopBuffer, the
`audio_out` Script CHOP still updates for analysis consumers, but no sound
plays through speakers.

### Audio output troubleshooting

If you see `[speaker_out] direct Pa_OpenDefaultStream@... failed: Internal
PortAudio error (err=-9986) hostErr code=-10851 text='Audio Unit: Invalid
Property Value'` in the textport on Connect, **TouchDesigner is holding
your output device's Core Audio AudioUnit**, and our Python audio thread
can't open it.

The fix is one click:

> **Edit → Preferences → Audio → Audio Device → None**

Save Preferences. TD releases the device immediately; no restart needed.
Re-pulse Connect and audio plays.

#### Why this happens

TD has a global "Audio Device" preference. When it's set to anything
other than `None`, TD opens that device's Core Audio output AudioUnit at
startup — independent of whether you have any `Audio Device In/Out
CHOPs` in your network. Once TD has the AudioUnit bound, Core Audio
refuses to let a second client (our `SpeakerOut` via PortAudio) call
`AudioUnitSetProperty(kAudioUnitProperty_StreamFormat)` on the same
device. The result is `kAudioUnitErr_InvalidPropertyValue` (-10851)
inside PortAudio, which surfaces as the "Internal PortAudio error" line.

You can verify your bundled PortAudio + device are otherwise fine by
running `python3 scripts/probe_portaudio.py` from a terminal — it makes
the same call demonTD makes, without TouchDesigner in the picture. If
the probe prints "OK", TD is the culprit.

#### Alternative: keep TD's Audio Device set, bypass `Python Audio Out`

If you need TD to own the device (because you have other audio ops in
your project), leave the TD preference alone and route demonTD's audio
through TD's chain instead:

1. Toggle the **Python Audio Out** par OFF on the Session page.
2. Wire the COMP's `out_chop` output port to a TD-native
   `Audio Device Out CHOP` placed outside the COMP.

TD's audio chain has cook-rate quirks across COMP boundaries (documented
in `src/audio.py`), but for a single output device with no upstream
processing it generally works.

#### Other rarer causes

Two other things can make `Pa_OpenDefaultStream` fail with -10851, both
much rarer than the TD-holds-the-device case above:

1. **Default macOS output device is in a weird state.** Try System
   Settings → Sound → Output → switch to MacBook Speakers (or any other
   device), then back. Forces Core Audio to reconfigure.
2. **Device hardware format mismatch.** Open Audio MIDI Setup (Spotlight),
   select the device that's failing, set Format to
   `Stereo 48000 Hz, 32-bit Float`, then re-pulse Connect.

The textport log lines starting with `[speaker_out]` will show what
PortAudio tried and the underlying Core Audio error code on each
failure — that's enough to file a precise bug if none of the
workarounds help.

## Debug toggle

The Session page has a **Debug Logging** toggle (default off). When on:

- Per-tick state, WS frame echoes, source/initial-buffer/slice WAV dumps to
  `/tmp/demon-debug/`, byte-level hex dumps of incoming binary frames.
- Useful for filing a bug or investigating an audio decode problem.

Off by default so the textport stays usable.

## Troubleshooting: extension won't load / `No module named 'websocket'`

If the textport shows any of:

```
DAT compile error: /demon/ws_client ... ModuleNotFoundError: No module named 'websocket'
[demon_ext] zstandard load failed: ModuleNotFoundError: No module named 'zstandard'
Error retrieving extension for /demon: tdError: Module compilation error.
```

…you downloaded the **bare `demonTD.tox`** without the `vendor/` folder.
The operator loads its Python deps from `vendor/` at runtime, so the
`.tox` needs that folder next to it (or next to your `.toe`).

**Fix:** download **`demonTD-vX.Y.Z.zip`** from the
[release](https://github.com/daydreamlive/demonTD/releases), extract it,
keep `demonTD.tox` and `vendor/` in the same folder, and drag the `.tox`
in from there. (`zstandard` missing on its own is harmless — the operator
falls back to uncompressed slices — but a missing `websocket` is fatal.)

## Architecture

```
                    ┌─────────────── demon Base COMP ──────────────────┐
                    │                                                  │
   user params ──►  │  ParExec ─► _dirty ─► Timer 8ms ─► params msg  ─►│ WS ─► DEMON
                    │                                                  │
                    │                          ┌───── slice (binary) ──│ WS ◄─ DEMON
                    │                          │                       │
                    │                          ▼                       │
                    │             ┌──── LoopBuffer ────┐                │
                    │             │   patch/add_delta  │                │
                    │             │   at start_sample  │                │
                    │             └────────┬───────────┘                │
                    │                      │                            │
                    │             ┌────────┴───────────┐                │
                    │             │                    │                │
                    │             ▼                    ▼                │
                    │       SpeakerOut          audio_out CHOP          │
                    │       (PortAudio)         (peek snapshot)         │
                    │             │                    │                │
                    │             │                    └──► Out CHOP ───┼──► Analyze CHOP, FFT, etc.
                    │             │                                     │
                    └─────────────┼─────────────────────────────────────┘
                                  ▼
                          system audio device
```

- **One COMP = one session.** Spin up multiple `demonTD.tox` copies for
  parallel sessions.
- **Continuous param fanout**: parameter changes coalesce into a single
  `{type:"params", raw:{...}}` message every 8 ms. A frantic slider drag
  becomes ≤ 125 Hz of dispatch, not 60×.
- **All current values are sent on `ready`**, so the server starts generating
  immediately with your configured `denoise`, `hint_strength`, channel gains,
  etc. — no need to nudge a slider to "kick things off."
- **Audio model mirrors demon-public-demo's AudioPlayer**: server's initial
  buffer is the full track loop. Subsequent slices are positional patches at
  `start_sample` indices. Playback loops the buffer continuously while slices
  evolve its content.

## Repo layout

```
demon-td/
  src/                       # Python source (file-synced into the COMP's Text DATs)
    params.py                # SOURCE OF TRUTH for the parameter schema
    wire.py                  # WS message encoders + slice decoder
    queue_client.py          # /api/queue/{join,status,extend,leave} (hosted mode)
    oauth.py                 # Daydream sign-in + token exchange (hosted mode)
    audio.py                 # LoopBuffer + SpeakerOut (ctypes → PortAudio)
    ws_client.py             # Python WebSocket (replaces TD's broken WS DAT)
    demon_ext.py             # DemonExt — the extension class loaded by TD
  vendor/                    # bundled per-platform native deps
    zstandard/{darwin-arm64, darwin-x64, win-amd64}/
    sounddevice/             # pure-Python wrapper + libportaudio.dylib (universal2)
    websocket-client/        # pure-Python WebSocket
  build/
    build_tox.py             # under TD CLI: regenerate demonTD.tox from src/
    template.toe             # base scaffold (generated on first build)
  examples/
    minimal.toe              # demo project referencing demonTD.tox
  tests/                     # pytest, runs outside TD
  dist/                      # gitignored; demonTD.tox lives here after build
  README.md
  CHANGELOG.md
```

## Building locally

The `.tox` is built from inside a running TouchDesigner:

1. Open TouchDesigner.
2. Drop a Text DAT into the network.
3. Set its **File** par to `<repo>/build/build_tox.py` and turn on **Sync to File**.
4. Right-click the DAT → **Run Script**.
5. Watch Alt+T (Textport) for `[build_tox] wrote .../dist/demonTD.tox`.

Re-run the script any time `src/*.py` changes. The script is idempotent.

## Bundled native libraries

We bundle three things under `vendor/` so users don't need to install anything:

- **`zstandard`** — for DEMON's zstd-compressed audio slices. Per-platform wheels.
- **`websocket-client`** — pure-Python WS lib that replaces TD's broken WebSocket
  DAT (TD 2025's DAT silently drops binary frames > a few MB).
- **`sounddevice` + PortAudio** — for the Python audio output path. Universal
  macOS dylib (arm64 + x86_64) + Windows x64 DLLs (regular + ASIO variants)
  ship in the same `_sounddevice_data/portaudio-binaries/` directory. The
  cross-platform loader in `src/audio.py` picks the right one at runtime.

The Windows binaries are vendored but **not yet runtime-tested** by the
maintainer — please open an issue if anything misbehaves on Windows.

## Development

```bash
python -m venv .venv
.venv/bin/pip install pytest responses numpy
PYTHONPATH=src .venv/bin/pytest tests/ -v
```

All Python in `src/` is unit-testable outside TouchDesigner — the WS protocol,
queue API, OAuth flow, ring buffer, and param schema each have their own tests.

`src/demon_ext.py` is the only module that imports TD globals (`me`, `op`,
`project`, etc.), and it does so lazily inside methods so the test runner
can still import it.

## Releases

Two assets are attached to each [GitHub release](https://github.com/daydreamlive/demonTD/releases):

- **`demonTD-vX.Y.Z.zip`** — the bundle to download. Contains `demonTD.tox`
  + the `vendor/` folder of Python deps. Extract it and keep the two
  together (see [Quick start](#quick-start)).
- `demonTD.tox` — the bare operator, for repo users who already have
  `vendor/` checked out. **Don't** download this on its own — without
  `vendor/` next to it the extension fails to load
  (`ModuleNotFoundError: No module named 'websocket'`).

## Out of scope (v0.2, deferred to v0.2.x+)

- **Auto-reconnect on WS drop.** v0.2 keeps the explicit Connect /
  Disconnect model. Transient WS errors will surface in the Status par
  rather than retrying silently.
- **Encrypted apiKey on disk.** The persisted `daydream_auth.json` is
  plaintext JSON, matching the rtmg-vst PropertiesFile pattern. Lock
  down access via your OS user perms.
- **Admin `?pod=` override.** The queue client supports it via
  `QueueClient.join(pod_id=...)` but no UI surface yet. Add an
  `Adminpod` Str par if needed.
- **Internal `audiodeviceoutCHOP`** — a Session-page device picker that
  embeds a TD-native `Audio Device Out` inside the COMP. Currently
  unnecessary since SpeakerOut handles playback; useful for users who want
  their audio device choice picked in TD instead of OS Sound settings.
- **Custom C++ CHOP** — the "drop one op and everything works for any TD
  audio chain" solution. Significant build complexity; revisit if adoption
  justifies it.
- **Visual curve editor** — curves accept raw JSON in v1.
- **MIDI/OSC mapping helpers** — route via standard TD MIDI In + CHOP Export.
- **Multi-session orchestration from a single COMP** — one COMP = one session.

## License

TBD — follows the rest of the DEMON ecosystem.

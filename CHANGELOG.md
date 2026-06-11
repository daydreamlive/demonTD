# Changelog

All notable changes to demonTD. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.2.17] — 2026-06-11

### Fixed: client-side disconnects + audio bleed-through

The big stability pass. All client-side; the webapp and rtmg-vst stream
the same pods, so these were demonTD's own bugs (diagnosed by diffing
against the VST on `origin/master`).

- **No more `1011 keepalive ping timeout` ~40s into a session.** demonTD
  streamed the `params` keepalive from `_connected`, flooding the pod
  *during* its 30-40s synchronous VAE encode (before `ready`) and wedging
  its keepalive. Now silent until `ready` (`_build_params_message`
  gated on `_saw_ready`), exactly like the VST. The WS stays alive
  pre-`ready` via the recv loop auto-ponging the pod's server pings.
- **Surface the server's WS close code + reason** (was always
  `code=None`) — parses the close frame, so a pod's `1011`/reason is
  finally visible. This is what made the keepalive diagnosis possible.
- **Fragment large WS sends** (the ~46 MB source upload) so keepalive
  pings are answered mid-upload; **coalesce the params slot** (newest
  wins) so a slow-draining pod can't build a backlog that head-of-line-
  blocks the pong; **write-readiness gate** so the recv thread never
  wedges in a blocking send.
- **Smooth (de-jitter) the reported playhead.** `playback_pos` was the
  raw `ring.position`, which steps in ~85 ms audio-callback jumps; the
  pod placed slice leading edges against that stale stair-step, leaving
  un-patched "source flash" gaps. `LoopBuffer.playhead_estimate()` ramps
  it between callbacks, **mean-neutral** (zero added latency).
- **Debounce LoRA-strength changes** (trailing-edge, 300 ms quiet —
  matches the VST). A strength delta forces a pod weight refit that
  blocks a decode tick; streaming every fader value during a drag /
  MIDI / automation gesture was a refit storm that stalled the decode
  frontier (the source-flash, worse in TD). Now one refit per gesture.

### Changed: defaults + UI match the rtmg-vst

- Prompt A `acoustic deep house hybrid`, Prompt B `daft punk style,
  beautiful, four to the floor, angelic`; Strength 0.9, Structure 0.8,
  Timbre 0.3, Seed 0; auto-enable LoRAs `ambient-v1` + `deep_house-v1`
  @ 0.8 on first catalog load (your saved toggles still win).
- Quieter textport: handshake/lifecycle play-by-play moved behind Debug
  Logging; signed session token no longer printed in connect/status.

### Contract-based protocol parity (replaces the regex drift checker)

### Contract-based protocol parity (replaces the regex drift checker)

The DEMON backend is self-describing (`protocol.py::wire_contract()`,
served live at `GET /api/protocol`). demonTD now vendors that registry —
plus the web UI's parity data (canonical labels, `config.json` starting
values, the `loraTriggers.ts` blob SHA) — into
`vendor/demon_contract.json` and tests itself against it. demon-public-demo
is out of the loop entirely.

- **`scripts/sync_contract.py`** — extracts the contract from
  `DEMON@origin/main` via `git show` (never the working tree). Candidate
  paths + hard failures everywhere: if DEMON restructures, the sync
  fails loudly by name instead of going silently blind (the failure
  mode that bit the old checker in the 2026-06 SDK refactor).
- **`tests/test_contract.py`** — the drift checker is now the test
  suite. Coverage vs the old `check_protocol_drift.py` categories:
  server msg types → `test_event_dispatch_parity`; client msg types
  (and the hand-kept `encoder_to_wire` table) → encoder tests, both
  directions, no table; slice flags/constants → `test_slice_constants`;
  SessionConfig fields → field-parity test on the real builder;
  ui_coverage / label_parity / lora_trigger_injection / tags_b_plumbing
  → ported. **Net-new coverage the old checker never had:** per-field
  payload schema validation, enum option sets, knob ranges/types,
  default values vs the web installation, reverse-direction checks
  (new knobs/events flag the day they land upstream), whitelist
  hygiene (an intentional-gap entry that goes stale or gets implemented
  fails the suite).
- **Runtime drift check** — at connect, the op GETs the pod's
  `/api/protocol` + `/api/knobs` off the main thread and surfaces
  vocabulary drift as a one-shot ⚠ Status warning. A stale .tox now
  says it's stale instead of mysteriously misbehaving.
- **Nightly auto-PR** — `.github/workflows/contract-sync.yml` re-syncs
  the artifact from DEMON@main; on drift, a Claude agent drafts the
  parity fix and one rolling PR (`contract-sync/auto`) carries the
  upstream compare, sync summary, and test results. Degrades to a
  detection-only PR if the agent can't finish. Plain pytest CI added
  (`tests.yml`). The old `protocol-drift.yml` + 947-line regex checker
  are deleted; the `DEMON_DEMO_PAT` secret and the demon-public-demo
  sibling checkout are no longer needed.

### Fixed (real drift the new tests caught on day one)

- `Vaewindow` default 6.0 → **0.36** (web installation default — the
  v0.2.15-era default regression, now impossible to reintroduce).
- `Shift` range [0,1] default 0.5 → **[1,6] default 3.5**: the server
  clamps into the registry band, so the entire old range was dead
  travel pinned at 1.0.
- `Hintstrength` (Structure) 1.4/[0,2] → **1.0/[0,1]**: values above
  1.0 were server-clamped; the slider's top half did nothing.
- `Guidancescale` default 7.0 → **2.5** (web starting value), min 0 →
  **1.0** (registry floor).
- `Denoise` (Strength) default 0.85 → **0.7** (web starting value).
- `Rcfgmode` menu gained **`full`**; default `off` → **`initialize`**
  (web starting value).
- `Odenoise` param **removed** — the server deleted the scalar
  `ode_noise` knob from its registry; the slider was dead on the wire.
- New **`command_failed`** handler: the server's loud-failure event
  (e.g. LoRA commands on a lora-disabled session) now logs the rejected
  command + reason and shows in Status instead of the unknown-kind log.

### Changed

- `_on_text` dispatches through `events.EVENT_HANDLERS` (src/events.py);
  SessionConfig assembly extracted to pure `src/session_config.py` with
  defaults sourced from `params.py`. Both are now contract-testable
  outside TD.
- `scripts/canary.py`'s `KNOWN_SERVER_KINDS` is derived from
  `events.EVENT_HANDLERS` + the whitelist instead of hand-copied.
- `scripts/release.sh` gates on contract freshness
  (`sync_contract.py --check`) + pytest; requires `~/git/DEMON`
  (override `DEMON_REPO`) instead of demon-public-demo.

## [0.2.16] — 2026-06-09

### Big one: "occasionally choppy" audio — root causes found and fixed

Everything except the PortAudio callback ran on TD's main thread, so
any main-thread hitch degraded audio in two ways at once: the
continuous params stream (the WS keepalive AND the server's pacing
signal — `playback_pos` tells the pipeline where the playhead is,
against a 0.25 s lead floor) went silent, and incoming slices stopped
being patched, so the playhead played stale loop content. The biggest
recurring hitch was ours: the hosted-session heartbeat ran a
synchronous HTTPS poll (fresh TCP+TLS handshake, 10 s timeout) on the
main thread **every 5 seconds**.

- **Heartbeat HTTP off the main thread** — new `src/queue_worker.py`.
  The `/api/queue/status` poll + auto-extend run on a worker thread;
  results marshal back as `hb-*` events and all TD par writes / state
  transitions stay on the main thread, logic unchanged. The queue
  `leave()` calls (WS-close path and Disconnect) are fire-and-forget
  threads now too.
- **Dedicated params pacer thread** — new `src/params_pacer.py`,
  ~16 ms cadence, immune to frame hitches. Sends via the enqueue-only
  WS path (single-thread-socket preserved). Also fixes two latent
  bugs: the params snapshot was starved (user edits never reached it),
  and stale snapshot re-sends could stomp curve manual-overrides.
  The main thread supervises (restart-if-dead, teardown on send-fail
  streak) — same belt-and-suspenders philosophy as the old fallbacks.
- **Slices decode + patch on the WS recv thread** — new
  `src/binary_router.py`. ready/swap_ready/stem_assets are sniffed on
  the recv thread so binary routing state is race-free; the initial
  buffer inits the loop right there; only the SpeakerOut start (TD
  par reads) marshals to the main thread. Old routers are detached on
  reconnect so a lingering recv thread can't touch the new session's
  ring. Per-connection zstd decompressor (the shared one isn't
  concurrency-safe across overlapping recv threads).
- **Audio-callback hygiene** (`src/audio.py`): the underrun branch no
  longer logs from the audio thread (blocking I/O at the worst moment
  — one underrun cascaded into several); underruns surface via an
  always-on main-thread report instead. The oversized-callback
  fallback was broken — it allocated AND raised on a too-small
  scratch, playing the whole block as **silence**; replaced with a
  zero-allocation chunked fill that's bit-identical at any block size.
- **Smoothness telemetry** — new `src/telemetry.py`. A `[health]` line
  (~5 s) reports params-send gaps, slice patch lead vs playhead (late
  patches = the "music glitch" chop), main-thread hitch size,
  heartbeat HTTP duration, and underrun deltas. Quiet unless degraded;
  full line in Debug mode. If chop ever comes back, this names the
  subsystem.

### Bug sweep (three adversarial review passes over the whole codebase)

- **Stale-connection events could kill a fresh session** (HIGH): a
  recv thread outliving `close()`'s 2 s join could enqueue a late
  `close` event that tore down the NEW session. Events now carry a
  connection generation; stale ones are dropped.
- `wire.encode_params` emitted invalid JSON on NaN/Inf params — and
  this message is the keepalive. Non-finite values are dropped
  (never raises on the pacer path), `allow_nan=False` backstop.
- `wire.decode_slice` now validates payload length against the header
  — a truncated frame used to patch short garbage into the loop.
- Failover retried with the api key captured at close time, ignoring
  a key pasted during the backoff; it now prefers the current key.
- `LoopBuffer` read/peek/patch read the channel count outside the
  lock — a racing re-init could silently broadcast mono into stereo.
  Channel count is read under the lock; mismatches emit silence.
- `WSClient.connect()`/`close()` lifecycle race (a connect could
  cancel an in-flight close) — serialized with a small lock.
- Error-handling polish: oauth surfaces malformed-JSON bodies as
  `OAuthError`; HTTP error bodies decode with `errors="replace"`.
- Deleted the write-only `_epoch` counter (TCP FIFO + recv-thread
  routing make slice epochs unnecessary).

Tests: 175 (up from 105) — new suites for the speaker callback, pacer,
heartbeat worker, binary router, telemetry, generation filter, plus
wire/ws/audio additions. CLAUDE.md runtime-gotchas updated (the
keepalive bullet now describes the pacer thread).

## [0.2.15] — 2026-06-09

### Big one: LoRA trigger word prepend (fixes "LoRAs feel weaker in TD")

Every LoRA carries a `metadata.primary_trigger_word` — the activation
token it was trained against. For the LoRA's style to actually fire,
that word has to reach the model's text encoder. TD was passing the
user's prompt through verbatim — enabling a LoRA loaded it server-side
but the encoder never saw the activation token, so the LoRA's style
barely affected output. The canonical web client injects the trigger
prefix at WS send time; TD now does the same.

- **New `src/lora_triggers.py`** — pure-Python port of
  `demon-public-demo/vendor/demon-ui/lib/loraTriggers.ts`
  (`build_trigger_prefix` + `strip_leading_triggers` + `inject`).
  Idempotent, dedupes case-insensitively, strips stale prefixes from
  upstream drift.
- `_apply_lora_catalog` captures `metadata.primary_trigger_word` into
  a new `trigger_word` column on the `lora_catalog` Table DAT and an
  in-memory dict.
- `SendPrompt` prepends the prefix to both `tags` AND `tags_b`,
  stripping any stale leading triggers first.
- **New `Auto-Prepend LoRA Triggers` toggle** on the Prompt+LoRA page
  (default On). Off = manual workflow where you include trigger words
  in the prompt yourself.
- **`enable_lora` / `disable_lora` now also re-push the prompt** so the
  encoder picks up the new trigger prefix immediately — without this,
  toggling a LoRA loaded it server-side but the encoder kept running
  the stale prompt and the user had to touch a strength slider or
  pulse Sendprompt to make the LoRA audibly fire.

### Tags B / Prompt blending now actually blends

`Promptblend` was streaming a value to the server but blending nothing
because the second prompt was empty mid-session. `encode_prompt`
accepted `tags_b` but `SendPrompt` never passed it, and there was no
runtime `Promptb` param — only the hidden Init-page `Initpromptb`,
sent once in SessionConfig and never editable while connected.

- **New `Promptb` Str param** on the Prompt+LoRA page next to `Prompt`.
  Edits are picked up on the next Send Prompt.
- `SendPrompt` reads `Promptb` and passes as `tags_b` to
  `encode_prompt`. LoRA prefix above injects into both.
- `_build_session_config` now sources `prompt_b` from the live
  `Promptb` — one source of truth, editable any time. Deleted
  `Initpromptb`.

### Synthesis page: "Structure" (the canonical's label)

`hint_strength` is the model's structure control — it governs how
closely the model follows the source's section / rhythm / dynamics.
The canonical web client labels it **"Structure"**; TD was labeling
it "Hint Strength" with the help text "Reference latent hint
strength." A user looking for a Structure knob next to Timbre Strength
couldn't find one. Wire key unchanged; only the user-facing label and
tooltip updated to match the canonical's `DISPLAY_NAMES` table.

Also caught + corrected by the new drift checker's label-parity check:
- `Denoise` → **"Strength"** (canonical: users perceive this knob as
  "how strong is the remix", not as a diffusion-process internal).
- `DCW Scaler` → **"DCW low"** (band-pair naming).
- `DCW High Scaler` → **"DCW high"**.

### Drift checker hardening — would have caught all of the above

The original drift checker only inspected protocol message types,
SessionConfig field names, and SLICE constants. Four new checks:

- **`ui_coverage`** — every `DISCRETE_PULSE_TO_KIND` pulse must have a
  non-hidden Param in `params.py`. Catches "protocol exists but the UI
  can't fire it."
- **`label_parity`** — TD `Param` labels must contain the canonical's
  `DISPLAY_NAMES` word for shared wire keys. Catches Hint-Strength-vs-
  Structure and similar.
- **`lora_trigger_injection`** — verifies the three integration points
  (module exists, `_apply_lora_catalog` captures `primary_trigger_word`,
  `SendPrompt` calls `inject`).
- **`tags_b_plumbing`** — if SessionConfig sends `prompt_b`, an
  `encode_prompt(...)` call site must pass `tags_b=`.

Each verified by regression-testing (temporarily break → check fires).

### Fix: UI lag from `_apply_lora_catalog` thrashing

First cut of the LoRA trigger work folded `trigger_word` into the
catalog signature used to skip redundant Table DAT + dynamic-par
rebuilds. The server echoes the catalog with metadata on some events
and without it on others; sig flipped every echo, forcing a full UI
redraw on every `enable_lora`. Manifested as major per-parameter UI
lag. Sig is now id-only (matches original behavior); trigger dict
updates idempotently outside the sig check.

### Fix: "DAT did not load from … overwrite anyway?" dialog on import

Earlier builds set `dat.par.file = abs_path` on every code DAT in the
exported .tox. When users imported the .tox into a fresh project, TD
evaluated each absolute developer path on first instantiation and
prompted on every miss. Now `par.file = ""` — the embedded `dat.text`
is the only source; nothing for TD to fail loading on.

Dev hot-reload workflow (per session, no longer baked in): in TD,
re-point the DAT's file picker at your local `src/<file>.py` and flip
`loadonstart` to True on that instance. Don't commit.

### Known cosmetic mismatch

The shipped .tox carries `BUILD_MARKER = v0.2.15-...-lora-resend` (the
boot textport string) but `DEMON_TD_VERSION = 0.2.14` (the User-Agent
sent on cloud REST calls). TD's bidirectional DAT sync wrote the old
`version.py` back to disk between the version bump and the rebuild —
the underlying root cause (`syncfile=True` baked into the previous
build's DATs) is fixed in this release for everyone running v0.2.16+,
but this specific .tox is mid-upgrade. Functionally identical to
v0.2.15 in every other respect.

### Stats
105 unit tests pass (24 new). Drift checker clean against `origin/main`.

## [0.2.14] — 2026-06-05

### Fix: Seed is an integer, not a 0–1 slider

The `Seed` param was a `Float` clamped to `0.0–1.0` — which can't express a
real generation seed (every value rounded to ~the same seed server-side).
The reference web client uses an **arbitrary uint32** (`config.json`
default 42) with a dice button to randomize.

- `Seed` is now an **Int** (default 42, range `0 … 2147483647` — capped at
  int32 max to stay safely inside TD's numeric-par range; ~2.1 billion
  seeds). Still streamed continuously in the params message, now as an
  integer.
- New **Randomize Seed** pulse on the Synthesis page sets Seed to a random
  integer (the web client's dice), since typing a 10-digit seed by hand is
  no fun.

BUILD_MARKER → v0.2.14-seed-int. 81 tests pass.

## [0.2.13] — 2026-06-04

### Audio output device picker (fixes "connected but no audio")

Reports on **both macOS and Windows** of a session connecting but playing no
sound. Cause: the operator always opened the **system default** output device
(`Pa_OpenDefaultStream`) — whatever device PortAudio/TD happened to be on —
with no way to choose another. When that default wasn't the device the user
was listening on, silence.

- **New `Audio Output Device` menu** on the Session page + a **Refresh Audio
  Devices** pulse. Refresh enumerates output-capable PortAudio devices (name
  + host API: Core Audio / MME / WASAPI / …); pick one and Connect.
- **Live switching:** changing the menu while connected restarts the Python
  audio stream on the new device immediately (no reconnect).
- `SpeakerOut` opens an explicitly-chosen device via `PaStreamParameters`
  (reusing the proven rate/buffer/format fallback matrix), skipping the
  default-device fast path. `-1` = system default (unchanged behavior).
- Enumeration brackets a balanced `Pa_Initialize`/`Pa_Terminate` so it leaves
  PortAudio's refcount untouched — avoids the macOS eager-probe poisoning
  that motivated the lazy-probe design.
- New `list_output_devices()` + pure `format_output_device_menu()` (unit
  tested); 81 tests pass.

### Audio: removed the fabricated "Preferences → Audio" fix; device diagnostics

A Windows tester hit **connected but no audio**, and our textport + Status
field told them to *"Edit → Preferences → Audio → Audio Device → None"* — a
**setting that does not exist** in TouchDesigner on any platform. Removed
that fabricated instruction everywhere it appeared (the `[speaker_out]`
failure message in `audio.py`, the Connect-failure Status line in
`demon_ext.py`, and three spots in the README including the whole
"global Audio Device preference" explanation, which was fiction).

- **New diagnostic:** on every successful open, `speaker_out` now logs the
  device it actually opened — `[speaker_out] output device: dev=N name=...
  hostApi='Windows WASAPI' maxOut=2 defaultSampleRate=48000`. A "connected
  but silent" session is almost always the wrong default device / host API
  (esp. MME vs WASAPI on Windows); this makes it visible instead of a guess.
- Failure/Status messaging now leads with the actual first fix (**save +
  fully restart TD** — the device selection can land in a bad state a clean
  restart clears), then accurate device-ownership guidance, cross-platform.
- README "Audio output troubleshooting" rewritten around restart-first +
  the new device log; macOS `-10851` kept as a platform-specific note.

> Note: this does not yet change the device-*selection* logic (the likely
> Windows root cause). It removes the misinformation and surfaces the data
> needed to pin the real fix.


## [0.2.12] — 2026-06-03

### Fix client-side disconnects: WS socket is now single-threaded

Report: persistent disconnects with **no errors on the pod** — i.e. the
client was tearing the connection down, not the server.

Root cause: **concurrent read+write on the same `ssl.SSLSocket` from two
threads.** Sends (`send_text`/`send_binary`, called from the main thread
via the param stream / `OnParChange`) did `SSL_write` while the recv
thread did `SSL_read` — with no lock between them (`_send_lock` only
serialized send-vs-send). Python's `SSLSocket` is **not safe for
simultaneous read+write**; OpenSSL's record layer corrupts, surfacing as
`SSL: BAD_LENGTH` and an abrupt client-side close the pod never logs.
v0.2.11's params-every-tick keepalive (~30–60 sends/s) made the collision
window far more likely — which is why disconnects got *worse* after that.

Fix — `ws_client.py` now touches the socket from **exactly one thread**:
- `send_text`/`send_binary` only **enqueue** onto a bounded outbound
  queue (any thread). The recv thread drains + sends it between recvs, so
  `SSL_read` and `SSL_write` never overlap. Bounded queue drops oldest on
  overflow (params are idempotent) so a stalled socket can't OOM/block.
- `close()` no longer touches the socket either — it signals the recv
  thread, which performs the actual `ws.close()` in its finally block.
- **Removed the 25 s app-level WS ping** (it was another cross-thread
  write). Keepalive is now purely the param stream, exactly like the
  browser web client.
- Rich close diagnostics: `closed (... ) — uptime=Xs sent=N recv=N
  dropped=N since_last_recv=Xs queued=N`, so a "no pod error" disconnect
  now says whether WE closed and how alive the link was.

New `tests/test_ws_client.py` (6 tests) covers the enqueue/flush/overflow
/failure logic. 78 tests pass total.

BUILD_MARKER → v0.2.12-single-thread-ws-socket; UA → DaydreamDEMON-TD/0.2.12.

### Verification
- Hosted session stays connected; disconnects (if any) now print the
  diagnostic line with `sent`/`recv`/`uptime` so the cause is unambiguous.
- No more `SSL: BAD_LENGTH` floods.

### Protocol parity with the latest backend (DEMON sync 1c07327)

Closed the drift the checker surfaced against `origin/main` (it had been
hidden by a stale local reference — fixed separately):

- **3 new SessionConfig fields** — `lead_floor_s` (0.25), `lead_ceiling_s`
  (1.35), `lead_release_tau_s` (1.5). Server-side decode-buffer lookahead
  tuning (latency vs robustness to GPU contention). Exposed as Init-page
  `Lead *` params and sent in the config at Connect, matching the web
  client's defaults.
- **`set_interp_method`** — per-path blend interpolation method
  (slerp/linear) for prompt / timbre / structure / feedback. Four new
  `Blend Interpolation` menus on the Prompt+LoRA page (default slerp =
  server default); applied immediately on change and re-pushed on every
  `ready`, mirroring the web client's `useInterpSync`.

`scripts/check_protocol_drift.py` now reports **no drift** against
`origin/main`. New `encode_set_interp_method` + test; 79 tests pass.

## [0.2.11] — 2026-06-02

**The big one: sessions died right after `ready` with zero generation
slices.** In-the-wild report: "it's audio reactive but I can't access
the Daydream side… looks like it tries three times and then stops."

### Root cause (found by comparing against demon-public-demo)

The session connected, uploaded the source, received `ready` + the
initial buffer + `stem_assets` — then the pod closed the WS before
streaming a single generation slice (`binary_frames_recv` never exceeded
1, across every pod). Looked server-side; it wasn't.

The web client's `useParamSync` sends a `{type:"params"}` message **every
8 ms after `ready`**, and that continuous stream is the **only** thing
keeping the pod's WS alive — there is no separate keepalive. If the pod
receives nothing after `ready`, it idle-times-out and closes.

demonTD's param flush lives in `OnTick`, driven by the `tick8ms` Timer
CHOP — which has been **silent in practice** (same TD callback gremlin
that kept `OnHeartbeat` silent; the v0.2.6 `onTimerPulse` rename did not
fix it). So after `ready`, demonTD went **completely silent** → the pod
idle-timed-out → dropped → no slices. Heartbeat survived only because
v0.2.6 added a frame_exec fallback for it; `OnTick` had no such fallback.

### Fixes

1. **`MaybeTickFromFrame` — drive `OnTick` from `frame_exec`** (the
   reliable, verified-firing hook) on a ~33 ms floor when the Timer CHOP
   is silent. No-op if the Timer CHOP is actually feeding. This restores
   the post-`ready` param stream that keeps the session alive.
2. **`OnTick` now sends params every tick, not just on change.** It
   accumulates changes into a running `_params_snapshot` and re-sends it
   (with an advancing `playback_pos`) every tick after `ready` — matching
   the web client. The old send-only-on-dirty behavior went silent the
   moment the user stopped touching params, re-triggering the idle
   timeout even when the timer worked.
3. **Send-failure teardown.** After 3 consecutive WS send failures the
   connection is declared dead and torn down once. Previously a corrupted
   SSL stream (from a timed-out binary write) caused every per-frame send
   to fail with `SSL: BAD_LENGTH` and retry **forever** — ~1,500 errors
   in the user's log, pegging CPU and blocking failover.
4. **Heartbeat tolerates `status=unknown`.** A transient / unparseable
   status poll no longer disconnects a live session — the authoritative
   "ended" signal is the WS closing, not a status poll. (One bad poll was
   killing healthy sessions.)

### Not changed (deliberately)
- Source upload stays float32 (int16 halving deferred — the keepalive
  fix addresses the actual disconnect; the 46 MB timeout was a secondary
  effect on a slow link).

BUILD_MARKER bumped to v0.2.11-params-keepalive; UA → DaydreamDEMON-TD/0.2.11.

### Verification
- After `ready`, `binary_frames_recv` climbs past 1 (generation slices
  now stream) and the session stays up for the full lease.
- Debug ON: `[tick] Timer CHOP appears silent — driving OnTick from
  frame_exec fallback` appears once; params flow continuously after.
- A dropped connection tears down cleanly (one teardown line) instead of
  flooding `SSL: BAD_LENGTH`.

## [0.2.10] — 2026-06-02

### Silence the harmless `'td.Ext' object has no attribute 'DemonExt'` console error on tox drop

When the .tox is first dropped into a project, TD starts cook chains
*before* it instantiates the COMP's extension object. The `audio_out`
Script CHOP cooks once in that window, the callbacks DAT's `onCook`
calls `parent().ext.DemonExt`, and TD raises `tdAttributeError` to the
textport. One frame later the extension is alive and everything works
normally — the error is purely cosmetic — but it looks alarming and got
flagged by a user.

`frame_exec` already guarded against this exact race ([build/build_tox.py:697](build/build_tox.py:697));
this just applies the same `except AttributeError: return` pattern to
the callbacks DAT's `onCook`. No behavior change beyond the suppressed
first-frame traceback.

## [0.2.9] — 2026-06-02

### Send a `DaydreamDEMON-TD` User-Agent on cloud REST calls

Mirrors [rtmg-vst#7](https://github.com/daydreamlive/rtmg-vst/pull/7).
Every cloud REST call now sends `User-Agent: DaydreamDEMON-TD/<ver>`
(e.g. `DaydreamDEMON-TD/0.2.9`). This lets the cloud orchestrator
(demon-public-demo) tag each session by client — VST vs web vs
TouchDesigner — from the User-Agent, replacing the brittle "no Origin
header" heuristic. Convention shared across clients:
`DaydreamDEMON-<CLIENT>/<ver>` (the VST sends `DaydreamDEMON-VST/b<build>`).

- New `src/version.py` — single source of truth for `DEMON_TD_VERSION`
  and the derived `USER_AGENT`. Pure module (no TD / third-party deps),
  importable by the HTTP modules in both the TD runtime (via `mod()`)
  and unit tests (via `import`), with a literal fallback so a missing
  module can never break a request.
- `queue_client.py`: User-Agent added to `_headers()` — rides every
  `/api/queue/{join,status,claim,extend,leave}` call.
- `oauth.py`: User-Agent added to the `/users/profile` validation call.
- Tests assert the header is present on both clients.

Scope matches the VST: REST calls only. The WebSocket connects to a
pre-signed URL and the session is tagged at queue/join time, so the UA
on the REST layer is sufficient.

BUILD_MARKER bumped to v0.2.9-user-agent.

## [0.2.8] — 2026-06-02

Two changes: hide the confusing Source Audio File picker, and a
**candidate** fix for Audio Analyze (verify before trusting — see below).

### 1. Source Audio File picker hidden from the op UI

Users expected the `Source Audio File` picker to be the audio input, but
the real source is the CHOP wired into the COMP's input — the picker was
dead UI. TD can't programmatically hide a custom par (`Par.hidden` is
read-only), so the build now simply **doesn't create it**.

New `ui_hidden` flag on the `Param` schema: the entry stays in
`params.py` (so `_read_par("Sourcefile")` and `_has_source_audio()`
resolve exactly as before — returning its `""` default), but the build
skips rendering it. Connection behavior is byte-identical: the build
already reset `Sourcefile` to `""` on every rebuild, so the pre-flight
was already passing via `_wired_chop_file_path()` detecting the wired
Audio File In — not via this par. Removing it from view changes nothing
at runtime.

### 2. Audio Analyze cook-rate — force-cook + Wave CHOP carrier (NEEDS VERIFICATION)

User report: "audio analyze chop isn't getting anything." Diagnosed in
two stages from live logs:

Stage 1 — why it's silent. Python Audio Out plays the generated audio by
reading the LoopBuffer directly via PortAudio, so nothing in TD pulls
`audio_out`. And even with an Audio Analyze CHOP wired to the COMP's
`out`, telemetry showed `audio_out_cooks=0` — the downstream pull does
NOT propagate across the Base COMP boundary to `audio_out`. So
`audio_out` wasn't cooking at all, at any rate.

Stage 2 — the candidate fix, two pieces:
1. Force-cook `audio_out` every frame from `frame_exec`'s `onFrameStart`
   (`audio_out.cook(force=True)`). Guarantees it cooks despite the broken
   cross-COMP pull.
2. `audio_clock` Wave CHOP carrier. The earlier (reverted) force-cook
   cooked at FRAME rate (numSamples=1) because `audio_out` had no audio-
   rate input. Now its input is a Wave CHOP (Time Slice, 48 kHz), so each
   forced cook spans one frame of AUDIO samples (~800 at 48k/60fps).
   `OnCookRecv` reads the carrier's `numSamples` (input 0) as the
   authoritative block size, ignores its values, and fills that many
   samples from the LoopBuffer — an audio-rate stream for Analyze.

Independent of `audio_in`, so the source snapshot + Connect path are
untouched.

Explicitly a hypothesis to verify, not a confirmed fix. Debug-gated
diagnostic in `OnCookRecv` logs every ~600 cooks:
`out.numSamples=… carrier.numSamples=… → AUDIO-rate / FRAME-rate`.

BUILD_MARKER bumped to v0.2.8-hide-sourcefile-forcecook-wave-clock.

### Verification

- Source Audio File picker is gone from the Session page; Connect still
  works exactly as v0.2.7.
- Build log: `audio_out inputs=1 (waveCHOP audio-rate carrier)`.
- Telemetry `audio_out_cooks=` is now > 0 (force-cook working).
- With Debug ON, read `OnCookRecv: cook #600 out.numSamples=…
  carrier.numSamples=… → AUDIO-rate / FRAME-rate`. If AUDIO-rate, the
  wired Audio Analyze CHOP should show moving loudness/spectrum.

## [0.2.7] — 2026-06-01

**Pause fix only.** Reapplied incrementally on top of the confirmed-
working v0.2.6, after an earlier bundled v0.2.7 attempt regressed pod
connection. This increment is deliberately scoped to ONE change that
is orthogonal to source resolution and the WS path, so it cannot
affect whether Connect succeeds. (The Source Audio File par removal and
the Audio Analyze audio-rate work are deferred to later increments,
each verified on its own.)

### TD timeline pause now actually pauses audio

User report: "It doesn't seem to pause if you pause your touch
designer." The real bug: the Execute DAT (`frame_exec`) has a separate
toggle par per callback, all OFF by default. Defining
`onPlayStateChange(state)` in the DAT text without enabling the
`playstatechange` toggle is a silent no-op — so the handler never fired.

Fix:
- `build/build_tox.py`: enable the `playstatechange` toggle on
  `frame_exec` (alongside the existing `framestart` + `active`), and
  dispatch `onPlayStateChange(state)` → `DemonExt.OnPlayStateChange`.
- `src/demon_ext.py`: new `OnPlayStateChange(state)` sets a `_paused`
  flag on SpeakerOut.
- `src/audio.py`: `set_paused()` + `_paused` + a pause fast-path in
  `_pa_callback` — when paused it emits silence and does NOT advance
  the LoopBuffer playhead, so un-pause resumes from the same sample.
  One bool read per PortAudio callback, GIL-atomic, no lock.

The WS + queue heartbeats keep running through the pause (a "stop
hearing audio" gesture, not a teardown — that's Disconnect).

BUILD_MARKER bumped to v0.2.7-pause-only.

### Verification

- Connect STILL works exactly as v0.2.6 (this change touches no
  source/WS code).
- Hit Space / timeline pause during a session: audio cuts within one
  PortAudio callback (~85 ms); un-pause resumes from the same sample.
  With Debug ON: `OnPlayStateChange: state=False → PAUSE`.

## [0.2.6] — 2026-06-01

**Two fixes in one rev.**

### Part 1 — Heartbeat has never fired. Fix the timer callback name.

Follow-up to v0.2.5: user's in-the-wild Windows log (Vinyl Lemonade.mp3,
66.96 s — well under the new 120 s cap) still showed sessions dying
~30-90 s after `ready`. Slices were flowing, prompts and LoRAs were
working, then the WS closed with the server-initiated `closed:
Connection to remote host was lost`. Failover hit the same wall.

### Root cause

`build/build_tox.py` `CALLBACKS_PY` defined `onTimer(timerOp, segment)`
for the two Timer CHOPs (`tick8ms`, `heartbeat`) to dispatch from. But
**`onTimer` is not a TouchDesigner Timer CHOP callback name.** The
real names are `onTimerStart` / `onTimerPulse` / `onTimerCycle` /
`onTimerSegment` / `onTimerComplete`. TD silently ignored our hook, so
both Timer CHOPs have been generating cycles into the void since at
least v0.2.0 — `OnTick` and `OnHeartbeat` have **never fired** in any
released build.

Consequences this masked:

1. **No `/api/queue/status` heartbeat.** Per
   `demon-public-demo/hooks/useQueue.ts:79-82`, polling status IS the
   heartbeat. The server evicts sessions whose `lastHeartbeat` is older
   than `QUEUE_HEARTBEAT_TIMEOUT_MS` (30 s default). Without our
   5 s heartbeat, eviction fires shortly after the last activity that
   incidentally pinged the server (slice traffic kept things alive ~60-
   90 s; idle sessions died at the 30 s mark).
2. **`OnTick`'s params batch flush never ran.** Continuous-param slider
   moves (Denoise, Hint Strength, Feedback, Shift, channel groups,
   RCFG / DCW) silently dropped. User didn't notice because prompt
   and LoRA toggles ride on dedicated WS messages (`encode_prompt`,
   `encode_enable_lora`) fired directly from `OnParChange`, not the
   `_dirty` batch.
3. **Scheduled curves never sampled** (v0.2.2's curve work is gated on
   `OnTick`).
4. **Telemetry never logged** — `[speaker_out] cb_latency` and
   `[coverage]` from v0.2.4 would have surfaced this sooner.

The reason this wasn't obvious: `_drain_inbound`'s `telemetry:` line
runs from `onFrameStart` (correct TD callback name, verified firing),
so the textport always had activity, hiding the silent Timer CHOPs.

### Fix

- `build/build_tox.py`: rename `def onTimer(timerOp, segment)` to
  **`def onTimerPulse(timerOp, segment)`** — canonical "every cycle"
  hook for `cycle=True` Timer CHOPs. Body unchanged.
- `src/demon_ext.py` `OnHeartbeat`: Debug-gated "still alive" log
  every 30 s (`[heartbeat] #N ok status=active expires_in=Xs`). Without
  this, a future regression of the same shape would be silent again.
- `src/demon_ext.py` new `MaybeHeartbeatFromFrame` method, called from
  `onFrameStart`: belt-and-suspenders fallback driver. No-op when the
  Timer CHOP is firing normally; takes over if it ever stops (cheap to
  carry, much cheaper than the next "why are sessions dying" debug
  cycle).

### Part 2 — Loop-wrap source flash on first playthrough.

User report: "I am still hearing a very short cutover to the source
track right when the loop turns over." Root cause: `LoopBuffer.init()`
set `_position = 0`, so the very first iteration of playback ran
through the raw head region (frames 0..seam_frames) before any wrap-
crossfade had a chance to fold those frames over the tail. DEMON's
initial encode often produces a weak/source-leaky audio at the very
start of the loop — that's what the user heard, briefly, at each
loop turn-over.

(Algorithmically the wrap-crossfade already mixes the head into the
tail every wrap — but on iteration 1 the head plays raw FIRST, then
gets folded into iteration 1's tail, then iteration 2 wraps to
`_seam_frames` and the head is never reached again. So the source
flash only ever happened on the first wrap, not on every wrap. But
"first wrap" is when the user hears it on every fresh connect.)

Fix: `init()` now seeks `_position` to `min(seam_frames, frames // 4)`
on entry, mirroring the same clamp `read_into` uses for short
buffers. The head region (frames 0..seam) is now ONLY ever audible
through the wrap-crossfade fold — never raw. `swap()` (called for
server `swap_ready` events) inherits the same behavior since it
delegates to `init()`.

Trade-off: technically diverges from `demon-public-demo`'s
AudioWorklet, which does set position=0 on swap. But the web client
runs a separate `fading` crossfade between old and new buffers on
swap (5 s crossfade), which we don't implement — so our wrap behavior
is already different from theirs. The seam-skip is strictly an
improvement for our path.

### Part 3 — Auto-extend lease (opt-out via Session toggle).

New Session-page param `Auto-extend` (Toggle, default ON). When on,
the 5 s heartbeat watches `Expires in (s)` and pre-emptively pulses
`POST /api/queue/extend` once it drops below 60 s. Sessions stay alive
indefinitely without user input until the server's MAX_EXTENSIONS cap
is hit, at which point we back off for the remainder of the session
(no log spam) and the lease ends naturally.

Backoff policy:
- Network failure on extend: 5 s back-off, retry on next heartbeat.
- Server rejected extend (extensions_used didn't increment): assume
  MAX_EXTENSIONS; back off until the end of the session. Reset on
  the next Connect.

Toggle OFF reverts to manual-only extends via the `Still Playing?`
pulse button (the previous v0.2.5 behavior). Useful for:
- Unattended performances where you want a hard time limit
- Matching the web client's explicit-extend UX exactly
- Cost control during testing

Programmatic alternative also still works — any TD op can
`op('demon').par.Stillplaying.pulse()` whenever it wants.

BUILD_MARKER bumped to v0.2.19-auto-extend.

### Verification

- Within 5 s of hosted Connect, textport shows `[heartbeat] #1 ok
  status=active expires_in=600s`.
- Every 30 s after, `[heartbeat] #6 ok ...`, `#12`, etc.
- Within 65 s, `OnTick: timer is running (first tick)` (the canary
  log that should have caught this in v0.2.0).
- Sessions survive the full 10-minute lease without user input
  (Auto-extend ON by default). At ~9 min, textport shows
  `[auto-extend] expires_in=59s < 60s — pulsing extend ...` and the
  session continues. Toggle `Auto-extend` OFF to revert to manual.
- Continuous-param sliders (Denoise etc.) now propagate during
  hosted sessions.
- First loop wrap on a fresh Connect no longer has a brief source-y
  flash — the head-seam region is now only ever heard through the
  wrap crossfade, never raw.

## [0.2.5] — 2026-06-01

**Stop the post-`ready` disconnects.** Windows in-the-wild testing:
every hosted Connect ran through queue → `ready` → initial-buffer →
then the WS closed ("server sent close" / "Connection to remote host
was lost"). Failover retries hit the same wall. Coverage telemetry
never got data because no slices ever arrived.

### Root cause

Source uploads longer than 120 s. The pod's VAE encoder times out on
longer inputs, the WS closes once the encode pass blows its deadline.
The web client's `engine.max_source_duration_s = 120` (in
`demon-public-demo/vendor/demon-ui/lib/config.ts:312`) enforces this
upstream via the `WaveformTrimDialog` so nothing longer than 120 s
ever reaches the engine. demonTD's cap was **240 s** — predating the
web client's tightening — so a 151.6 s file (Amsterdam_44100.mp3 in
the user's test) passed through unchanged and crashed the pod.

### Fix

- `MAX_SOURCE_SECONDS = 240` → **120**.
- New `SAMPLE_POOL_FRAMES = 9600` constant — matches
  `demon-public-demo/vendor/demon-ui/lib/audio/trimAudioBuffer.ts`'s
  `SAMPLE_POOL`. The VAE encode constraint requires source length to
  be a multiple of this; 120 × 48000 = 5_760_000 is already pool-
  aligned but the helper enforces it for any future cap value.
- New `_crop_to_max_duration(pcm)` helper, called from both source
  load paths (`_load_source_wav` and `_snapshot_input_chop`). When
  the source exceeds the cap, the Status par shows
  `"Source cropped to 120s (max for hosted)"` and textport explains
  the cropping with a `"Trim your file manually for a different
  window"` hint.

### What this does NOT change

- Hosted-mode flow is unchanged for sources < 120 s (idempotent path
  short-circuits before the log line).
- No new TD parameter. Cap is hardcoded to match the web client; a
  per-track "Trim Start (s)" knob would be useful for picking
  WHICH 120 s of a long file, but that's deferred to v0.3 polish.
  For now: pre-trim your file in any DAW, or roll with the first
  120 s.

BUILD_MARKER bumped to v0.2.16-source-cap-120.

## [0.2.4] — 2026-05-29

LoRA toggle propagation finally works, and slice-coverage telemetry
lands as a diagnostic for the "random source flashes during playback"
reports.

### LoRA bug

User report: "I changed loras, I'm still getting bach-sounding stuff
even though jazz lora is on". Three concrete defects:

1. **`Loraenable<id>` toggles fired no wire message.** Dynamic pars
   added by `_apply_lora_catalog` aren't in `P.PARAMS`, so
   `OnParChange` hit `if not schema: return` and the user's toggle
   never reached the server.
2. **Hardcoded `DEFAULT_ON = {"bach"}` auto-enable** ran on every
   fresh `_apply_lora_catalog` call (i.e. every Connect), force-
   enabling bach regardless of the user's `Loraenablebach` par.
3. **`Lorastr<id>` strength changes silently dropped** for LoRAs the
   server hadn't loaded (filtered out by v0.2.3's
   `_filter_params_for_wire`), and #1 meant the toggle could never
   actually enable the LoRA in the first place.

### Fix

* **`OnParChange` routes dynamic LoRA pars BEFORE the schema lookup**
  via a `_lora_par_to_id` reverse map (built in `_apply_lora_catalog`).
  Flipping `Loraenable<safe>` on → sends `enable_lora(id, current_strength)`.
  Flipping it off → sends `disable_lora(id)`. Moving `Lorastr<safe>`
  while the LoRA is enabled → drops `lora_str_<id>` into `_dirty` for
  the next params flush.
* **Removed the `DEFAULT_ON = {"bach"}` auto-enable entirely.** The
  user's saved `Loraenable<id>` toggles already flow through
  `SessionConfig.enabled_loras` at handshake time — that's the server's
  source of truth. Forcing a separate `enable_lora` post-catalog was
  just overriding the user's choice. New LoRA toggles default to OFF;
  user opts in per-LoRA.
* Removed the now-dead `_auto_enable_done = False` reset in
  `_flush_pending`.

### Slice-coverage telemetry

Bug #1 from the same report: "once-in-a-while cutovers to original
song during playback", random cadence. Suggests slice arrival jitter
— the server doesn't keep every region of the loop patched at all
times, and the playhead occasionally enters un-patched regions
(which contain the original source audio until a slice replaces
them).

To diagnose, `LoopBuffer` gains a slice-coverage bitmap:

* `_patched_chunks` — one `bool` per ~1 s chunk of the loop. Flipped
  to True the first time a slice patches that chunk. Cheap (< 100 B
  for typical loops), allocation-free in steady state.
* New methods: `mark_patched(start, n)`, `coverage_fraction()`,
  `is_patched_at(frame)`.
* `_on_binary` slice path calls `mark_patched()` after each
  `patch` / `add_delta`.
* `OnTick` (Debug-gated) emits one line per second:
  ```
  [coverage] 87.5% patched (slices_recv=341) playhead@5.3s in_patched=True
  ```

If a flash correlates with `in_patched=False` or persistent < 100%
coverage, we have actionable evidence to either silence un-patched
regions or surface the issue upstream. No behavior change yet — this
commit just instruments.

### Tests

* `tests/test_audio.py`: 2 new tests for the coverage bitmap
  (`test_loop_buffer_coverage_fraction_init_zero`,
  `test_loop_buffer_mark_patched_basic`). 67/67 audio + curve + wire +
  queue tests pass.
* Drift script: exit 0 against `demon-public-demo @ f2be73b`.

BUILD_MARKER bumped to v0.2.15-lora-toggle.

## [0.2.3] — 2026-05-29

**Stop the disconnects.** User report from in-the-wild testing:
demonTD disconnects a lot, especially when messing with prompts and
LoRAs. Hot-path audit against the web client's `useParamSync.ts`
revealed three continuous-param wire keys whose engine handler is
NOT the generic `params` route — sending them inside a `params` raw
dict gets the WS closed by the server.

### Bugs fixed

1. **`prompt_blend` / `timbre_strength` / `lora_blend` were sent in
   `params` raw dict.** The web client explicitly deletes all three
   before calling `sendParams()`:
   - `prompt_blend` rides on `set_prompt_blend` (its own WS message).
   - `timbre_strength` rides on `set_timbre_strength`.
   - `lora_blend` is UI-only — the engine doesn't recognize it
     (web client uses it to fan out into per-LoRA strengths).

   demonTD sent all three on every params flush, which the server
   either rejected outright or closed the WS on. This was the
   "disconnects when messing with prompts / LoRAs" cause.

2. **`lora_str_<id>` was sent for non-enabled LoRAs.** Web client's
   `useParamSync.ts` only includes strengths for LoRAs in
   `lora.enabled` (line 61). demonTD was sending strengths for every
   LoRA with a UI slider, including disabled ones. Server-side
   strength application for unloaded LoRAs is undefined behavior,
   sometimes a close.

3. **`SessionConfig.lora_strengths` included disabled LoRAs.** Same
   mismatch with `useStartSession.ts` `buildConfig()` — that loop
   only emits strengths for enabled LoRAs.

### Changes

* New `_PARAMS_NOT_FOR_WIRE = {prompt_blend, timbre_strength,
  lora_blend}` set + `_filter_params_for_wire(raw)` helper that
  strips those three keys AND drops `lora_str_<id>` for non-enabled
  LoRAs. Applied at every `encode_params` call site (OnTick flush,
  frame_exec flush, `SetParam`, `SetParams`).
* `OnParChange` for the three special params now routes to the
  dedicated WS message instead of `_dirty`. Moving the Prompt Blend
  slider sends `set_prompt_blend` directly; moving Timbre Strength
  sends `set_timbre_strength` directly. `Lorablend` logs a one-shot
  warning that it's UI-only.
* `_seed_dirty_from_current_pars` (runs on `ready`) special-cases
  the same three: sends `set_prompt_blend` / `set_timbre_strength`
  via dedicated messages so the server has the initial values
  immediately, AND skips them from the `_dirty` seed.
* `_lora_strengths()` (used by `_build_session_config`) now filters
  to enabled LoRAs only, matching the web client.

### Tests

Full pytest sweep: 65 passed, 1 skipped (zstd round-trip needs the
zstandard package). No new tests needed — the bugs are wire-level
behavior whose fix is "stop including these keys"; pytest can't
exercise the live server response, only the encoders themselves
(which are unchanged and still tested in `test_wire.py`).

BUILD_MARKER bumped to v0.2.14-params-filter.

## [0.2.2] — 2026-05-29

Two features pulled from the latest `rtmg-vst` PR (commit `b2e1953`):
**pod failover** and **scheduled curves**. Both are pure client-side
— no new wire surface, no new queue endpoints.

### Pod failover

When a hosted WS opens but never reaches the server `ready` handshake
(1011 keepalive, overloaded pod, VAE encode hang behind a Cloudflare
502, etc.), demonTD now releases the dead session and re-queues for
a different pod, up to 3 attempts. Reset on a successful `ready` so
mid-session drops are still treated as terminal disconnects.

- Behavior matches rtmg-vst's `RTMGSession::applyResult` failover
  branch: leave + re-join without pod_id pin.
- 1.5 s backoff between attempts so we don't hammer the queue.
- After 3 failed attempts the Status par reads "Pod failover
  exhausted (3 tries). Try Connect again later or switch to Direct
  mode."
- `_pending_audio` now lives across the WS cycle until the `ready`
  handler clears it, so failover re-sends the source on the next WS
  open without re-resolving PCM (which would be slow on a 24 s WAV).

### Scheduled curves

Replaces the old Curves page wholesale. The previous 5 `*_curve`
JSON-spec params (sde_denoise_curve, ode_noise_curve, x0_target_curve,
velocity_scale_curve, initial_noise_curve) sent keys the server
stopped applying — they were a static whole-buffer schedule the pod
ignored. The web client moved to **client-side per-frame sampling**
and writing the resulting scalar into the regular continuous-param
stream. This commit mirrors that behavior.

- **Schedulable params**: `Denoise`, `Hintstrength`, `Feedback`,
  `Shift` — matches `demon-public-demo/types/curves.ts`'s
  `SCHEDULEABLE_PARAMS`.
- **Master toggle**: `Schedulecurves` (off by default, NOT persisted
  on across sessions for the same reason the web client documents —
  a stale persisted-on schedule silently driving denoise on every
  reload was a footgun).
- **Per-param controls**: `<Name>curveenable` toggle + `<Name>curve`
  multiline Str holding a piecewise-linear JSON spec like
  `{"points": [[0, 0.5], [0.5, 1.0], [1, 0.3]]}`.
- **Sampler in `OnTick`**: t = `(ring.position / ring.frames) % 1.0`,
  evaluate piecewise-linear, map y∈[0,1] to the base param's
  [min, max], write into both `_dirty` (for the next wire flush)
  and the TD par's `.val` (so the user sees the slider move).
- **Manual override window**: 500 ms after the user moves a
  curve-bound slider directly, the curve yields for that param.
  Matches the web client's `isManualOverrideActive`.
- **Cache**: parsed control points are cached by spec STRING so
  editing the JSON invalidates the cache on next tick without
  re-parsing every tick. Capped at 64 entries.
- **`tests/test_curves.py`** — 7 unit tests covering the parser
  (valid input, endpoint clamping, x-sort, invalid → None) and
  evaluator (exact at control points, linear interp between, t
  clamping).

### Deferred to v0.3+

- Per-LoRA strength curves (web client supports `lora_str_<id>` keys).
  Needs dynamic-param plumbing.
- Catmull-Rom interpolation. Linear gets ~90% of the visual range.
- An in-TD curve editor. TD has no native curve-editor primitive;
  JSON spec is the pragmatic v0.2 UI.

BUILD_MARKER bumped to v0.2.13-failover-curves.

## [0.2.12] — 2026-05-29

**Audio stutter fix.** User reported a longstanding intermittent
stutter that comes and goes — survives across versions, doesn't
exist in `demon-public-demo`'s web client. Hot-path audit revealed
the PortAudio callback (`SpeakerOut._pa_callback`) was allocating
~10 numpy arrays per call (~100-200 KB), running at ~12 callbacks/sec
→ ~1-2 MB/sec of allocation churn on the audio thread. CPython's
gen-0 GC fires on whatever thread allocates; a GC pause on the
audio thread of even ~30 ms blows our 85 ms deadline → stutter. The
"resolves itself, then recurs" pattern matches GC quiesce/spike
cycles exactly.

### Changes

* **`LoopBuffer.read_into(out)`** — new method that fills a caller-
  provided buffer instead of allocating. Cached seam-crossfade scratch
  on the buffer instance (`_seam_t_scratch`, `_seam_one_minus_t_scratch`,
  `_seam_blend_scratch`). The existing `read()` stays as a thin
  alloc-and-delegate wrapper for non-audio-thread callers.
* **`SpeakerOut` pre-allocated scratch** — `_scratch_pcm`,
  `_scratch_interleaved_f32`, `_scratch_interleaved_i16`, sized at
  `_max_block_frames = max(frames_per_buffer * 4, 16384)` so a
  surprise PortAudio block size doesn't force a fallback alloc.
* **`_pa_callback` rewritten** to use the scratches + `np.copyto` +
  `out=` keyword args on every numpy op. Zero allocations in steady
  state. int16 path does in-place `np.clip` + `np.multiply` + a
  single `np.copyto` cast (no `astype` temp).
* **Audio-thread latency telemetry** — per-callback elapsed time
  measured via `time.perf_counter_ns()`; mean + max published once
  per second through `SpeakerOut.drain_latency_stats()`. `OnTick`
  reads + logs them under the Debug toggle. Lets the user confirm
  the fix worked (max << 85 ms) or, if not, prove the stutter is
  something else.
* **Always-on underrun log** — dropped the every-50th gate; every
  audio underrun now lands in the textport immediately.
* **`tests/test_audio.py` rewritten** — the obsolete RingBuffer-name
  tests were replaced with LoopBuffer-equivalents (init, read,
  read_into, patch / add_delta, seam crossfade, swap, clear) plus a
  new `test_loop_buffer_read_into_is_allocation_free` that uses
  `tracemalloc` to enforce zero hot-path allocations going forward.

### Measured improvement (local)

Before refactor: 200 calls of `_pa_callback` → ~tens of MB allocated.
After refactor: 200 calls → ~1372 bytes attributed to audio.py,
most of which is `tracemalloc` / lock-context-manager bookkeeping.
Per-callback latency: mean 0.058 ms, max 0.315 ms (PortAudio deadline
at 4096 frames / 48 kHz is ~85 ms — we use ~0.07% of available time).

BUILD_MARKER bumped to v0.2.12-no-audio-alloc.

## [0.2.11] — 2026-05-29

* Removed the **Sign out** pulse from the Session page. The paste-key
  flow + on-disk persistence cover the relevant lifecycle without it.
* README now has a dedicated **Quick start — Hosted mode** section with
  step-by-step API-key + paste-key + Connect instructions, plus a
  one-time setup note in the regular Quick Start about setting TD's
  Audio Device preference to None.
* The Session-page parameter-table entry in the README now reflects
  the v0.2 layout (Mode menu, hosted controls, queue readouts).

## [0.2.10] — 2026-05-29

**Real root cause of the v0.2.x audio failure**, after `scripts/probe_portaudio.py`
confirmed PortAudio + the user's device open fine outside TouchDesigner:

> TouchDesigner holds the default output device's Core Audio AudioUnit
> whenever its **Edit > Preferences > Audio > Audio Device** preference
> points at a real device. Once TD has the AudioUnit bound, Core Audio
> refuses to let our PortAudio thread call
> `AudioUnitSetProperty(kAudioUnitProperty_StreamFormat)` on the same
> device — the result is `kAudioUnitErr_InvalidPropertyValue (-10851)`
> wrapped as PortAudio's `paInternalError`.

The v0.2.9 lazy-probe fix didn't help because the eager probe wasn't
the cause; TD owning the device was.

### What's actually fixed in v0.2.10

* **Failure path now points at the real cause.** The "no usable combo"
  log line and the user-facing Status par both lead with "Set TD's
  Audio Device pref to None (Edit > Prefs > Audio) and re-pulse
  Connect" before the other workarounds. No more telling the user to
  fiddle with Audio MIDI Setup as the first thing to try.
* **README troubleshooting section rewritten** to put the TD-preference
  fix front and center with a concrete walkthrough, plus a pointer at
  `scripts/probe_portaudio.py` for users who want to verify.
* **`scripts/probe_portaudio.py` ships** as the diagnostic users can
  run from a terminal: same Pa_OpenDefaultStream call demonTD's v0.1.5
  made, against the bundled dylib, without TD in the picture.

The previous v0.2.6 / v0.2.8 / v0.2.9 fallback layers stay in place —
they cover edge cases where TD's preference is already None AND the
device still refuses our format (rare but real). The failure log just
explains which case fires first.

BUILD_MARKER bumped to v0.2.10-td-holds-device-msg.

## [0.2.9] — 2026-05-29

**Regression fix.** v0.2.4 added an eager `Pa_GetDefaultOutputDevice` +
`Pa_GetDeviceInfo` probe right before `Pa_OpenDefaultStream` so we
could log device info and feed the sample-rate fallback. PortAudio's
API documents `Pa_GetDeviceInfo` as a getter, but on macOS Sequoia it
triggers a Core Audio device-list refresh that touches the default-
output AudioUnit's stream-format property. After that touch, the
subsequent `AudioUnitSetProperty(kAudioUnitProperty_StreamFormat)` is
rejected with `kAudioUnitErr_InvalidPropertyValue` (-10851) — even
though it's the same call that succeeded in v0.1.5.

The fix is one move, no API changes: the device-info probe is now
**lazy**. `start()` calls `Pa_OpenDefaultStream` immediately (the
v0.1.5 known-good code path); only on failure does it probe device
info and run the v0.2.4–v0.2.8 fallback matrix (alternate rates,
buffer sizes, `Pa_OpenStream`+`PaStreamParameters`, `paInt16`).

For users where v0.1.5 worked: audio comes right back. For users on
genuinely-incompatible devices: same fallback coverage as v0.2.8, just
deferred until needed.

If you saw `[speaker_out] no usable rate / buffer / format / open-mode
combination` in v0.2.6–v0.2.8 logs on a device that previously worked,
v0.2.9 should restore it. If it doesn't, please file an issue with the
new `[speaker_out] direct Pa_OpenDefaultStream ... failed` line —
that's the v0.1.5-equivalent attempt failing for a genuinely different
reason, and we'll need a vendored libportaudio.dylib bump to fix it.

BUILD_MARKER bumped to v0.2.9-no-eager-probe.

## [0.2.8] — 2026-05-29

PortAudio compatibility expansion. User report: on macOS Sequoia with
"External Headphones" as default output, `Pa_OpenDefaultStream` failed
at every rate × buffer-size combination with `Pa internal err=-9986 /
hostErr code=-10851 'Audio Unit: Invalid Property Value'`. Core Audio
was refusing whatever stream format PortAudio's minimal-API path was
trying to set.

### Added

- **Layer 2: `Pa_OpenStream`** with explicit `PaStreamParameters` at
  the device's `defaultHighOutputLatency`. The high-latency hint gives
  PortAudio room to renegotiate the AudioUnit's format, which resolves
  -10851 on many Sequoia devices.
- **Layer 3: paInt16 fallback.** Some macOS Core Audio devices reject
  `paFloat32` even though PortAudio's docs claim auto-conversion. After
  every float32 attempt fails, we retry the whole matrix with `paInt16`
  and convert int16↔float32 inside the audio callback. Headroom drops
  ~3 dB and clipping is now hard at ±1.0, but you get audio out.
- **`Pa_IsFormatSupported` pre-probe** before each `Pa_OpenStream`
  attempt. Cleaner failure messages, and there are mailing-list reports
  that the probe "primes" the AudioUnit and resolves -10851 on some
  devices.
- **README "Audio output troubleshooting" section** with the three
  user-side workarounds (different default device, Audio MIDI Setup
  format, or toggle `Python Audio Out` off + wire your own
  `Audio Device Out CHOP`).

### Changed

- `speaker_out.start()` failure no longer kills the WS session. Status
  shows a clear "Audio output failed — toggle Python Audio Out off
  and wire your own Audio Device Out CHOP, or fix your default device
  and pulse Connect again." The hosted session stays alive (your
  reservation isn't burned) and the user can route audio out via the
  COMP's `out_chop` port instead.
- Logging gains a per-format prefix and the surrounding context for
  each (rate, buffer, open-API) attempt. `Pa_GetLastHostErrorInfo`
  prints the underlying Core Audio OSStatus + text on every failure.

BUILD_MARKER bumped to v0.2.8-pa-openstream-int16.

### Still failing?

If you see the new "no usable combination" message even after the
workarounds in the README, the next escalation is a vendored
PortAudio binary upgrade (the sounddevice-bundled dylib is ~12
months old and predates several Sequoia AudioUnit fixes). Tracking
that as a separate follow-up.

## [0.2.5] — 2026-05-29

Trim hosted-mode sign-in to paste-only. The browser-OAuth flow was
fragile (Web Server DAT rebind quirks, port-binding races, hangs when
the system browser launch failed silently) for a use case that's one
extra click to do manually: open the dashboard, copy the key, paste it
in.

### Removed

- `Sign in via browser` pulse on the Session page
- `SignInBrowser` + `Authenticate` extension methods
- `OnAuthCallback` + `OnHTTPRequest` extension methods
- `_oauth_server`, `_oauth_state`, `_oauth_port` internal state
- `oauth_server` WebServer DAT from the COMP topology (built-in TD op)
- Everything in `src/oauth.py` except `fetch_profile` + `OAuthError`
  (the paste-key validation path still uses these)
- `onHTTPRequest` callback function in the COMP's callbacks DAT

### Changed

- `Paste API Key` pulse now deep-links to
  https://app.daydream.live/dashboard/api-keys instead of the
  dashboard root — one less click for the user.
- `tests/test_oauth.py` rewritten around `fetch_profile`. 3 tests pass
  (down from 6, all of which tested removed surface).

BUILD_MARKER bumped to v0.2.5-paste-only. Note: rebuilding the .tox
removes the `oauth_server` op from the COMP. Old .tox files keep the
op but it's dormant and harmless.

## [0.2.1] — 2026-05-29

Catch-up sync with `demon-public-demo` since the v0.1.5 protocol pass.
The drift script (`scripts/check_protocol_drift.py`) flagged four new
server message types, two new client encoders, and four new
`SessionConfig` fields. v0.1.5's "log once per unknown kind"
defense-in-depth meant the textport stayed quiet, but the actual
handshakes are tightened up here.

### Server messages now recognized

- **`depth_applied`** — server ack of a runtime depth retune
  (`set_depth`). Logged for visibility; no UI surface (depth is
  Init-only in TD).
- **`params_echo`** — MCP-driven param mirror. Logged under Debug only,
  since TD has no MCP integration.
- **`prompt_blend_echo`** — MCP-driven prompt-blend update. Now mirrors
  the value back into the `Promptblend` continuous par so the TD UI
  reflects external control bus changes.
- **`stem_failed`** — surfaced as a visible log line (was hitting the
  unknown-kind dedupe).

### SessionConfig fields now sent

- **`prompt_b`** — secondary prompt for A/B blending. Wired to a new
  `Initpromptb` par on the Init page (default empty).
- **`client_id`** — per-machine identifier. Reuses the queue
  `deviceId` we already generate. Server stashes it into loguru
  contextvars so pod logs can be filtered by demonTD instance.
- **`use_server_fixture: false`** — sent explicitly. The JS client
  capability-probes `/api/server-info` before flipping this to true;
  TD sends false unconditionally to use the unchanged upload path.

### Out of scope (intentional, not drift)

- **`set_depth`** client encoder — runtime depth retune is a UX
  feature, not a protocol gap. Depth stays Init-only.
- **`loop_band`** client encoder — TD's LoopBuffer does its own seam
  crossfade locally; the band isn't a TD parameter.
- **`stem_source_mode`** — only sent when the user uploads a custom
  track and selects a stem mode in the web client. TD has no stems UX
  in v0.2.

The drift script now knows about the intentionally-omitted client
encoders + config field so future runs stay green.

## [0.2.0] — 2026-05-29

**Hosted mode.** The operator can now connect to the Daydream queue at
`music.daydream.live` and play on a managed pod — no more spinning up
your own VAST instance to demo it. Direct mode (your own pod URL) keeps
working unchanged.

### What's new

- **`Mode` menu** on the Session page (`Direct` / `Hosted`). Direct keeps
  the existing `Server URL` flow. Hosted POSTs `/api/queue/join` against
  the Daydream queue, polls until `active`, then connects to the
  server-signed `wss://` URL — same flow the Daydream web app uses, and
  the same protocol as the `rtmg-vst` plugin.
- **Two ways to sign in** (Session page pulses):
  - **Paste API Key** — opens `app.daydream.live` in your browser; paste
    your key into the TD dialog. Validates against `/users/profile`
    before saving.
  - **Sign in via browser** — full OAuth flow. TD spins up a local
    listener on a free port, your browser redirects there with the
    one-time token, the key is fetched and saved.
- **`Sign out`** wipes the stored key (preserves the device ID).
- **Queue status surfacing** while connecting + heartbeat-driven while
  active: `Queue Position`, `Expires in (s)`, `Deny reason` (for paywall
  / over-budget responses).
- **`Still playing?`** pulse hits `/api/queue/extend` to bump the
  session lifetime.
- **`POST /api/queue/claim` after WS open.** Cancels the server-side
  reservation-eviction timer (added in the latest VST PR, now in TD).
- **Stable device ID** persisted to `<prefs>/daydream_auth.json`. Sent
  on every join for analytics + rate-limit attribution.

### Persistence

API key + profile + device ID live in a per-user file, NOT in the .toe:
  - macOS: `~/Library/Application Support/derivative/daydream_auth.json`
  - Windows: `%APPDATA%/Derivative/daydream_auth.json`
  - Linux: `~/.local/share/derivative/daydream_auth.json`

That matches the rtmg-vst PropertiesFile approach and avoids leaking
your API key when you share a .toe.

### What's NOT changed

- Direct-mode flow is byte-for-byte identical to v0.1.5. If you've been
  pointing demonTD at your own pod URL, nothing about that path moves.
- Wire protocol is unchanged. v0.1.5's "log once per unknown kind"
  defense-in-depth stays as-is.

### Reference

Mirrors the queue + auth surface from the new RTMG VST PR
([daydreamlive/rtmg-vst#4](https://github.com/daydreamlive/rtmg-vst/pull/4)).

## [0.1.5] — 2026-05-27

Compatibility update for the current DEMON server build. Reports of
"no generated audio plays, just the source loops, textport is flooded
with error spam" trace to three server-side changes since v0.1.4 shipped.

### What broke

The server now:
1. **Always emits zstd-compressed slices** (flag=0x01). v0.1.4 had a
   try/except around `import zstandard` that silently swallowed errors;
   if TD's bundled Python couldn't load the vendored binary, `_ZSTD_DEC`
   was None and every slice failed `decode_slice` with "no decompressor
   was provided" — no generated audio audible.
2. **Emits a `stem_assets` JSON message** followed by two large
   binary blobs with new flag bits (e.g. 0x07). Server-side stem
   separation feature. We don't handle stems, but were logging "Bad
   slice" for each blob.
3. **Sends slices with future-feature flag bits** beyond {0,1}. Decoded
   as "Bad slice" with the same spam.

### What's fixed

- **`SessionConfig.compression = "none"` fallback.** If our vendored
  `zstandard` fails to load, we ask the server to emit raw float16
  slices instead of zstd-compressed. ~1.5× more bandwidth on the recv
  path, but works without depending on a binary load that the user's
  TD bundle may not support. The actual zstd load failure now logs
  its specific reason at boot.
- **`stem_assets` recognized.** The two binary blobs that follow are
  consumed silently (counter-tracked). No textport spam.
- **Slice flags > 1 silently skipped.** Logged ONCE per unknown flag
  value per session, then quiet. Future server features won't flood
  the textport.
- **`Reconnect to apply Init changes` deduped.** The status string is
  only set when it differs from the current Status value, so touching
  multiple Init pars in rapid succession doesn't produce 14 identical
  status lines.

### Files changed
- `src/demon_ext.py`:
  - zstd load failure now logs reason; SessionConfig compression
    fallback.
  - `_on_text`: `stem_assets` ack; unknown-message dedupe.
  - `_on_binary`: skip stem blobs (announced by `stem_assets`),
    skip unknown-flag slices, dedupe slice-decode errors.
  - `OnParChange`: dedupe Reconnect status set.

BUILD_MARKER → v0.1.5-demon-compat.

## [0.1.4] — 2026-05-20

Two audio-thread improvements landing the playback path at zero
underruns over hundreds of callbacks of testing.

### Vectorized loop-seam read

v0.1.3's seam crossfade used a per-frame Python loop inside
`LoopBuffer.read` — ~2k iterations per audio callback at audio
rate. Each iteration did several numpy ops. Total cost was small
(~2 ms per callback) but Python overhead caught by TD's main-
thread GIL pressure occasionally pushed wrap-spanning callbacks
past their 43 ms deadline → ~5% audible stutter rate.

The read is now split into vectorized runs of (a) bulk copy from
contiguous buffer ranges, (b) numpy-vectorized crossfade over the
tail seam. Crossfade math is identical to the AudioWorklet, just
batched. Per-callback Python overhead dropped from ~2k iterations
to ~3 numpy ops.

### Bigger PortAudio block (4096 frames)

Audio latency floor: ~43 ms (2048 frames) → ~85 ms (4096 frames).
Doubles the audio callback's deadline so wrap-spanning callbacks
have headroom even when TD's main thread holds the GIL for >40 ms.

Verified clean: `[speaker_out] stopped (cb_count=615 underruns=0)`
after ~52 s of normal use with TD activity. Pre-fix the same
session produced occasional audible glitches.

### Files changed
- `src/audio.py` — `LoopBuffer.read` vectorized; `SpeakerOut`
  default `frames_per_buffer` 2048 → 4096.
- `src/demon_ext.py` — bump `BUILD_MARKER` to `4k-buffer-v1`.

BUILD_MARKER → 4k-buffer-v1.

## [0.1.3] — 2026-05-20

Single fix: loop seam crossfade.

### Bug

User reported occasional "flashes" of source audio mixed into the
generated output. This did NOT happen in `demon-public-demo`'s web
client. Hours of speculation about delta math and server-side
behavior were red herrings.

### Root cause

`LoopBuffer.read()` was hard-wrapping the playhead from
`frames - 1` to `0`. The web client's `AudioWorklet` doesn't — it
crossfades the last 50 ms of the loop with the FIRST 50 ms, then
wraps the playhead to `position = seam` (= 2400 frames at 48 kHz)
so those leading frames aren't replayed verbatim.

The DEMON server's slice positions don't start at frame 0 (first
slices land around start_sample = 3840, 107520, 211200…). So the
first ~80 ms of the loop tend to remain unpatched source content
for a long time. Hard-wrapping replayed that source content on
every 24-second loop boundary — exactly the "occasional flash"
cadence.

### Fix

Ported the worklet's seam crossfade into `LoopBuffer.read()`:
- New `seam_seconds=0.05` parameter on `LoopBuffer.__init__`
  (default 50 ms; matches `SEAM_FADE_SECONDS` in
  `demon-public-demo/public/audio-worklet.js`).
- `read()` now does a per-frame loop with two paths: bulk copy in
  the middle of the loop, crossfade math in the tail-seam region.
- On wrap, jumps to `position = seam_frames`, not 0.

Bonus: this also smooths the small audio discontinuity that hard
wraps were producing on every loop boundary, even when the leading
samples weren't audibly source.

### Files changed
- `src/audio.py` — `LoopBuffer.__init__` and `LoopBuffer.read`.
- `src/demon_ext.py` — pass `sample_rate=wire.SAMPLE_RATE` to the
  LoopBuffer constructor. Bump `BUILD_MARKER` to `seam-crossfade-v1`.

BUILD_MARKER → seam-crossfade-v1.

## [0.1.2] — 2026-05-20

Polish + Windows build. No behavior changes for working flows.

### UX
- **Session page decluttered.** The disabled `Hosted Mode (coming soon)`
  header and its eight greyed-out children (`Anonymous`, `Direct Pod`,
  `Authenticate`, `Paste API Key`, `API Key`, `Queue Position`, `Expires In`,
  `Still Playing`) are gone for v0.1.x. The supporting code in
  `demon_ext.py` is unchanged — defaults via `_read_par` fallbacks keep
  the direct-anonymous mode that's always been the only working path.
  Hosted mode reappears in v0.2 when it actually works.
- **Source Audio File pre-flight.** Pulsing Connect without a source
  file (and no wired CHOP) now bails immediately with a clear status
  message AND a TD popup dialog, instead of half-attempting a connect
  and burying the error in textport.
- **Server URL default** is now `ws://localhost:8765/` (DEMON's
  realtime_motion_graph_web port) instead of the bogus
  `http://localhost:1318`.

### Windows build (untested by maintainer)
- Vendored `libportaudio64bit.dll` and `libportaudio64bit-asio.dll` from
  the `sounddevice` Windows wheel into
  `vendor/sounddevice/_sounddevice_data/portaudio-binaries/`.
- Cross-platform path resolution in `demon_ext.py` picks the right
  binary at runtime: `.dylib` on macOS, `.dll` on Windows, `.so` on
  Linux (Linux not vendored — falls through to a system install).
- `SpeakerOut._load_lib` candidate list extended with Windows + Linux
  paths.

### Internal
- Removed `_playback_pos` redundant += updates (already done in v0.1.1,
  reconfirmed here).

## [0.1.1] — 2026-05-20

End-to-end audio playback now works on macOS. v0.1.0 shipped with the wire
protocol, schema, and extension class but the audio output path was broken
(Time Slice doesn't propagate across TD Base COMP boundaries, by design — see
README's "Audio routing" section). This release fixes that and a long list
of paper-cuts.

### Audio output (works now)

- **Python-side audio playback via PortAudio.** A small Python audio thread,
  bound to the bundled `libportaudio.dylib` via stdlib `ctypes`, plays the
  generated audio through the system default device. No TD CHOP audio chain
  crossing; no need to wire an external `Audio Device Out CHOP`.
- **LoopBuffer**, the audio model — replaces the original ring buffer.
  Mirrors `demon-public-demo/vendor/demon-ui/engine/audio/AudioPlayer.ts`:
  the server's initial buffer is the full track loop; subsequent slices
  patch positions at their `start_sample` indices; playback wraps
  continuously while content evolves.
- **`peek()` for visual reactivity.** A new `LoopBuffer.peek()` reads the
  current play position without advancing it. The `audio_out` Script CHOP
  uses this so Analyze CHOPs / FFTs / peak detectors can mirror what's
  playing without racing the audio thread. Previously the Script CHOP's
  `read()` was advancing the play head from frame_exec at 60 Hz while the
  audio thread also called `read()` at audio rate — the two consumers
  raced through the buffer, causing constant chop.
- **Wave-decode model**: `wire.decode_slice` correctly parses the 23-byte
  header, decompresses `SLICE_FLAG_DELTA` payloads via vendored
  `zstandard`, converts float16 → float32, and dispatches to
  `LoopBuffer.patch()` or `add_delta()` based on flag bit.

### Defaults aligned with demon-public-demo

So TD users get the same out-of-the-box sound as web users.

| param | old default | new default | source |
|---|---|---|---|
| `denoise` | 0.7 | 0.85 | manual tuning |
| `vae_window` | 3.0 | 6.0 | `useStartSession.ts buildConfig` |
| `fast_vae` | True | False | same |
| `Initprompt` | "instrumental music" | "heavy dubstep, deathstep, afxdump, growl heavy bass distortion" | same |
| Bach LoRA strength | server-reported (often 0) | **always 1.0 on DEFAULT_ON LoRAs** | server occasionally reports 0 before LoRA loaded |

### Initial-params seed on `ready`

Continuous param values (denoise, hint_strength, all 14 channel gains, DCW
block, etc.) used to only reach the server when the user moved a slider
mid-session. After `ready`, the server ran with its internal defaults
(`denoise = 0` = passthrough), so generated audio didn't kick in until the
user touched a control. Now every continuous param's current TD value is
seeded into the dirty set on `ready` so the next 8 ms tick sends a complete
params message immediately.

### Textport silence

Massive cleanup. Removed:
- `[DIAG sent_to_server]` / `[DIAG initial_buffer]` hex+peak dumps
- Per-Connect WAV dumps to `/tmp/demon-debug/` (gated behind Debug toggle now)
- Every-600-cook `OnCookRecv #N` loop_pos+peak log
- The broken `[POST]` block (was raising on every call because TD blocks
  `numChans` reads during cook)
- The sampled `_send_text #N ok` lines
- The `[callbacks.onCook #N]` sampled counter prints in `build_tox.py`'s
  callbacks DAT
- Vestigial TD WebSocket DAT receive prints (we use `ws_client.py` now)

Gated behind a new **Debug Logging** Session-page toggle (default off):
- Vendor-path discovery prints
- WS frame echoes
- OnTick state telemetry every 2 s
- SpeakerOut underrun sampled logs
- `/tmp/demon-debug/*.wav` dumps

### Vendored deps

- New: `vendor/sounddevice/` (pure-Python wrapper) +
  `vendor/sounddevice/_sounddevice_data/portaudio-binaries/libportaudio.dylib`
  (universal2 binary, ~230 KB). On macOS the build proactively strips
  `com.apple.quarantine` from the dylib so first-load works without user
  intervention.
- Existing: `vendor/zstandard/{darwin-arm64, darwin-x64, win-amd64}/`,
  `vendor/websocket-client/`.

### Internal changes

- `wire.decode_config` no longer strips empty strings from the config
  payload (server expects `fixture_name: ""` to be present and was closing
  the WS otherwise).
- `_playback_pos` is now sourced from `LoopBuffer.position` (the
  authoritative play head) rather than dead-reckoned in `OnTick`. Sent to
  server as `playback_pos` in seconds; matches `demon-public-demo`'s
  `session.player.positionSec`.
- Removed the `audio_clock` Constant CHOP and the internal `audiodevout`
  Audio Device Out CHOP (both attempts to force TD's audio chain that we
  superseded with SpeakerOut).
- Removed the `frame_exec onFrameStart` force-cook on `audio_out`. The
  Script CHOP only cooks when something downstream consumes it (correct
  TD pattern). SpeakerOut reads the LoopBuffer directly and doesn't
  depend on Script CHOP cooks.

### Known limitations (deferred)

- Windows build pending — currently macOS universal2 (arm64 + x86_64) only.
- TD-native audio chain access (Audio Filter CHOP, multi-device routing,
  recording via Audio File Out) requires either a Select CHOP reference
  pattern or a virtual loopback device (BlackHole). README's "Audio
  routing" section documents both.

---

## [0.1.0] — 2026-05-14

Initial source release. Wire protocol, queue API, OAuth, schema, and
extension class complete and unit-tested (52 tests). `.tox` artifact
generated from this repo via a headless TD build.

End-to-end audio playback was not yet wired in this release.

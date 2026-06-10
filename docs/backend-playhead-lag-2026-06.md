# Backend escalation: server playhead lags client by 5–12s (growing) on short sources

**Date:** 2026-06-10 · **Reporter:** demonTD (TouchDesigner client) ·
**Affected:** post-2026-06-09/10 fleet deploy · **Repro:** hosted session,
source ≤ 60s (non-walk path)

## Symptom

Generated slices land progressively BEHIND the client playhead — lag grows
~0.77s/s from session start, pinning at the loop half-length (client
telemetry: `lead_min` −5.17 → −9.01 → −11.97s on a 24s loop). Continuous
param changes are therefore never audible (they only reach loop regions the
playhead already passed). Sessions die after ~60–75s with the pod resetting
the TCP connection (client sees `SSLError [SYS]` mid-write, no
close-notify; the queue row then shows "user left" from the client's
cleanup `leave()`).

The VST works fine on the same fleet — but its tested sessions use
full-length (>60s) tracks, i.e. **walk mode active**; the failing demonTD
sessions are ≤60s sources, i.e. the **non-walk** path
(`session.py:860`: `walk_window and use_trt and audio_duration_s >
walk_window_s + 0.1` — no-op for short sources, so the deployed web
client's `walk_window: true` makes no difference there either).

## Why this points server-side (evidence chain)

Backend code (DEMON.git@dc06b19):

1. `acestep/streaming/pipeline_runner.py` — the writer always chases the
   playhead: `decode_start = playhead_now + advance_s`, with `advance_s`
   clamped to `[lead_floor_s, lead_ceiling_s]` (0.25–1.35s for these
   sessions), and gap-fill renders at the advancing playhead every tick.
   **A slice can never legitimately land >1.35s+window from
   `playhead_now`.** Observed: slices land 5–12s behind the CLIENT
   playhead ⇒ `playhead_now` (server) lags the client playhead by 5–12s,
   growing.
2. `playhead_now` ← `_RemotePlayheadClock.sample()`
   (`pipeline_runner.py:~60-78`): anchors on `audio_eng.position` and
   extrapolates by wall clock between observed changes.
3. `audio_eng.position` is written ONLY by `session.set_knobs(...,
   playback_pos)` (`session.py:~1354`):
   `audio_eng.position = int(playback_pos * SAMPLE_RATE) % max(1,
   len(audio_eng.current))` — i.e., the clock's only input is the
   client's reported `playback_pos`.
4. Client-side freshness is verified:
   - `playback_pos` is read from the live ring position at build time on
     a dedicated thread, every ~8ms (effective wire rate ~29 msg/s under
     TD GIL load; ws-level `sent` counter matches — no client backlog).
   - The outbound queue was empty at close (`queued=1`).
   - The client playhead advances at exactly 48kHz (PortAudio callback
     math: 623 cb × 4096 frames over 53.2s).
   - Transport is TCP/WSS — no reordering possible.

⇒ The positions LEAVE the client fresh at ~29Hz; the server acts on
positions that are 5–12s old and getting older. **The staleness develops
between the pod's socket and `set_knobs`** — e.g., the session's WS
intake coroutine/task falling behind (growing message backlog) while the
runner keeps generating at the stale anchor, or something else delaying
`set_knobs` application for this connection profile.

Open question we can't answer from outside: why demonTD's connection
profile triggers the intake lag when the web client's (125 msg/s, larger
raw dicts) reportedly doesn't. Differences in our profile: ~29 msg/s
params cadence, smaller raw dict (no dcw_*/rcfg/guidance keys —
backend defaults verified fine), `walk_window=false` at the time of the
captures (since changed to true for parity; no-op for ≤60s sources per
the gating above).

## Asks

1. Run a pod with `DEMON_LAT_TRACE=1` serving a demonTD session with a
   ≤60s source and grep `lat_knob` — compare the logged `playback_s`
   against wall time. If `playback_s` falls progressively behind wall
   time, measure where the delay accrues (WS recv → dispatch →
   `set_knobs`).
2. Same trace for a web-client session with the SAME ≤60s source — the
   web takes the identical non-walk path; if it also lags, this is a
   plain backend regression on short sources, independent of client.
3. `_RemotePlayheadClock.sample()` hardening worth considering
   regardless: the anchor trusts `audio_eng.position` unconditionally;
   a monotonicity/staleness guard (ignore anchor regressions smaller
   than a wrap; cap extrapolation) would make the writer robust to
   intake hiccups instead of amplifying them into multi-second drift.

## Client-side telemetry available for joint debugging

demonTD ≥ v0.2.17 logs a `[health]` line every ~5s:
`patches=N late=N lead_min=±X.XXs tick=avg/max ms dec=max ms gens=N ...`
(`tick`/`dec`/`gens` are read from the slice headers — the pod's own
timing). With Debug enabled it also traces every ~10th slice:
`[slice] start=Xs pos=Ys lead=±Zs tick=…ms dec=…ms gens=N`.
Normal `tick_ms` + growing negative lead = stale playhead (intake/clock);
huge `tick_ms` = pod genuinely slow.

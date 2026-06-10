"""Binary-frame router — decodes + patches audio slices ON the WS recv
thread, so the loop buffer stays fresh through TD main-thread hitches.

Why this exists
---------------
Binary frames (the initial loop buffer + streaming slices) used to be
queued to the TD main thread for decode + patch. Any main-thread hitch
(heavy cook, UI interaction) delayed patching past the playhead — the
server's lead floor is only 0.25 s — so the playhead played STALE loop
content where the late patches should have been: the "music glitch"
flavor of choppy audio. zstd decompress + numpy conversion also ate TD
frame budget.

Everything the binary path needs is TD-free: `wire.decode_slice`,
numpy, and the LoopBuffer (its own lock, safe from any thread). Only
ONE thing in the old path touched TD — starting SpeakerOut after the
initial buffer (reads the Speakerout/Audiodevice pars) — so that, and
only that, is marshalled to the main thread as a `loop-initialized`
event.

Ordering
--------
Binary routing depends on text-message state (`ready`/`swap_ready` mark
the next binary as the headerless initial buffer; `stem_assets`
announces N opaque blobs to skip). The router learns these by SNIFFING
text frames on the recv thread (`sniff_text`, called by `_on_ws_text`
BEFORE enqueueing for the main thread). Since the WS is a single TCP
stream and everything now sequences on the one recv thread, ordering is
strictly stronger than the old main-thread-FIFO arrangement:

  * ready → initial → slices: sniff sets the flag, the next binary
    inits, subsequent binaries patch. No cross-thread race.
  * swap_ready mid-stream: the ring is cleared HERE (recv thread), so
    the main thread's swap_ready text handler must NOT also clear it —
    by the time the main thread drains that event, the recv thread may
    already have init'd the NEW loop, and a late clear would wipe it.
  * TCP FIFO means no old-track slices can arrive after swap_ready, so
    no epoch tracking is needed.

Lifecycle: one router per connection (created in `_open_ws`).
`detach()` is called on the OLD router before a reconnect and on
Disconnect: a lingering recv thread (WSClient.close joins only 2 s; a
stuck send can pin it for 30 s) can then never patch the new session's
ring or post stale events.
"""

from __future__ import annotations

import json

import numpy as np

# Sibling-module imports — same two-environment shim as demon_ext.py
# (TD Text DATs via mod(), plain files via sys.path outside TD).
try:
    _mod = mod  # type: ignore[name-defined]  # noqa: F821
    wire = _mod('wire')
    telemetry_mod = _mod('telemetry')
except NameError:
    import wire  # type: ignore
    import telemetry as telemetry_mod  # type: ignore


class BinaryRouter:
    def __init__(
        self,
        ring,
        post_event,
        stats=None,
        zstd_dec=None,
        log=print,
        is_debug=None,
        debug_dump=None,
    ):
        """
        ring        : audio.LoopBuffer (thread-safe; its own lock)
        post_event  : callable(kind, payload) — marshals to the main
                      thread (DemonExt passes _inbound.put)
        stats       : telemetry.SmoothnessStats | None
        zstd_dec    : a PER-ROUTER zstandard.ZstdDecompressor (the
                      decompressor is not concurrency-safe and recv
                      threads can briefly overlap across reconnects —
                      never share the module-global probe instance)
        is_debug    : callable() -> bool (GIL-atomic read of the Debug
                      toggle cache; never a TD par read)
        debug_dump  : callable(filename, pcm, channels) -> None | None —
                      WAV dump hook, only invoked when is_debug()
        """
        self._ring = ring
        self._post_event = post_event
        self._stats = stats
        self._zstd = zstd_dec
        self._log = log
        self._is_debug = is_debug or (lambda: False)
        self._debug_dump = debug_dump

        # Routing state — recv-thread-only after construction.
        self._expecting_initial = False
        self._ready_channels = 2
        self._stem_blobs_pending = 0
        self._detached = False
        self._unknown_flags_seen: set[int] = set()
        self._slice_err_seen: set[str] = set()
        self._debug_slice_count = 0

        # Counters (read by main-thread telemetry; GIL-atomic ints).
        self.n_binary_frames = 0
        self.n_slices = 0

    # -- lifecycle ---------------------------------------------------------------

    def detach(self) -> None:
        """Make this router inert. Called on the OLD router before a
        reconnect / on Disconnect, from the main thread (bool set is
        GIL-atomic)."""
        self._detached = True

    # -- text sniffing (recv thread) -----------------------------------------------

    # Only these message types affect binary routing; everything else is
    # skipped without a JSON parse (params_echo etc. arrive frequently).
    _SNIFF_MARKERS = ('"ready"', '"swap_ready"', '"stem_assets"',
                      '"stem_ready"')

    def sniff_text(self, msg: str) -> None:
        """Update binary-routing state from a text frame. Runs on the
        recv thread BEFORE the frame is enqueued for the main thread.
        Never raises."""
        if self._detached:
            return
        try:
            if not any(m in msg for m in self._SNIFF_MARKERS):
                return
            data = json.loads(msg)
            kind = data.get("type", "")
            if kind == "ready":
                self._expecting_initial = True
                self._ready_channels = int(data.get("channels", 2)) or 2
            elif kind == "swap_ready":
                # Clear HERE (recv thread) so clear→init sequences with
                # the arriving binaries. The main thread's swap_ready
                # handler is logging-only — a main-thread clear could
                # land AFTER the new track's init and wipe it.
                self._ring.clear()
                self._expecting_initial = True
                self._ready_channels = int(data.get("channels", 2)) or 2
            elif kind in ("stem_assets", "stem_ready"):
                self._stem_blobs_pending = int(data.get("count", 2) or 2)
        except Exception as e:
            try:
                self._log(f"[router] sniff_text raised: "
                          f"{type(e).__name__}: {e}")
            except Exception:
                pass

    # -- binary handling (recv thread) -----------------------------------------------

    def handle_binary(self, buf: bytes) -> None:
        """Route one binary frame: initial buffer → ring.init + event;
        stem blob → skip; slice → decode + patch. Runs on the recv
        thread; never raises."""
        if self._detached:
            return
        self.n_binary_frames += 1
        try:
            if self._expecting_initial:
                # Clear the flag only on a SUCCESSFUL init: if this
                # frame fails to decode as the initial buffer (e.g. a
                # malformed/truncated frame — odd byte count can't even
                # be float16), keep waiting so the real initial buffer
                # isn't misrouted as a slice.
                self._expecting_initial = not self._handle_initial(buf)
                return
            self._handle_slice_or_skip(buf)
        except Exception as e:
            try:
                self._log(f"[router] handle_binary raised: "
                          f"{type(e).__name__}: {e}")
            except Exception:
                pass

    def _handle_initial(self, buf: bytes) -> bool:
        """The first binary after ready/swap_ready: raw float16
        interleaved PCM, NO 23-byte slice header. Becomes the full loop
        content. ring.init is TD-free (numpy + the ring's lock), so it
        runs here; only the speaker start needs the main thread.
        Returns True iff the ring was initialized."""
        ch = self._ready_channels or 2
        try:
            u16 = np.frombuffer(bytes(buf), dtype=np.uint16)
        except ValueError as e:
            # Odd byte count — can't be float16 PCM. Don't burn the
            # expecting-initial flag on it.
            self._log(f"initial buffer: undecodable ({len(buf)}B): {e}")
            return False
        pcm = u16.view(np.float16).astype(np.float32)
        n = pcm.size // ch
        if n <= 0:
            self._log("initial buffer: empty")
            return False
        if self._is_debug():
            try:
                head_hex = bytes(buf[:32]).hex(" ")
                peak = float(np.max(np.abs(pcm))) if pcm.size > 0 else 0.0
                mabs = float(np.mean(np.abs(pcm))) if pcm.size > 0 else 0.0
                self._log(f"[DIAG initial_buffer] bytes={len(buf)} "
                          f"head32={head_hex}")
                self._log(f"[DIAG initial_buffer] decoded peak={peak:.4f} "
                          f"mean_abs={mabs:.4f} first10={pcm[:10].tolist()}")
            except Exception as e:
                self._log(f"[DIAG initial_buffer] log failed: {e}")
            if self._debug_dump is not None:
                try:
                    self._debug_dump("initial_buffer.wav", pcm[: n * ch], ch)
                except Exception:
                    pass
        self._ring.init(pcm[: n * ch], channels=ch)
        self._debug_slice_count = 0
        # Main thread: log + start SpeakerOut (TD par reads live there).
        self._post_event("loop-initialized",
                         {"frames": n, "channels": ch, "bytes": len(buf)})
        return True

    def _handle_slice_or_skip(self, buf: bytes) -> None:
        """Streaming slice (23-byte header + raw/zstd float16). Each
        slice PATCHES the loop at slice.start_sample. Flag bit 1 = delta
        (mix), otherwise overwrite. Mirrors useStartSession.ts.

        Skip-ahead for server features we don't decode:
          - stem blobs: announced by a `stem_assets` text message
            (sniffed above); consumed silently here.
          - unknown flag bits: logged ONCE per flag value.
        """
        flag_byte = buf[0] if len(buf) > 0 else 0
        if self._stem_blobs_pending > 0:
            self._stem_blobs_pending -= 1
            return
        if flag_byte & ~0x01:
            if flag_byte not in self._unknown_flags_seen:
                self._log(
                    f"slice flags=0x{flag_byte:02x} unknown ({len(buf)}B) "
                    f"— ignoring (future server feature, e.g. stems)"
                )
                self._unknown_flags_seen.add(flag_byte)
            return
        try:
            slice_ = wire.decode_slice(buf, zstd_dec=self._zstd)
        except Exception as e:
            # One log per error message — don't spam.
            key = type(e).__name__ + ":" + str(e)[:80]
            if key not in self._slice_err_seen:
                self._log(f"slice rejected ({len(buf)}B, "
                          f"flags=0x{flag_byte:02x}): {e}")
                self._slice_err_seen.add(key)
            return

        ch = max(1, slice_.channels)
        n = slice_.pcm.size // ch
        if n <= 0:
            return
        self.n_slices += 1

        if self._is_debug() and self._debug_slice_count < 3:
            idx = self._debug_slice_count
            self._debug_slice_count = idx + 1
            try:
                peak = float(np.max(np.abs(slice_.pcm))) if slice_.pcm.size else 0.0
                mabs = float(np.mean(np.abs(slice_.pcm))) if slice_.pcm.size else 0.0
                self._log(
                    f"[DIAG slice_{idx}] flags={slice_.flags} "
                    f"start_sample={slice_.start_sample} "
                    f"num_samples={slice_.num_samples} "
                    f"channels={slice_.channels} peak={peak:.4f} "
                    f"mean_abs={mabs:.4f}"
                )
            except Exception:
                pass
            if self._debug_dump is not None:
                try:
                    self._debug_dump(f"slice_{idx}.wav",
                                     slice_.pcm[: n * ch], ch)
                except Exception:
                    pass

        if slice_.flags == wire.SLICE_FLAG_DELTA:
            self._ring.add_delta(slice_.start_sample, slice_.pcm[: n * ch])
        else:
            self._ring.patch(slice_.start_sample, slice_.pcm[: n * ch])

        # Slice-coverage telemetry: flag every loop chunk this slice
        # touched as patched-at-least-once (diagnostic for "random
        # source flashes" reports).
        self._ring.mark_patched(slice_.start_sample, n)

        # Patch-lead telemetry: a slice landing at/behind the playhead
        # means the listener hears STALE loop content where this audio
        # should have been — the "music glitch" flavor of chop.
        # Server-side timing from the slice header rides along: normal
        # tick_ms + late patches = the pod generates fine but at a
        # STALE playhead; huge tick_ms = the pod itself is slow.
        if self._stats is not None:
            try:
                lead_s, late = telemetry_mod.compute_patch_lead(
                    slice_.start_sample, self._ring.position,
                    self._ring.frames, wire.SAMPLE_RATE)
                self._stats.note_patch(lead_s, late)
                self._stats.note_slice_timing(
                    slice_.tick_ms, slice_.dec_ms, slice_.num_gens)
            except Exception:
                pass

        # Debug slice-placement trace (client mirror of the backend's
        # DEMON_LAT_TRACE): every ~10th slice, log exactly where it
        # landed relative to the playhead, so a failing session shows
        # the lag curve directly in the textport.
        if self._is_debug() and self.n_slices % 10 == 1:
            try:
                pos = self._ring.position
                frames = self._ring.frames
                lead_s, late = telemetry_mod.compute_patch_lead(
                    slice_.start_sample, pos, frames, wire.SAMPLE_RATE)
                self._log(
                    f"[slice] start={slice_.start_sample / wire.SAMPLE_RATE:.2f}s "
                    f"pos={pos / wire.SAMPLE_RATE:.2f}s "
                    f"lead={lead_s:+.2f}s{' LATE' if late else ''} "
                    f"tick={slice_.tick_ms:.0f}ms dec={slice_.dec_ms:.0f}ms "
                    f"gens={slice_.num_gens}"
                )
            except Exception:
                pass

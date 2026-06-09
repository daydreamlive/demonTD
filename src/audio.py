"""
Audio I/O helpers for the DEMON TouchDesigner operator.

This module mirrors demon-public-demo's audio model: DEMON streams audio as
a LOOP. The server first sends an "initial buffer" containing the full
track (typically 24s = 1,152,000 samples at 48 kHz). After that, each
binary slice carries a `start_sample` (start frame in the loop) and PCM
data that PATCHES that region of the loop. Playback advances continuously
and wraps at end-of-buffer.

This is NOT a FIFO/streaming model — slices don't arrive in playback
order, they target arbitrary positions. The earlier RingBuffer
implementation treated slices as appended audio, which produced silence
+ glitches because slice positions didn't line up with the consumer's
read head.

Reference: demon-public-demo/vendor/demon-ui/engine/audio/AudioPlayer.ts

No TD dependencies.
"""

from __future__ import annotations

import threading
import time

import numpy as np


class LoopBuffer:
    """Fixed-size stereo PCM loop buffer with positional patching.

    Storage layout: (channels, frames) float32. The playback position
    advances on each `read()` call and wraps modulo `frames`. Slices
    (server → client) are written via `patch()` or `add_delta()` at
    explicit start-frame offsets — they DO NOT advance the read head.

    Reads return silence on uninitialized buffer rather than blocking.
    """

    def __init__(self, channels: int = 2, sample_rate: int = 48000,
                 seam_seconds: float = 0.05):
        self.channels = channels
        self._sample_rate = int(sample_rate)
        # Seam crossfade length (frames) — last N frames of the loop are
        # blended with the first N frames as the playhead approaches end-
        # of-buffer. Mirrors demon-public-demo/public/audio-worklet.js
        # `SEAM_FADE_SECONDS = 0.05` (50 ms at 48 kHz = 2400 frames).
        # On wrap we jump to position=_seam_frames (NOT 0) so the leading
        # samples that were folded into the crossfade aren't replayed.
        # This is what stops the "source flash every loop wrap" you'd
        # otherwise hear: the first ~50 ms of the buffer (which the
        # server's slice positions don't typically cover) plays only once.
        self._seam_frames = max(0, int(self._sample_rate * seam_seconds))
        self._buffer: np.ndarray | None = None  # shape (channels, frames)
        self._frames: int = 0
        self._position: int = 0  # next read frame
        self._lock = threading.Lock()
        # Slice-coverage tracking (diagnostic for the "random source
        # flashes during playback" reports). One bool per ~1s chunk of
        # the loop; flipped to True the first time a slice patches that
        # chunk. Cheap (< 100 bytes for typical loops) and allocation-
        # free in steady state. demon_ext.py reads
        # `coverage_fraction()` + `is_patched_at()` from the OnTick
        # telemetry block (Debug-gated) to surface whether the
        # playhead is ever reading an un-patched region.
        self._coverage_chunk_frames: int = int(self._sample_rate)  # 1 s
        self._patched_chunks: np.ndarray | None = None
        # Pre-allocated scratch for the seam crossfade weights. Each seam
        # span needs a (1.0 - t_vals) and t_vals coefficient array. Both
        # are derived from a single ramp [0, 1/seam, 2/seam, ..., (seam-1)/seam]
        # that's fixed for the life of the buffer — caching it lets
        # `read_into` skip ~3 numpy allocations per crossfade run, which
        # is the difference between a clean audio thread and one that
        # triggers gen-0 GC pauses on the PortAudio callback.
        # Shape (seam_frames,) float32. `_seam_one_minus_t_scratch` holds
        # (1.0 - t_vals) which the read_into hot path multiplies against
        # the tail samples.
        self._seam_t_scratch: np.ndarray | None = None
        self._seam_one_minus_t_scratch: np.ndarray | None = None
        if self._seam_frames > 0:
            self._init_seam_scratch()

    def _init_seam_scratch(self) -> None:
        """Recompute the cached seam-crossfade coefficient arrays. Called
        once at construction and whenever the buffer is re-`init()`'d (in
        case the channel count changed; seam_frames itself doesn't move
        but keeping the recompute path safe lets us extend later)."""
        s = self._seam_frames
        if s <= 0:
            self._seam_t_scratch = None
            self._seam_one_minus_t_scratch = None
            return
        # t_vals[k] = k/s for k in 0..s-1. Matches how `read` used to
        # compute it inline as (seam - dist_from_end)/seam where
        # dist_from_end = frames - tail_indices. As tail_indices walks
        # from (frames-s) up to (frames-1), dist_from_end goes from s
        # down to 1, so t = (s - dist)/s walks from 0 up to (s-1)/s.
        t = np.arange(s, dtype=np.float32) / float(s)
        self._seam_t_scratch = t
        self._seam_one_minus_t_scratch = (1.0 - t).astype(np.float32)
        # Scratch for the "head * t" partial product in the crossfade.
        # Pre-allocated at full seam width so `read_into` never has to
        # allocate, regardless of how many seam frames a given call
        # ends up touching. Sized as (channels, seam_frames) float32.
        self._seam_blend_scratch = np.zeros(
            (self.channels, s), dtype=np.float32)

    @property
    def frames(self) -> int:
        """Total frames in the loop (per channel). 0 if uninitialized."""
        return self._frames

    @property
    def position(self) -> int:
        """Current playback position in frames (per channel)."""
        with self._lock:
            return self._position

    @property
    def available(self) -> int:
        """Compatibility shim with RingBuffer.available for telemetry.
        In a loop model, "available" is always the loop size — the loop
        always has content (silence or audio). Reports frames * 2 to mirror
        the RingBuffer behavior (which counted total samples across channels)
        in legacy logs."""
        return self._frames

    def clear(self) -> None:
        with self._lock:
            self._buffer = None
            self._frames = 0
            self._position = 0
            # Coverage bitmap is sized to the loop; once the loop is
            # gone there's nothing to track. The next `init()` recreates
            # it at the new size.
            self._patched_chunks = None

    # -------- slice-coverage tracking ----------------------------------------

    def mark_patched(self, start_frame: int, num_frames: int) -> None:
        """Flag every coverage chunk overlapped by [start_frame,
        start_frame + num_frames) as patched-at-least-once.

        Called by demon_ext._on_binary right after `patch` / `add_delta`.
        Wraps across the end of the loop the same way patch() does so
        a slice that straddles the wrap correctly marks both halves.
        """
        if num_frames <= 0:
            return
        # Read these without the lock — they're set atomically by
        # init() and never partially written. We snapshot to locals
        # so even if init() races, we use a consistent view.
        chunks = self._patched_chunks
        if chunks is None:
            return
        frames = self._frames
        if frames <= 0:
            return
        cs = self._coverage_chunk_frames
        start = start_frame % frames
        end = start + num_frames
        # First-chunk to last-chunk indices (inclusive). The bitmap is
        # small enough that assignment is essentially free.
        if end <= frames:
            i0 = start // cs
            i1 = (end - 1) // cs
            chunks[i0:i1 + 1] = True
        else:
            # Wraps. Mark [start..frames) then [0..end-frames).
            i0 = start // cs
            i1 = (frames - 1) // cs
            chunks[i0:i1 + 1] = True
            rem = end - frames
            j0 = 0
            j1 = (rem - 1) // cs
            chunks[j0:j1 + 1] = True

    def coverage_fraction(self) -> float:
        """Fraction of the loop that has been patched at least once.
        Returns 0.0 on an uninitialized buffer."""
        chunks = self._patched_chunks
        if chunks is None or chunks.size == 0:
            return 0.0
        return float(chunks.sum()) / float(chunks.size)

    def is_patched_at(self, frame: int) -> bool:
        """True iff `frame`'s coverage chunk has been patched at least
        once. False for an uninitialized buffer or an out-of-range
        frame."""
        chunks = self._patched_chunks
        if chunks is None or chunks.size == 0:
            return False
        frames = self._frames
        if frames <= 0:
            return False
        idx = (frame % frames) // self._coverage_chunk_frames
        if idx >= chunks.size:
            return False
        return bool(chunks[idx])

    def init(self, pcm: np.ndarray, channels: int | None = None) -> None:
        """Initialize the loop with the server's initial buffer.

        Parameters
        ----------
        pcm : np.ndarray
            Either 1D interleaved (L0,R0,L1,R1,...) or 2D (channels, frames)
            float32. Sets the loop size to this length.
        channels : int, optional
            Override the channel count. Defaults to self.channels.
        """
        ch = int(channels or self.channels)
        pcm = np.ascontiguousarray(pcm, dtype=np.float32)
        if pcm.ndim == 1:
            frames = pcm.shape[0] // ch
            buf = pcm[: frames * ch].reshape(frames, ch).T
        elif pcm.ndim == 2:
            if pcm.shape[0] == ch:
                buf = pcm
                frames = pcm.shape[1]
            else:
                buf = pcm.T
                frames = pcm.shape[0]
        else:
            raise ValueError(f"unsupported pcm.ndim={pcm.ndim}")

        with self._lock:
            channels_changed = (ch != self.channels)
            self.channels = ch
            self._buffer = np.ascontiguousarray(buf, dtype=np.float32)
            self._frames = frames
            # Start playback PAST the seam region so frames 0..seam are
            # only ever heard through the wrap-crossfade fold, never
            # raw. Without this, the very first iteration of playback
            # plays the buffer's head (frames 0..seam) directly — and
            # if the server's initial encode is weak at the loop start
            # (low denoise / source leakage / edge artifacts), the user
            # hears a brief source-y flash on first connect. The
            # subsequent seam-fold then re-introduces those same head
            # samples on every wrap, but blended over the tail, which
            # is the entire point of the crossfade. Starting at `seam`
            # makes that the ONLY way those frames are ever audible.
            #
            # Edge case: buffers shorter than `4 * seam_frames` would
            # have `seam` clamped down to `frames // 4` in read_into,
            # so we mirror that clamp here. If seam is 0 (zero-length
            # seam config) or the buffer is tiny, fall back to 0.
            seam_start = (
                min(self._seam_frames, frames // 4)
                if frames > 0 else 0
            )
            self._position = seam_start
            # Re-size the seam blend scratch if channel count changed —
            # the read_into hot path indexes it as
            # `_seam_blend_scratch[:, :take]` and broadcasts a (take,)
            # weight against (channels, take). If we kept a stale
            # channel count, the broadcast would fail.
            if channels_changed and self._seam_frames > 0:
                self._seam_blend_scratch = np.zeros(
                    (ch, self._seam_frames), dtype=np.float32)
            # Reset slice-coverage bitmap for the new loop size. One
            # bool per chunk; +1 so the last partial chunk is tracked.
            n_chunks = (frames + self._coverage_chunk_frames - 1) // self._coverage_chunk_frames
            self._patched_chunks = np.zeros(max(1, n_chunks), dtype=bool)

    def swap(self, pcm: np.ndarray, channels: int | None = None) -> None:
        """Replace the entire loop buffer (server `swap_ready` path).

        Resets playback position to 0 like AudioPlayer.swap() does.
        """
        self.init(pcm, channels=channels)

    def patch(self, start_frame: int, pcm: np.ndarray) -> None:
        """Overwrite frames[start_frame : start_frame + N] with `pcm`.

        Wraps if the write region crosses the loop end.
        """
        self._write(start_frame, pcm, add=False)

    def add_delta(self, start_frame: int, pcm: np.ndarray) -> None:
        """Additive blend (used for SLICE_FLAG_DELTA payloads)."""
        self._write(start_frame, pcm, add=True)

    def _write(self, start_frame: int, pcm: np.ndarray, add: bool) -> None:
        pcm = np.ascontiguousarray(pcm, dtype=np.float32)
        with self._lock:
            buf = self._buffer
            frames = self._frames
            if buf is None or frames == 0:
                return
            # Read the channel count INSIDE the lock — a concurrent
            # init() with a different channel count between an unlocked
            # read and the write below would make the shape math below
            # silently wrong (mono broadcast into stereo, or a dropped
            # patch).
            ch = buf.shape[0]
            if pcm.ndim == 1:
                n = pcm.shape[0] // ch
                pcm_2d = pcm[: n * ch].reshape(n, ch).T
            elif pcm.ndim == 2 and pcm.shape[0] == ch:
                pcm_2d = pcm
                n = pcm.shape[1]
            elif pcm.ndim == 2 and pcm.shape[1] == ch:
                pcm_2d = pcm.T
                n = pcm.shape[0]
            else:
                return

            if n <= 0:
                return
            start = start_frame % frames
            end = start + n
            if end <= frames:
                if add:
                    buf[:, start:end] += pcm_2d
                else:
                    buf[:, start:end] = pcm_2d
            else:
                first_chunk = frames - start
                if add:
                    buf[:, start:] += pcm_2d[:, :first_chunk]
                else:
                    buf[:, start:] = pcm_2d[:, :first_chunk]
                rem = n - first_chunk
                # If pcm is larger than the whole loop, last block wins.
                if rem >= frames:
                    if add:
                        buf[:, :] += pcm_2d[:, first_chunk:first_chunk + frames]
                    else:
                        buf[:, :] = pcm_2d[:, first_chunk:first_chunk + frames]
                else:
                    if add:
                        buf[:, :rem] += pcm_2d[:, first_chunk:]
                    else:
                        buf[:, :rem] = pcm_2d[:, first_chunk:]

    def read(self, num_frames: int) -> np.ndarray:
        """Read `num_frames` frames at the playback position; advance head.

        Allocates a fresh `(channels, num_frames)` float32 array and
        delegates to `read_into`. Kept for non-audio-thread callers
        (tests, `peek`-style helpers). **Audio-thread callers should use
        `read_into(out)` with a pre-allocated buffer** to avoid GC
        pressure that causes intermittent stutters; see `read_into`
        docstring for the why.
        """
        out = np.zeros((self.channels, max(0, num_frames)), dtype=np.float32)
        if num_frames > 0:
            self.read_into(out)
        return out

    def read_into(self, out: np.ndarray) -> None:
        """Fill `out` with the next `out.shape[1]` frames; advance head.

        `out` must be shape (channels, num_frames) float32. Allocation-
        free in steady state — all temporaries come from the cached
        `_seam_t_scratch` / `_seam_one_minus_t_scratch` arrays plus
        views into the buffer.

        Why: the audio thread's PortAudio callback runs every ~85 ms
        (4096 frames @ 48 kHz). Each numpy allocation pushes CPython's
        gen-0 heap closer to a collection, and a gen-0 GC that fires
        on the audio thread is a ~10-30 ms pause — half our deadline.
        The "audio stutters, then resolves, then stutters again"
        pattern matches GC quiesce/spike cycles exactly. Removing the
        allocations from this hot path is the difference between
        clean audio and intermittent dropouts.

        Behavior is otherwise identical to `read`: wraps the loop with
        a seam crossfade (last `_seam_frames` blended with first
        `_seam_frames`; on wrap, playhead jumps to `_seam_frames`,
        not 0). Mirrors the AudioWorklet at
        `demon-public-demo/public/audio-worklet.js` lines 191–239.

        IMPORTANT: This is the AUTHORITATIVE play head. Only one
        consumer (`SpeakerOut._pa_callback`) should call this. Other
        consumers (the Script CHOP cook callback for visual reactivity)
        must use `peek()` so they don't race the head forward and cause
        the audio thread to skip samples.
        """
        ch = self.channels
        num_frames = out.shape[1]
        if num_frames <= 0:
            return

        with self._lock:
            buf = self._buffer
            frames = self._frames
            if buf is None or frames == 0:
                # Caller's `out` may be uninitialized — explicitly silence.
                out.fill(0.0)
                return
            if buf.shape[0] != out.shape[0]:
                # Channel count changed under us (init() raced between
                # the caller allocating `out` and this lock). Numpy
                # would silently BROADCAST a mono buffer into a stereo
                # `out` — emit silence for this one read instead.
                out.fill(0.0)
                return

            seam = min(self._seam_frames, frames // 4)
            seam_start = frames - seam  # first frame inside the tail seam
            pos = self._position
            written = 0
            # Cached crossfade weights — same shape as the full seam.
            # We slice into them per crossfade run; no allocation.
            t_full = self._seam_t_scratch
            one_minus_t_full = self._seam_one_minus_t_scratch

            while written < num_frames:
                need = num_frames - written
                if pos < seam_start:
                    # Pre-seam: bulk copy contiguous range from buf into
                    # out. Already allocation-free — numpy slicing into
                    # an existing destination just memmoves.
                    take = min(seam_start - pos, need)
                    out[:, written:written + take] = buf[:, pos:pos + take]
                    pos += take
                    written += take
                else:
                    # In-seam crossfade. Walk position pos..pos+take-1;
                    # the corresponding "distance from end" is
                    # (frames - pos), (frames - pos - 1), ... down to 1.
                    # t_vals[k] = (seam - dist_from_end[k]) / seam, which
                    # is the same ramp as t_full[seam - dist_from_end]
                    # for k = 0..take-1.
                    max_take = frames - pos
                    take = min(max_take, need)
                    # The t ramp inside the seam starts at index
                    # `seam - (frames - pos)` and runs for `take`
                    # consecutive entries of t_full.
                    seam_offset = seam - (frames - pos)
                    t_slice = t_full[seam_offset:seam_offset + take]
                    one_minus_t_slice = one_minus_t_full[
                        seam_offset:seam_offset + take]
                    # tail comes from buf[:, pos:pos+take] (contiguous);
                    # head comes from buf[:, seam_offset:seam_offset+take]
                    # (also contiguous — these are the first `seam`
                    # frames of the buffer being folded back).
                    tail = buf[:, pos:pos + take]
                    head = buf[:, seam_offset:seam_offset + take]
                    # out[:, w:w+take] = tail*(1-t) + head*t, zero temps.
                    # Two products into pre-allocated buffers:
                    #   dst       <- tail * (1-t)        (broadcast)
                    #   blend     <- head * t            (broadcast)
                    #   dst      += blend
                    # `_seam_blend_scratch` is (channels, seam_frames)
                    # so a take-wide slice into it is always valid.
                    dst = out[:, written:written + take]
                    blend = self._seam_blend_scratch[:, :take]
                    np.multiply(tail, one_minus_t_slice, out=dst)
                    np.multiply(head, t_slice, out=blend)
                    np.add(dst, blend, out=dst)
                    pos += take
                    written += take
                    if pos >= frames:
                        # Wrap to `seam`, NOT 0 — the first seam frames
                        # were folded into the crossfade above.
                        pos = seam if seam > 0 else 0

            self._position = pos

    def peek(self, num_frames: int,
             position: int | None = None) -> np.ndarray:
        """Read `num_frames` frames WITHOUT advancing the play head.

        For non-authoritative consumers (e.g. a Script CHOP that wants
        to mirror the current audio for visual reactivity but must not
        affect what the actual speaker thread plays). Returns shape
        (channels, num_frames) float32; wraps the loop automatically;
        returns silence if uninitialized.

        `position` lets you read from an explicit frame offset instead
        of the current play head. Defaults to the play head.
        """
        ch = self.channels
        out = np.zeros((ch, num_frames), dtype=np.float32)
        if num_frames <= 0:
            return out

        with self._lock:
            buf = self._buffer
            frames = self._frames
            if buf is None or frames == 0:
                return out
            if buf.shape[0] != ch:
                # init() raced and changed the channel count between
                # allocating `out` and taking the lock — silence beats
                # numpy's silent mono→stereo broadcast.
                return out

            pos = self._position if position is None else (position % frames)
            written = 0
            while written < num_frames:
                end_in_buf = frames - pos
                take = min(end_in_buf, num_frames - written)
                out[:, written:written + take] = buf[:, pos:pos + take]
                written += take
                pos = (pos + take) % frames

            # Critically: do NOT update self._position here.
            return out

    def seek(self, position_frames: int) -> None:
        """Set the playback position. Wraps modulo loop size."""
        with self._lock:
            if self._frames > 0:
                self._position = position_frames % self._frames


# Back-compat alias: existing call sites use `RingBuffer`. Map it to
# LoopBuffer with a similar interface so the rest of the code base
# doesn't have to be touched everywhere.
RingBuffer = LoopBuffer


# -----------------------------------------------------------------------------
# Resampling
# -----------------------------------------------------------------------------

def linear_resample(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Cheap linear-interpolation resample.

    pcm: shape (channels, samples) float32.
    Returns shape (channels, new_samples).
    """
    if src_rate == dst_rate or pcm.size == 0:
        return pcm

    pcm = np.asarray(pcm, dtype=np.float32)
    if pcm.ndim == 1:
        pcm = pcm.reshape(1, -1)

    n_in = pcm.shape[1]
    n_out = max(1, int(round(n_in * dst_rate / src_rate)))
    if n_out == n_in:
        return pcm

    x_in = np.linspace(0.0, 1.0, num=n_in, endpoint=False, dtype=np.float32)
    x_out = np.linspace(0.0, 1.0, num=n_out, endpoint=False, dtype=np.float32)

    out = np.empty((pcm.shape[0], n_out), dtype=np.float32)
    for ch in range(pcm.shape[0]):
        out[ch] = np.interp(x_out, x_in, pcm[ch]).astype(np.float32, copy=False)
    return out


def to_stereo(pcm: np.ndarray) -> np.ndarray:
    """Force a CHOP-shaped array to stereo (2, samples). Mono → duplicated L→R."""
    pcm = np.asarray(pcm, dtype=np.float32)
    if pcm.ndim == 1:
        return np.stack([pcm, pcm], axis=0)
    if pcm.shape[0] == 1:
        return np.repeat(pcm, 2, axis=0)
    if pcm.shape[0] == 2:
        return pcm
    if pcm.shape[0] > 2:
        return pcm[:2]
    raise ValueError(f"Unsupported PCM shape: {pcm.shape}")


# -----------------------------------------------------------------------------
# SpeakerOut — Python-side audio playback via sounddevice / PortAudio.
# -----------------------------------------------------------------------------
#
# We use this to bypass TD's CHOP audio chain (which cannot pull a Script
# CHOP at audio rate across a Base COMP output boundary in TD 2025). The
# OutputStream callback runs in PortAudio's own audio thread; we just pull
# samples from the LoopBuffer and write them into the output buffer.
#
# The TD Script CHOP `audio_out` path is left functional so users can still
# tap the audio for visual reactivity in their networks — both paths read
# from the same thread-safe LoopBuffer.


import ctypes as _ctypes
import os as _os


# PortAudio C constants we need
_paFloat32 = 0x00000001
_paInt16   = 0x00000008
_paNoFlag = 0
_paContinue = 0
_paOutputUnderflow = 4
_paNoDevice = -1
_paFormatIsSupported = 0
_paFramesPerBufferUnspecified = 0
_paInternalError = -9986       # PaError: generic Pa internal failure
_paInvalidSampleRate = -9997   # PaError: rate the device can't deliver
_paUnanticipatedHostError = -9999  # PaError: host (CoreAudio) bubbled an err

# Mirror of PortAudio's PaDeviceInfo struct so we can read device-config
# metadata for diagnostic logging when Pa_OpenDefaultStream fails.
# Field order per portaudio.h (struct version 2). Field types match the
# ABI — sizeof(PaTime)==8 (double), int==4, char*==8 on 64-bit.
class _PaDeviceInfo(_ctypes.Structure):
    _fields_ = [
        ("structVersion",            _ctypes.c_int),
        ("name",                     _ctypes.c_char_p),
        ("hostApi",                  _ctypes.c_int),
        ("maxInputChannels",         _ctypes.c_int),
        ("maxOutputChannels",        _ctypes.c_int),
        ("defaultLowInputLatency",   _ctypes.c_double),
        ("defaultLowOutputLatency",  _ctypes.c_double),
        ("defaultHighInputLatency",  _ctypes.c_double),
        ("defaultHighOutputLatency", _ctypes.c_double),
        ("defaultSampleRate",        _ctypes.c_double),
    ]


# Mirror of PortAudio's PaHostErrorInfo. Pa_GetLastHostErrorInfo()
# returns this with the OS-level error code that wrapped into a generic
# paUnanticipatedHostError / paInternalError. On macOS that's a
# CoreAudio OSStatus (a four-char code) — much more diagnosable than
# the Pa-level "Internal PortAudio error".
class _PaHostErrorInfo(_ctypes.Structure):
    _fields_ = [
        ("hostApiType",  _ctypes.c_int),
        ("errorCode",    _ctypes.c_long),
        ("errorText",    _ctypes.c_char_p),
    ]


# Minimal mirror of PortAudio's PaHostApiInfo — enough to log which host
# API backs the default output device (MME / DirectSound / WASAPI / WDM-KS
# on Windows; Core Audio on macOS). On Windows the host-API + default-
# device choice is the usual cause of a "connected but silent" session.
class _PaHostApiInfo(_ctypes.Structure):
    _fields_ = [
        ("structVersion",       _ctypes.c_int),
        ("type",                _ctypes.c_int),
        ("name",                _ctypes.c_char_p),
        ("deviceCount",         _ctypes.c_int),
        ("defaultInputDevice",  _ctypes.c_int),
        ("defaultOutputDevice", _ctypes.c_int),
    ]


# Mirror of PortAudio's PaStreamParameters. The "right" way to open a
# stream — Pa_OpenDefaultStream is a wrapper around this that picks
# a tight default latency. Some macOS devices reject the tight default
# with kAudioUnitErr_InvalidPropertyValue; passing the device's
# defaultHighOutputLatency here gives PortAudio room to negotiate.
class _PaStreamParameters(_ctypes.Structure):
    _fields_ = [
        ("device",                    _ctypes.c_int),
        ("channelCount",              _ctypes.c_int),
        ("sampleFormat",              _ctypes.c_ulong),
        ("suggestedLatency",          _ctypes.c_double),
        ("hostApiSpecificStreamInfo", _ctypes.c_void_p),
    ]


# Sentinel menu value for "use the system default output device".
DEFAULT_DEVICE_TOKEN = "-1"


def format_output_device_menu(
        devices: list[dict]) -> tuple[list[str], list[str]]:
    """Build (menu_names, menu_labels) for the Audio Output Device menu
    from SpeakerOut.list_output_devices() output.

    `menu_names` are the *values* read back via par.eval() — device indices
    as strings, with "-1" first for the system default. `menu_labels` are the
    human-readable display strings. Pure function — unit-tested, no PortAudio.
    """
    names: list[str] = [DEFAULT_DEVICE_TOKEN]
    labels: list[str] = ["Default (system)"]
    for d in devices:
        names.append(str(int(d["index"])))
        label = f"{d.get('name', '?')} — {d.get('host_api', '?')}"
        if d.get("is_default"):
            label += " [system default]"
        labels.append(label)
    return names, labels


class SpeakerOut:
    """Plays a LoopBuffer to the system default audio device at audio rate.

    Uses stdlib `ctypes` to bind directly to libportaudio.dylib. We DON'T use
    the `sounddevice` Python wrapper because it depends on cffi/`_cffi_backend`
    which TouchDesigner 2025's bundled Python doesn't ship.

    Lifecycle: `start()` opens the default output stream and begins audio
    callback at the requested sample rate; `stop()` halts and closes the
    stream. Both are idempotent.

    Thread model: PortAudio invokes `_pa_callback` from its own audio thread
    at hardware buffer cadence (~5 ms typical). The callback reads the
    requested `frames` from the LoopBuffer (thread-safe) and copies an
    interleaved float32 buffer into PortAudio's output pointer via
    `ctypes.memmove`. No Python allocations beyond the LoopBuffer.read call.
    """

    # Class-level lazy-loaded PortAudio binding. Shared across instances so
    # we don't re-dlopen on every Connect/Disconnect cycle.
    _lib: "_ctypes.CDLL | None" = None
    _lib_initialized: bool = False

    @classmethod
    def _load_lib(cls, vendor_dylib_path: str | None = None,
                  log=print) -> "_ctypes.CDLL | None":
        if cls._lib is not None:
            return cls._lib
        candidates: list[str] = []
        if vendor_dylib_path:
            candidates.append(vendor_dylib_path)
        # System-installed PortAudio fallbacks, per platform.
        import platform as _platform
        sysname = _platform.system().lower()
        if sysname == "darwin":
            candidates.extend([
                "/opt/homebrew/lib/libportaudio.dylib",
                "/usr/local/lib/libportaudio.dylib",
                "libportaudio.dylib",  # let dlopen search DYLD path
            ])
        elif sysname == "windows":
            candidates.extend([
                "libportaudio64bit.dll",
                "libportaudio.dll",  # some installs drop the bitness suffix
            ])
        else:
            candidates.extend([
                "libportaudio.so",
                "libportaudio.so.2",
            ])
        last_err = None
        for path in candidates:
            try:
                lib = _ctypes.CDLL(path)
                cls._configure_lib(lib)
                cls._lib = lib
                log(f"[speaker_out] loaded PortAudio from {path}")
                return lib
            except OSError as e:
                last_err = e
                continue
        log(f"[speaker_out] could not load PortAudio binary: {last_err}")
        return None

    @staticmethod
    def _configure_lib(lib: "_ctypes.CDLL") -> None:
        """Set argtypes / restype for the PortAudio C functions we call.
        Mostly defensive on 64-bit; ctypes does the right thing for void* /
        int / double if signatures aren't declared, but being explicit avoids
        size mismatches on edge cases."""
        lib.Pa_Initialize.restype = _ctypes.c_int
        lib.Pa_Terminate.restype = _ctypes.c_int
        lib.Pa_GetErrorText.argtypes = [_ctypes.c_int]
        lib.Pa_GetErrorText.restype = _ctypes.c_char_p
        lib.Pa_OpenDefaultStream.argtypes = [
            _ctypes.POINTER(_ctypes.c_void_p),  # PaStream**
            _ctypes.c_int,                       # numInputChannels
            _ctypes.c_int,                       # numOutputChannels
            _ctypes.c_ulong,                     # PaSampleFormat
            _ctypes.c_double,                    # sampleRate
            _ctypes.c_ulong,                     # framesPerBuffer
            _ctypes.c_void_p,                    # PaStreamCallback*
            _ctypes.c_void_p,                    # userData
        ]
        lib.Pa_OpenDefaultStream.restype = _ctypes.c_int
        lib.Pa_StartStream.argtypes = [_ctypes.c_void_p]
        lib.Pa_StartStream.restype = _ctypes.c_int
        lib.Pa_StopStream.argtypes = [_ctypes.c_void_p]
        lib.Pa_StopStream.restype = _ctypes.c_int
        lib.Pa_CloseStream.argtypes = [_ctypes.c_void_p]
        lib.Pa_CloseStream.restype = _ctypes.c_int
        # Device-inspection APIs — used for diagnostic dumps when
        # Pa_OpenDefaultStream fails. Pa_GetDeviceInfo returns a pointer
        # into Pa's internal table; we read it as a struct (declared
        # below as PaDeviceInfo).
        lib.Pa_GetDefaultOutputDevice.restype = _ctypes.c_int
        lib.Pa_GetDeviceCount.restype = _ctypes.c_int
        lib.Pa_GetDeviceInfo.argtypes = [_ctypes.c_int]
        lib.Pa_GetDeviceInfo.restype = _ctypes.POINTER(_PaDeviceInfo)
        lib.Pa_GetHostApiInfo.argtypes = [_ctypes.c_int]
        lib.Pa_GetHostApiInfo.restype = _ctypes.c_void_p
        # Surfaces the OS-level error wrapped inside paUnanticipated /
        # paInternalError. Read after a failed Pa_OpenDefaultStream to
        # see the actual CoreAudio OSStatus.
        lib.Pa_GetLastHostErrorInfo.restype = _ctypes.POINTER(_PaHostErrorInfo)
        # Full Pa_OpenStream — used as the fallback when
        # Pa_OpenDefaultStream's tight built-in latency is rejected
        # by Core Audio (kAudioUnitErr_InvalidPropertyValue / -10851).
        lib.Pa_OpenStream.argtypes = [
            _ctypes.POINTER(_ctypes.c_void_p),       # PaStream**
            _ctypes.POINTER(_PaStreamParameters),    # input params (or NULL)
            _ctypes.POINTER(_PaStreamParameters),    # output params
            _ctypes.c_double,                        # sampleRate
            _ctypes.c_ulong,                         # framesPerBuffer
            _ctypes.c_ulong,                         # streamFlags (paNoFlag)
            _ctypes.c_void_p,                        # PaStreamCallback*
            _ctypes.c_void_p,                        # userData
        ]
        lib.Pa_OpenStream.restype = _ctypes.c_int
        lib.Pa_IsFormatSupported.argtypes = [
            _ctypes.POINTER(_PaStreamParameters),
            _ctypes.POINTER(_PaStreamParameters),
            _ctypes.c_double,
        ]
        lib.Pa_IsFormatSupported.restype = _ctypes.c_int

    # ctypes callback type. Kept as class-level to avoid recreating the
    # CFUNCTYPE on every instance (which would trigger libffi closure
    # allocation churn).
    _CB_TYPE = _ctypes.CFUNCTYPE(
        _ctypes.c_int,        # return: PaContinue / Complete / Abort
        _ctypes.c_void_p,     # input buffer
        _ctypes.c_void_p,     # output buffer
        _ctypes.c_ulong,      # frame count
        _ctypes.c_void_p,     # PaStreamCallbackTimeInfo*
        _ctypes.c_ulong,      # status flags
        _ctypes.c_void_p,     # userData
    )

    def __init__(self, loop: "LoopBuffer",
                 sample_rate: int = 48000,
                 channels: int = 2,
                 log=print,
                 dylib_path: str | None = None,
                 frames_per_buffer: int = 4096,
                 device_index: int = -1):
        self._loop = loop
        self._sample_rate = float(sample_rate)
        self._channels = int(channels)
        # PortAudio output device index to open. -1 = system default
        # (Pa_OpenDefaultStream fast path). >= 0 = a specific device the
        # user picked (see list_output_devices); opened explicitly via
        # PaStreamParameters. Set by set_device_index() before start().
        self._device_index = int(device_index)
        # Larger blocks = fewer Python callbacks per second = less GIL
        # contention with TD's main thread. 4096 frames @ 48 kHz = ~85 ms.
        # That's our audio latency floor; acceptable for a generative
        # session, and avoids occasional stutters when a wrap-spanning
        # callback coincides with TD doing heavy work on the main thread.
        # 2048 had occasional misses (~5% glitch rate); 4096 doubles our
        # deadline headroom for the audio callback to complete.
        self._frames_per_buffer = int(frames_per_buffer)
        self._log = log
        self._dylib_path = dylib_path
        self._stream: int | None = None  # raw c_void_p value
        self._stream_ptr = None  # holds the C pointer alive
        self._underrun_count = 0
        self._callback_count = 0
        # Negotiated by start(). Defaults to paFloat32 (preferred); falls
        # back to paInt16 when Core Audio refuses float32 on the user's
        # device. The pa_callback reads this to decide which dtype to
        # write into PortAudio's output buffer.
        self._sample_format_pa: int = _paFloat32

        # --- audio-thread scratch buffers (zero-alloc callback path) ----
        # The PortAudio callback runs ~12 times/sec at 4096 frames @
        # 48 kHz. If we allocate numpy arrays on the audio thread,
        # CPython's gen-0 GC can fire there and pause us long enough
        # to blow the 85 ms deadline → stutter. Pre-allocating means
        # zero allocations per callback in steady state.
        #
        # `_max_block_frames` is the largest block size we expect from
        # PortAudio. 4096 is our request (frames_per_buffer); when we
        # open with paFramesPerBufferUnspecified the device decides and
        # sometimes uses larger blocks. 16384 frames @ 48 kHz = ~340 ms
        # which is comfortably above anything macOS Core Audio emits in
        # practice. If a callback ever exceeds this we fall back to
        # per-call allocation (rare, logged).
        self._max_block_frames = max(self._frames_per_buffer * 4, 16384)
        # (channels, max_block_frames) float32 — LoopBuffer.read_into
        # writes here.
        self._scratch_pcm = np.zeros(
            (self._channels, self._max_block_frames), dtype=np.float32)
        # Interleaved float32 destination for memmove — same total
        # samples but laid out (frame0_L, frame0_R, frame1_L, ...).
        self._scratch_interleaved_f32 = np.zeros(
            self._max_block_frames * self._channels, dtype=np.float32)
        # Interleaved int16 for the degraded fallback open path. Only
        # touched when self._sample_format_pa == paInt16. Pre-allocating
        # both means we don't have to grow a scratch on a format change.
        self._scratch_interleaved_i16 = np.zeros(
            self._max_block_frames * self._channels, dtype=np.int16)

        # --- audio-thread latency telemetry --------------------------
        # Counts since the last reporter drain (in _pa_callback's sense
        # of "since"). The main thread reads + resets these via
        # `drain_latency_stats()`. Plain ints — Python's GIL serializes
        # reads/writes on small builtins, so no extra locking needed
        # here for the orders-of-magnitude correct numbers we want.
        self._cb_count_since_drain: int = 0
        self._cb_latency_sum_ns: int = 0
        self._cb_latency_max_ns: int = 0
        # Watermark for drain_latency_stats' underruns_since_drain delta
        # (only ever touched by the main thread inside the drain).
        self._underruns_at_last_drain: int = 0
        # Underrun-by-rate-mismatch hint — set True if a callback ever
        # asks for more frames than _max_block_frames. Visible in the
        # next telemetry report.
        self._cb_over_max_block: bool = False

        # Pause flag, driven from the main thread by DemonExt's
        # OnPlayStateChange when TD's timeline is paused. Audio thread
        # reads this every callback; when True we emit silence and do
        # NOT advance the LoopBuffer playhead, so audio resumes from
        # the same sample on un-pause. Single bool = GIL-atomic, no
        # lock needed.
        self._paused: bool = False

        # Keep a strong reference to the bound CFUNCTYPE so it doesn't get
        # garbage-collected while the audio thread is calling into it.
        self._c_callback = self._CB_TYPE(self._pa_callback)

    @property
    def underrun_count(self) -> int:
        return self._underrun_count

    @property
    def is_running(self) -> bool:
        return self._stream is not None

    def start(self) -> bool:
        """Open the default output stream and start playback. Returns True
        on success, False on any error (already logged).

        Order of operations
        -------------------
        1. **Direct open** — Pa_OpenDefaultStream at the requested
           (sample_rate, frames_per_buffer, paFloat32). This is the
           v0.1.5 known-good code path; for most users it succeeds and
           we return immediately.
        2. **Only on failure**: probe the default device's metadata
           (sample rate, defaultHighOutputLatency), then run the
           v0.2.4 - v0.2.8 fallback matrix (alternate rates / buffer
           sizes, Pa_OpenStream with explicit PaStreamParameters,
           paInt16 sample format).

        The eager probe used to run BEFORE step 1; that introduced a
        regression on macOS Sequoia where Pa_GetDeviceInfo touches the
        default-output AudioUnit's stream-format property and the
        subsequent AudioUnitSetProperty(kAudioUnitProperty_StreamFormat)
        is rejected with kAudioUnitErr_InvalidPropertyValue (-10851).
        Deferring the probe to the failure branch restores the v0.1.5
        path while keeping the fallbacks available for devices that
        actually need them.
        """
        if self._stream is not None:
            return True
        lib = self._load_lib(self._dylib_path, log=self._log)
        if lib is None:
            return False
        # Pa_Initialize is idempotent — fine to call repeatedly.
        if not SpeakerOut._lib_initialized:
            err = lib.Pa_Initialize()
            if err != 0:
                msg = lib.Pa_GetErrorText(err) or b"unknown"
                self._log(f"[speaker_out] Pa_Initialize failed: "
                          f"{msg.decode(errors='replace')}")
                return False
            SpeakerOut._lib_initialized = True

        def _host_error_detail() -> str:
            """Read Pa_GetLastHostErrorInfo for the OS-level reason."""
            try:
                hei_ptr = lib.Pa_GetLastHostErrorInfo()
                if not hei_ptr:
                    return ""
                hei = hei_ptr.contents
                txt = (hei.errorText or b"").decode(errors="replace")
                return (
                    f" hostErr code={hei.errorCode} "
                    f"text={txt!r}"
                )
            except Exception:
                return ""

        # --- Step 1: direct v0.1.5-style open ---------------------------------
        # Do this BEFORE any device probe so Pa_GetDeviceInfo doesn't
        # poison the AudioUnit state on Sequoia. On success we short-
        # circuit out and never run the fallback matrix.
        stream_ptr = _ctypes.c_void_p()
        chosen_rate = self._sample_rate
        chosen_buf = self._frames_per_buffer
        chosen_format = _paFloat32
        # When the user picked a specific output device, skip the
        # default-device fast path entirely (it opens the *default* device,
        # not the chosen one) and go straight to opening that device via
        # PaStreamParameters in the matrix below. err=-1 forces it.
        explicit = self._device_index >= 0
        if explicit:
            err = -1
            self._log(
                f"[speaker_out] opening user-selected output device "
                f"index={self._device_index} "
                f"(skipping default-device fast path)"
            )
        else:
            err = lib.Pa_OpenDefaultStream(
                _ctypes.byref(stream_ptr),
                0,                              # no input
                self._channels,
                _paFloat32,
                self._sample_rate,
                self._frames_per_buffer,
                _ctypes.cast(self._c_callback, _ctypes.c_void_p),
                None,
            )
            if err != 0:
                msg = (lib.Pa_GetErrorText(err) or b"unknown").decode(
                    errors="replace")
                self._log(
                    f"[speaker_out] direct Pa_OpenDefaultStream@"
                    f"{self._sample_rate}Hz buf={self._frames_per_buffer} "
                    f"failed: {msg} (err={err}){_host_error_detail()} "
                    f"— running fallback matrix"
                )

        # --- Step 2: only on failure, probe + run fallback matrix --------------
        device_rate: float | None = None
        device_index: int = -1
        device_high_latency: float = 0.020   # 20 ms — safe default
        device_max_out: int = self._channels
        if err != 0:
            try:
                dev = (self._device_index if explicit
                       else int(lib.Pa_GetDefaultOutputDevice()))
                if dev >= 0:
                    device_index = dev
                    info_ptr = lib.Pa_GetDeviceInfo(dev)
                    if info_ptr:
                        info = info_ptr.contents
                        device_rate = float(info.defaultSampleRate)
                        device_high_latency = float(info.defaultHighOutputLatency)
                        device_max_out = int(info.maxOutputChannels)
                        self._log(
                            f"[speaker_out] default output: "
                            f"dev={dev} name={(info.name or b'?').decode(errors='replace')} "
                            f"maxOut={device_max_out} "
                            f"defaultSampleRate={device_rate} "
                            f"defaultHighOutputLatency={device_high_latency:.4f}s"
                        )
            except Exception as e:
                self._log(f"[speaker_out] device-info probe failed: {e}")

        rates = [self._sample_rate]
        if device_rate and abs(device_rate - self._sample_rate) > 1.0:
            rates.append(device_rate)
        bufsizes = [self._frames_per_buffer, _paFramesPerBufferUnspecified]

        def _try_open(sample_format: int, fmt_label: str
                      ) -> tuple[int, _ctypes.c_void_p, float, int]:
            """Try every (rate, bufsize, open-API) combination at one
            sample format. Returns (err, stream_ptr, chosen_rate,
            chosen_buf). err==0 on success.

            Three open layers:
              1. Pa_OpenDefaultStream (simple API, tight default latency)
              2. Pa_OpenStream + PaStreamParameters at the device's
                 defaultHighOutputLatency (more room for Core Audio to
                 renegotiate). Skipped if device probe failed.

            Before each Pa_OpenStream attempt, IsFormatSupported probes
            the format — both as a cleaner failure mode and because some
            macOS users on the PortAudio mailing list report that calling
            IsFormatSupported first "primes" the AudioUnit and resolves
            -10851 (kAudioUnitErr_InvalidPropertyValue) on subsequent
            opens.
            """
            local_stream = _ctypes.c_void_p()
            local_err = -1
            local_rate = self._sample_rate
            local_buf = self._frames_per_buffer

            # Layer 1: Pa_OpenDefaultStream matrix. Skipped when the user
            # picked an explicit device — Pa_OpenDefaultStream always opens
            # the *default* device, so we jump straight to Layer 2 (explicit
            # PaStreamParameters on the chosen device index).
            for rate in ([] if explicit else rates):
                for bufsz in bufsizes:
                    local_stream = _ctypes.c_void_p()
                    local_err = lib.Pa_OpenDefaultStream(
                        _ctypes.byref(local_stream),
                        0,                              # no input
                        self._channels,
                        sample_format,
                        float(rate),
                        int(bufsz),
                        _ctypes.cast(self._c_callback, _ctypes.c_void_p),
                        None,
                    )
                    if local_err == 0:
                        return (local_err, local_stream,
                                float(rate), int(bufsz))
                    msg = (lib.Pa_GetErrorText(local_err) or b"unknown"
                           ).decode(errors="replace")
                    bufsz_label = "auto" if bufsz == 0 else str(bufsz)
                    self._log(
                        f"[speaker_out] {fmt_label} "
                        f"Pa_OpenDefaultStream@{rate}Hz buf={bufsz_label} "
                        f"failed: {msg} (err={local_err})"
                        f"{_host_error_detail()}"
                    )

            # Layer 2: Pa_OpenStream with explicit PaStreamParameters at
            # defaultHighOutputLatency. Needs a working device probe.
            if device_index < 0:
                return local_err, local_stream, local_rate, local_buf
            self._log(
                f"[speaker_out] {fmt_label} falling back to Pa_OpenStream "
                f"+ defaultHighOutputLatency={device_high_latency:.4f}s"
            )
            for rate in rates:
                out_params = _PaStreamParameters(
                    device=device_index,
                    channelCount=self._channels,
                    sampleFormat=sample_format,
                    suggestedLatency=device_high_latency,
                    hostApiSpecificStreamInfo=None,
                )
                # IsFormatSupported probe. If it says no, log and skip
                # the Pa_OpenStream call entirely.
                supported = lib.Pa_IsFormatSupported(
                    None, _ctypes.byref(out_params), float(rate))
                if supported != _paFormatIsSupported:
                    smsg = (lib.Pa_GetErrorText(supported) or b"unknown"
                            ).decode(errors="replace")
                    self._log(
                        f"[speaker_out] {fmt_label} "
                        f"Pa_IsFormatSupported@{rate}Hz: {smsg} "
                        f"(err={supported}) — skipping"
                    )
                    continue
                for bufsz in bufsizes:
                    local_stream = _ctypes.c_void_p()
                    local_err = lib.Pa_OpenStream(
                        _ctypes.byref(local_stream),
                        None,                       # no input
                        _ctypes.byref(out_params),
                        float(rate),
                        int(bufsz),
                        _paNoFlag,
                        _ctypes.cast(self._c_callback, _ctypes.c_void_p),
                        None,
                    )
                    if local_err == 0:
                        return (local_err, local_stream,
                                float(rate), int(bufsz))
                    msg = (lib.Pa_GetErrorText(local_err) or b"unknown"
                           ).decode(errors="replace")
                    bufsz_label = "auto" if bufsz == 0 else str(bufsz)
                    self._log(
                        f"[speaker_out] {fmt_label} "
                        f"Pa_OpenStream@{rate}Hz buf={bufsz_label} "
                        f"failed: {msg} (err={local_err})"
                        f"{_host_error_detail()}"
                    )
            return local_err, local_stream, local_rate, local_buf

        # Outer loop: prefer float32 (lossless), fall back to int16 if
        # every float32 attempt fails. Some Core Audio devices reject
        # float32 even though PortAudio's docs say it should auto-convert.
        # int16 is the workaround; we convert in the pa_callback.
        # Gated on `err != 0` from step 1 — if the direct open already
        # succeeded, stream_ptr / chosen_* are already correctly set and
        # we skip the matrix entirely.
        if err != 0:
            formats = [
                (_paFloat32, "float32"),
                (_paInt16,   "int16"),
            ]
            for sample_format, fmt_label in formats:
                err, stream_ptr, chosen_rate, chosen_buf = _try_open(
                    sample_format, fmt_label)
                if err == 0:
                    chosen_format = sample_format
                    if sample_format == _paInt16:
                        self._log(
                            "[speaker_out] WARNING: opened with paInt16 "
                            "(float32 rejected by Core Audio). Headroom "
                            "drops ~3 dB; clipping is now hard at \xb11.0."
                        )
                    if chosen_rate != self._sample_rate:
                        self._log(
                            f"[speaker_out] WARNING: opened at {chosen_rate} Hz "
                            f"instead of {self._sample_rate} Hz. Audio will "
                            f"pitch by "
                            f"~{(chosen_rate / self._sample_rate - 1.0) * 100:+.2f}%. "
                            f"Set your default output device to "
                            f"{int(self._sample_rate)} Hz to fix "
                            f"(macOS: Audio MIDI Setup; Windows: Sound "
                            f"settings → device Properties → Advanced)."
                        )
                    if chosen_buf == _paFramesPerBufferUnspecified:
                        self._log(
                            "[speaker_out] using paFramesPerBufferUnspecified "
                            "(device negotiated its own block size)"
                        )
                    break

        if err != 0:
            self._log(
                "[speaker_out] could not open the output device (no usable "
                "rate / buffer / format / open-mode combination).\n"
                f"  Default output device: {self._describe_default_output()}\n"
                "  >>> First thing to try: SAVE, fully quit TouchDesigner, "
                "and reopen. The output-device selection can get into a bad "
                "state that a clean restart clears.\n"
                "  If it still fails, another app or an Audio Device Out CHOP "
                "in your project is holding the device:\n"
                "    * Close other apps using the device, or switch your "
                "system default output to another device, then re-pulse "
                "Connect.\n"
                "    * Or toggle 'Python Audio Out' OFF and wire the COMP's "
                "`out` to your own Audio Device Out CHOP (lets TD own the "
                "device instead of our Python/PortAudio path).\n"
                "  To confirm it's TD-side, run "
                "`python3 scripts/probe_portaudio.py` in a terminal — if that "
                "succeeds outside TD, something inside TD owns the device."
            )
            return False

        self._sample_format_pa = chosen_format
        err = lib.Pa_StartStream(stream_ptr)
        if err != 0:
            msg = lib.Pa_GetErrorText(err) or b"unknown"
            self._log(f"[speaker_out] Pa_StartStream failed: "
                      f"{msg.decode(errors='replace')}")
            lib.Pa_CloseStream(stream_ptr)
            return False
        self._stream = stream_ptr.value
        self._stream_ptr = stream_ptr
        self._sample_rate = chosen_rate
        self._frames_per_buffer = chosen_buf
        # When we opened with paFramesPerBufferUnspecified, PortAudio
        # decides per-callback what block size to deliver. Logging "auto"
        # is more honest than printing 0 as a frames-per-buffer count.
        buf_label = "auto" if chosen_buf == 0 else str(chosen_buf)
        latency_label = (
            "device-negotiated" if chosen_buf == 0
            else f"~{chosen_buf / self._sample_rate * 1000:.1f}ms"
        )
        fmt_label = "int16" if chosen_format == _paInt16 else "float32"
        self._log(
            f"[speaker_out] started PortAudio default stream "
            f"sr={self._sample_rate} ch={self._channels} "
            f"format={fmt_label} "
            f"frames_per_buffer={buf_label} (latency {latency_label})"
        )
        # Log WHICH device PortAudio actually opened. A "connected but no
        # audio" session is almost always the wrong default output device
        # / host API (esp. on Windows: MME vs WASAPI) — this makes it
        # visible instead of a guessing game. Done only AFTER a successful
        # open+start so the (macOS-sensitive) device probe can't poison the
        # AudioUnit before the stream exists.
        self._log(
            f"[speaker_out] output device: "
            f"{self._describe_default_output(self._device_index if self._device_index >= 0 else None)}")
        return True

    def set_device_index(self, index: int) -> None:
        """Choose which output device start() opens. -1 = system default.
        Takes effect on the next start() (the caller restarts the stream to
        switch live). No PortAudio calls here — safe from the main thread."""
        self._device_index = int(index)

    @classmethod
    def list_output_devices(cls, dylib_path: str | None = None,
                            log=print) -> list[dict]:
        """Enumerate output-capable PortAudio devices for the device picker.

        Returns a list of dicts:
          {index, name, host_api, max_out, default_sr, is_default}

        Brackets the probe with a balanced Pa_Initialize/Pa_Terminate so it
        leaves PortAudio's refcount exactly as it found it — important on
        macOS, where eager Pa_GetDeviceInfo calls can otherwise poison a
        later default-stream open. Never raises; returns [] on any failure.
        """
        lib = cls._load_lib(dylib_path, log=log)
        if lib is None:
            return []
        devices: list[dict] = []
        initialized = False
        try:
            if lib.Pa_Initialize() != 0:
                return []
            initialized = True
            count = int(lib.Pa_GetDeviceCount())
            try:
                default = int(lib.Pa_GetDefaultOutputDevice())
            except Exception:
                default = -1
            for i in range(max(0, count)):
                info_ptr = lib.Pa_GetDeviceInfo(i)
                if not info_ptr:
                    continue
                info = info_ptr.contents
                if int(info.maxOutputChannels) <= 0:
                    continue
                name = (info.name or b"?").decode(errors="replace")
                host = "?"
                try:
                    hptr = lib.Pa_GetHostApiInfo(int(info.hostApi))
                    if hptr:
                        h = _ctypes.cast(
                            hptr, _ctypes.POINTER(_PaHostApiInfo)).contents
                        host = (h.name or b"?").decode(errors="replace")
                except Exception:
                    pass
                devices.append({
                    "index": i,
                    "name": name,
                    "host_api": host,
                    "max_out": int(info.maxOutputChannels),
                    "default_sr": float(info.defaultSampleRate),
                    "is_default": (i == default),
                })
        except Exception as e:
            log(f"[speaker_out] list_output_devices failed: "
                f"{type(e).__name__}: {e}")
        finally:
            # Balance our Pa_Initialize. PortAudio refcounts, so this only
            # actually terminates if nothing else holds it — it never tears
            # down a live stream (start() holds its own init).
            if initialized:
                try:
                    lib.Pa_Terminate()
                except Exception:
                    pass
        return devices

    def _describe_default_output(self, index: int | None = None) -> str:
        """Best-effort one-line description of an output device (index,
        name, host API, channels, sample rate). `index=None` means the
        system default. Purely diagnostic; returns a '?' string on any
        failure and never raises."""
        lib = SpeakerOut._lib
        if lib is None:
            return "?"
        try:
            dev = int(lib.Pa_GetDefaultOutputDevice()) if index is None \
                else int(index)
            if dev < 0:
                return f"none (Pa_GetDefaultOutputDevice={dev})"
            info_ptr = lib.Pa_GetDeviceInfo(dev)
            if not info_ptr:
                return f"dev={dev} (no device info)"
            info = info_ptr.contents
            name = (info.name or b"?").decode(errors="replace")
            host = "?"
            try:
                hptr = lib.Pa_GetHostApiInfo(int(info.hostApi))
                if hptr:
                    h = _ctypes.cast(
                        hptr, _ctypes.POINTER(_PaHostApiInfo)).contents
                    host = (h.name or b"?").decode(errors="replace")
            except Exception:
                pass
            return (f"dev={dev} name={name!r} hostApi={host!r} "
                    f"maxOut={int(info.maxOutputChannels)} "
                    f"defaultSampleRate={float(info.defaultSampleRate):g}")
        except Exception as e:
            return f"? (probe failed: {type(e).__name__}: {e})"

    def stop(self) -> None:
        lib = SpeakerOut._lib
        stream = self._stream
        self._stream = None
        if stream is None or lib is None:
            return
        try:
            lib.Pa_StopStream(_ctypes.c_void_p(stream))
            lib.Pa_CloseStream(_ctypes.c_void_p(stream))
            self._log(f"[speaker_out] stopped (cb_count={self._callback_count} "
                      f"underruns={self._underrun_count})")
        except Exception as e:
            self._log(f"[speaker_out] stop failed: {e}")
        self._stream_ptr = None

    def set_paused(self, paused: bool) -> None:
        """Mark the stream as paused/resumed without touching PortAudio.

        When True, `_pa_callback` emits silence into the output buffer
        and does NOT advance the LoopBuffer playhead — so when un-
        paused, audio resumes exactly where it left off. Cheaper +
        cleaner than calling Pa_StopStream/StartStream (which can click
        on resume on some macOS Core Audio paths).

        Driven by `DemonExt.OnPlayStateChange` whenever TD's timeline
        play state changes. Safe to call from the main thread — the
        flag read in `_pa_callback` is a single bool, GIL-atomic.
        """
        self._paused = bool(paused)

    def _pa_callback(self, in_buf, out_buf, frames, time_info, status_flags,
                     user_data) -> int:
        """PortAudio callback (audio thread).

        Zero allocations in the steady-state path:
          * `_scratch_pcm[:, :n]` is filled by `LoopBuffer.read_into`
            (no allocation — see LoopBuffer.read_into docstring).
          * For float32 output, we view `_scratch_interleaved_f32` as
            `(n, channels)` and `np.copyto` from the transposed pcm
            view (transpose is a strided view, free; copyto memcpy's
            into the contiguous interleaved buffer).
          * For int16 output, we clip + multiply in place inside
            `_scratch_pcm[:, :n]`, then cast into the interleaved
            int16 scratch.
          * memmove copies the interleaved scratch into PortAudio's
            output buffer.
        No `np.zeros`, no `np.ascontiguousarray`, no implicit numpy
        temporaries → no GC pressure on the audio thread.

        `self._sample_format_pa` records the format we negotiated by
        `start()`. float32 is the preferred path; int16 is the degraded
        fallback when Core Audio refuses float32.
        """
        # Microsecond-resolution latency measurement. `perf_counter_ns`
        # is a C-level call, no allocation, and the audio thread can
        # call it freely.
        t0 = time.perf_counter_ns()
        self._callback_count += 1
        self._cb_count_since_drain += 1

        if status_flags & _paOutputUnderflow:
            # Counter only — NEVER log from the audio thread. An f-string
            # + print here is blocking I/O at exactly the moment the
            # callback is already late, which turns one underrun into a
            # cascade. The main thread reports these via
            # drain_latency_stats() (underruns_since_drain).
            self._underrun_count += 1

        n = int(frames)
        is_int16 = (self._sample_format_pa == _paInt16)
        bytes_per_sample = 2 if is_int16 else 4
        n_bytes = n * self._channels * bytes_per_sample

        # TD-pause fast path: emit silence + skip the LoopBuffer read so
        # the playhead doesn't advance. When the user un-pauses, audio
        # resumes from the same sample. Cheaper than Pa_StopStream and
        # avoids the click some Core Audio paths produce on stop/start.
        if self._paused:
            try:
                _ctypes.memset(out_buf, 0, n_bytes)
            except Exception:
                pass
            elapsed_ns = time.perf_counter_ns() - t0
            self._cb_latency_sum_ns += elapsed_ns
            if elapsed_ns > self._cb_latency_max_ns:
                self._cb_latency_max_ns = elapsed_ns
            return _paContinue

        try:
            # Unified chunked fill: produce the block through the
            # pre-allocated scratch in <= _max_block_frames pieces.
            # The normal case (n <= _max_block_frames) is the single-pass
            # degenerate loop; an outsized callback (device chose a
            # bigger block than we planned for) walks the output buffer
            # in scratch-sized chunks. Sequential read_into calls are
            # bit-identical to one big read — the seam crossfade is
            # position-based, not call-based. Zero allocations either
            # way: the old fallback called `self._loop.read(n)` (a GC
            # hazard on the audio thread) and then raised on the
            # too-small interleave scratch, playing the block as
            # SILENCE. Now it just takes extra passes.
            if n > self._max_block_frames:
                self._cb_over_max_block = True
            bytes_per_frame = self._channels * bytes_per_sample
            dst = out_buf
            remaining = n
            while remaining > 0:
                m = remaining if remaining <= self._max_block_frames \
                    else self._max_block_frames
                pcm_view = self._scratch_pcm[:, :m]
                self._loop.read_into(pcm_view)
                if is_int16:
                    # In-place clip + scale → pcm_view holds float32
                    # values in [-32767, 32767] range. The explicit clip
                    # is what makes the unsafe float32→int16 cast below
                    # safe (no wraparound on out-of-range floats).
                    np.clip(pcm_view, -1.0, 1.0, out=pcm_view)
                    np.multiply(pcm_view, 32767.0, out=pcm_view)
                    view_i16 = self._scratch_interleaved_i16[
                        :m * self._channels].reshape(m, self._channels)
                    np.copyto(view_i16, pcm_view.T, casting="unsafe")
                    _ctypes.memmove(
                        dst,
                        self._scratch_interleaved_i16.ctypes.data,
                        m * bytes_per_frame,
                    )
                else:
                    # float32 interleave: copy pcm_view.T (a non-
                    # contiguous strided view) into the contiguous
                    # interleaved scratch.
                    view_f32 = self._scratch_interleaved_f32[
                        :m * self._channels].reshape(m, self._channels)
                    np.copyto(view_f32, pcm_view.T)
                    _ctypes.memmove(
                        dst,
                        self._scratch_interleaved_f32.ctypes.data,
                        m * bytes_per_frame,
                    )
                dst += m * bytes_per_frame
                remaining -= m
        except Exception:
            # Any failure: write silence so the audio thread doesn't break.
            try:
                _ctypes.memset(out_buf, 0, n_bytes)
            except Exception:
                pass

        elapsed_ns = time.perf_counter_ns() - t0
        self._cb_latency_sum_ns += elapsed_ns
        if elapsed_ns > self._cb_latency_max_ns:
            self._cb_latency_max_ns = elapsed_ns
        return _paContinue

    def drain_latency_stats(self) -> dict | None:
        """Read + reset the audio-thread latency counters. Called by the
        main TD thread (~once per second) to publish a telemetry line.

        Returns None if no callbacks have fired since the last drain —
        avoids spamming idle ticks. Otherwise returns a dict with
        sample count, mean / max latency in milliseconds, and any
        over-max-block warning state.
        """
        n = self._cb_count_since_drain
        if n == 0:
            return None
        sum_ns = self._cb_latency_sum_ns
        max_ns = self._cb_latency_max_ns
        over = self._cb_over_max_block
        # Reset before computing — short race window with the audio
        # thread but the worst case is one missed sample per drain.
        self._cb_count_since_drain = 0
        self._cb_latency_sum_ns = 0
        self._cb_latency_max_ns = 0
        self._cb_over_max_block = False
        underruns_total = self._underrun_count
        underruns_since = underruns_total - self._underruns_at_last_drain
        self._underruns_at_last_drain = underruns_total
        return {
            "n": n,
            "mean_ms": (sum_ns / n) / 1_000_000.0,
            "max_ms": max_ns / 1_000_000.0,
            "over_max_block": over,
            "underruns_total": underruns_total,
            "underruns_since_drain": underruns_since,
        }

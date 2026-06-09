"""Tests for SpeakerOut._pa_callback — the PortAudio audio-thread hot path.

SpeakerOut.__init__ never touches the PortAudio dylib (that happens in
start()), so we can construct one and invoke _pa_callback directly with a
ctypes output buffer, exactly as PortAudio would.

Covers the v0.2.16 fixes:
  * underruns increment a counter but NEVER log from the audio thread
  * oversized callbacks (frames > _max_block_frames) produce CORRECT audio
    via the chunked scratch path — previously that branch allocated AND
    raised on a too-small interleave scratch, playing the block as silence
  * drain_latency_stats reports underruns_since_drain deltas
"""

import ctypes

import numpy as np
import pytest

import audio
from audio import LoopBuffer, SpeakerOut


SR = 48000
CH = 2


def _make_loop(frames: int = SR) -> LoopBuffer:
    """LoopBuffer initialized with a deterministic full-scale-ish ramp."""
    loop = LoopBuffer(sample_rate=SR)
    rng = np.random.default_rng(1234)
    pcm = (rng.standard_normal(frames * CH) * 0.5).astype(np.float32)
    loop.init(pcm, channels=CH)
    return loop


def _make_speaker(loop: LoopBuffer, log=None):
    return SpeakerOut(loop, sample_rate=SR, channels=CH,
                      log=log if log is not None else (lambda m: None))


def _invoke(spk: SpeakerOut, frames: int, status_flags: int = 0):
    """Call _pa_callback with a real ctypes output buffer; return raw bytes."""
    is_int16 = spk._sample_format_pa == audio._paInt16
    bps = 2 if is_int16 else 4
    n_bytes = frames * CH * bps
    out = (ctypes.c_char * n_bytes)()
    rc = spk._pa_callback(None, ctypes.addressof(out), frames,
                          None, status_flags, None)
    assert rc == audio._paContinue
    return bytes(out)


def _expected_f32(loop_src: np.ndarray, frames: int) -> np.ndarray:
    """Reference output: a parallel LoopBuffer read in ONE big read()."""
    ref = LoopBuffer(sample_rate=SR)
    ref.init(loop_src.copy(), channels=CH)
    pcm = ref.read(frames)  # (CH, frames)
    return np.ascontiguousarray(pcm.T).reshape(-1)  # interleaved


def test_underrun_increments_counter_without_logging():
    loop = _make_loop()
    logged = []
    spk = _make_speaker(loop, log=logged.append)
    _invoke(spk, 4096, status_flags=audio._paOutputUnderflow)
    _invoke(spk, 4096, status_flags=audio._paOutputUnderflow)
    assert spk.underrun_count == 2
    assert logged == []  # NEVER log from the audio thread


def test_normal_callback_outputs_loop_audio():
    frames = 4096
    rng = np.random.default_rng(99)
    src = (rng.standard_normal(SR * CH) * 0.5).astype(np.float32)
    loop = LoopBuffer(sample_rate=SR)
    loop.init(src.copy(), channels=CH)
    spk = _make_speaker(loop)

    raw = _invoke(spk, frames)
    got = np.frombuffer(raw, dtype=np.float32)
    want = _expected_f32(src, frames)
    np.testing.assert_array_equal(got, want)


def test_oversized_callback_outputs_correct_audio_not_silence():
    """Regression: frames > _max_block_frames used to play as SILENCE
    (fallback alloc + ValueError on the undersized interleave scratch,
    swallowed by the except). The chunked path must produce the same
    bytes as one big read."""
    rng = np.random.default_rng(7)
    src = (rng.standard_normal(SR * 2 * CH) * 0.5).astype(np.float32)
    loop = LoopBuffer(sample_rate=SR)
    loop.init(src.copy(), channels=CH)
    spk = _make_speaker(loop)
    frames = spk._max_block_frames + 4096  # forces multi-chunk path

    raw = _invoke(spk, frames)
    got = np.frombuffer(raw, dtype=np.float32)
    want = _expected_f32(src, frames)
    np.testing.assert_array_equal(got, want)
    assert spk._cb_over_max_block is True
    assert np.abs(got).max() > 0.0  # loudly assert: NOT silence


def test_oversized_callback_never_calls_allocating_read():
    loop = _make_loop()
    spk = _make_speaker(loop)

    def _boom(n):  # pragma: no cover - must never run
        raise AssertionError("allocating LoopBuffer.read() called "
                             "from the audio callback")

    loop.read = _boom
    raw = _invoke(spk, spk._max_block_frames + 4096)
    got = np.frombuffer(raw, dtype=np.float32)
    assert np.abs(got).max() > 0.0


def test_oversized_callback_int16_parity():
    rng = np.random.default_rng(42)
    src = (rng.standard_normal(SR * 2 * CH) * 1.2).astype(np.float32)  # clips
    loop = LoopBuffer(sample_rate=SR)
    loop.init(src.copy(), channels=CH)
    spk = _make_speaker(loop)
    spk._sample_format_pa = audio._paInt16
    frames = spk._max_block_frames + 1000

    raw = _invoke(spk, frames)
    got = np.frombuffer(raw, dtype=np.int16)
    want_f32 = _expected_f32(src, frames)
    want = (np.clip(want_f32, -1.0, 1.0) * 32767.0).astype(np.int16)
    np.testing.assert_array_equal(got, want)


def test_paused_callback_emits_silence_and_holds_position():
    loop = _make_loop()
    spk = _make_speaker(loop)
    spk.set_paused(True)
    pos_before = loop.position
    raw = _invoke(spk, 4096)
    assert raw == b"\x00" * len(raw)
    assert loop.position == pos_before


def test_drain_latency_stats_underrun_delta():
    loop = _make_loop()
    spk = _make_speaker(loop)
    _invoke(spk, 1024, status_flags=audio._paOutputUnderflow)
    _invoke(spk, 1024, status_flags=audio._paOutputUnderflow)
    stats = spk.drain_latency_stats()
    assert stats["underruns_total"] == 2
    assert stats["underruns_since_drain"] == 2

    _invoke(spk, 1024)  # no underrun this time
    stats = spk.drain_latency_stats()
    assert stats["underruns_total"] == 2
    assert stats["underruns_since_drain"] == 0

    # No callbacks since last drain → None (don't spam idle ticks).
    assert spk.drain_latency_stats() is None

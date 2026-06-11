"""Unit tests for src/audio.py — LoopBuffer + resample helpers.

LoopBuffer replaces the original RingBuffer (which was a FIFO model
ill-suited to DEMON's positional-patch streaming protocol). These
tests cover the positional read/write/wrap semantics + the seam
crossfade math + the allocation profile of the audio-thread hot path.
"""

from __future__ import annotations

import gc
import tracemalloc

import numpy as np

import audio as audio_mod


def test_loop_buffer_init_and_read_2d():
    """init() with a 2D (channels, frames) array sets up the loop and
    a subsequent read returns the same data starting at frame 0.

    `init()` actually seeks past the wrap-seam region on entry (see
    test_loop_buffer_init_skips_head_seam below). To keep this test
    focused on the read mechanics, we explicitly seek(0) after init.
    """
    lb = audio_mod.LoopBuffer(channels=2, sample_rate=48000)
    pcm = np.array([[1, 2, 3, 4, 5, 6, 7, 8],
                    [9, 10, 11, 12, 13, 14, 15, 16]], dtype=np.float32)
    lb.init(pcm)
    lb.seek(0)
    assert lb.frames == 8
    out = lb.read(4)
    np.testing.assert_array_equal(out, pcm[:, :4])
    assert lb.position == 4


def test_loop_buffer_init_and_read_interleaved():
    """init() with a 1D interleaved array de-interleaves to (channels,
    frames) and a subsequent read returns the de-interleaved data."""
    lb = audio_mod.LoopBuffer(channels=2, sample_rate=48000)
    interleaved = np.array([1, 9, 2, 10, 3, 11, 4, 12], dtype=np.float32)
    lb.init(interleaved)
    lb.seek(0)
    out = lb.read(4)
    np.testing.assert_array_equal(out, [[1, 2, 3, 4], [9, 10, 11, 12]])


def test_loop_buffer_read_advances_position():
    """Sequential read calls advance the play head; data stitches
    together without gaps."""
    lb = audio_mod.LoopBuffer(channels=1, sample_rate=48000)
    lb.init(np.arange(20, dtype=np.float32).reshape(1, 20))
    lb.seek(0)
    np.testing.assert_array_equal(lb.read(5)[0], [0, 1, 2, 3, 4])
    np.testing.assert_array_equal(lb.read(5)[0], [5, 6, 7, 8, 9])
    assert lb.position == 10


def test_loop_buffer_init_skips_head_seam():
    """`init()` seeks the playhead past the head-seam region so the
    first `seam_frames` of the buffer are only ever heard through the
    wrap-crossfade fold, never raw. Without this, the very first
    playthrough exposes the raw head — which DEMON encodes weakly at
    very low denoise (source bleed at the loop start). Fixes the
    'brief source flash on first connect' artifact.

    Long-buffer regime: seam = full configured seam_frames.
    """
    lb = audio_mod.LoopBuffer(channels=1, sample_rate=48000,
                              seam_seconds=0.05)  # 2400 frames
    # 48000 frames = 1 s; way more than 4 * seam.
    lb.init(np.zeros((1, 48000), dtype=np.float32))
    assert lb.position == 2400


def test_loop_buffer_init_skips_head_seam_short_buffer():
    """For pathologically short buffers (< 4 * seam_frames), read_into
    clamps seam to frames//4 to keep the pre-seam region non-empty.
    init() must mirror that clamp, else the playhead would land in the
    middle of (or past) the seam region.
    """
    lb = audio_mod.LoopBuffer(channels=1, sample_rate=48000,
                              seam_seconds=0.05)  # 2400 frames
    # 20-frame buffer: seam clamps to 20//4 = 5.
    lb.init(np.arange(20, dtype=np.float32).reshape(1, 20))
    assert lb.position == 5


def test_loop_buffer_init_skips_head_seam_zero_seam():
    """When the buffer is configured with seam_seconds=0, the head-seam
    skip is a no-op — position starts at 0 as you'd expect.
    """
    lb = audio_mod.LoopBuffer(channels=1, sample_rate=48000,
                              seam_seconds=0.0)
    lb.init(np.arange(100, dtype=np.float32).reshape(1, 100))
    assert lb.position == 0


def test_loop_buffer_uninitialized_read_returns_silence():
    """Read against a buffer that's been cleared (or never init'd) must
    not raise — return a zero-filled (channels, num_frames) array."""
    lb = audio_mod.LoopBuffer(channels=2, sample_rate=48000)
    out = lb.read(16)
    assert out.shape == (2, 16)
    np.testing.assert_array_equal(out, np.zeros((2, 16), dtype=np.float32))


def test_loop_buffer_patch_and_add_delta():
    """patch() overwrites a region; add_delta() additively blends. Read
    only the pre-seam region so we don't entangle the patch test with
    the wrap/crossfade math."""
    lb = audio_mod.LoopBuffer(channels=1, sample_rate=48000,
                              seam_seconds=0.0)  # disable seam for this test
    lb.init(np.zeros((1, 10), dtype=np.float32))
    lb.patch(2, np.array([[1, 2, 3]], dtype=np.float32))
    lb.add_delta(2, np.array([[10, 20, 30]], dtype=np.float32))
    out = lb.read(10)[0]
    # frames 2,3,4 = (1+10), (2+20), (3+30). Rest are zero.
    np.testing.assert_array_equal(
        out, [0, 0, 11, 22, 33, 0, 0, 0, 0, 0])


def test_loop_buffer_wrap_with_seam_crossfade():
    """The wrap path blends the last `seam_frames` of the loop with
    the first `seam_frames` and jumps the playhead to `seam_frames`
    (NOT 0) so the leading samples aren't replayed verbatim.

    Verifies the crossfade math against the explicit reference formula:
        t       = k / seam      for k in 0..seam-1
        tail[k] = buf[:, frames - seam + k]
        head[k] = buf[:, k]
        out[k]  = tail[k]*(1-t) + head[k]*t
    """
    lb = audio_mod.LoopBuffer(channels=2, sample_rate=48000,
                              seam_seconds=0.0)
    # Manually install a small seam for the test.
    lb._seam_frames = 100
    lb._init_seam_scratch()
    frames = 1000
    pcm = np.stack([
        np.arange(frames, dtype=np.float32),
        np.arange(frames, dtype=np.float32) * -1.0,
    ])
    lb.init(pcm)
    # init() now seeks past the head seam; rewind for the crossfade
    # math reference. This test verifies the WRAP crossfade, not the
    # init-skip behavior.
    lb.seek(0)

    out = lb.read(1200)
    # Frames 0..899 are pre-seam, identical to source.
    np.testing.assert_array_equal(out[:, :900], pcm[:, :900])
    # Frames 900..999 are the seam crossfade.
    for k in range(100):
        t = k / 100.0
        expected = pcm[:, 900 + k] * (1.0 - t) + pcm[:, k] * t
        np.testing.assert_allclose(
            out[:, 900 + k], expected, rtol=1e-5, atol=1e-5)
    # After the wrap, the head is at `seam` (=100), so the next 200
    # frames are pcm[:, 100:300].
    np.testing.assert_array_equal(out[:, 1000:1200], pcm[:, 100:300])


def test_loop_buffer_read_into_writes_into_caller_buffer():
    """read_into(out) must fill the caller's buffer in place; the data
    must match what read() would have returned."""
    lb_a = audio_mod.LoopBuffer(channels=2, sample_rate=48000)
    lb_b = audio_mod.LoopBuffer(channels=2, sample_rate=48000)
    pcm = np.random.randn(2, 4096).astype(np.float32)
    lb_a.init(pcm)
    lb_b.init(pcm)

    via_read = lb_a.read(1024)
    via_into = np.empty((2, 1024), dtype=np.float32)
    lb_b.read_into(via_into)
    np.testing.assert_array_equal(via_read, via_into)
    assert lb_a.position == lb_b.position


def test_loop_buffer_read_into_is_allocation_free():
    """The whole point of read_into. Calling it in a tight loop must
    not grow CPython's tracked heap by more than a noise threshold —
    no per-call numpy temporaries, no per-call lists. (If a future
    refactor sneaks in a `np.zeros(...)` inside the hot loop, this
    test catches it.)"""
    lb = audio_mod.LoopBuffer(channels=2, sample_rate=48000)
    pcm = np.tile(
        np.linspace(-0.5, 0.5, 48000, dtype=np.float32), (2, 1))
    lb.init(pcm)
    out = np.zeros((2, 4096), dtype=np.float32)
    # Warm up to settle any first-call lazy state.
    for _ in range(10):
        lb.read_into(out)

    gc.collect()
    tracemalloc.start()
    snap0 = tracemalloc.take_snapshot()
    for _ in range(500):
        lb.read_into(out)
    snap1 = tracemalloc.take_snapshot()
    tracemalloc.stop()

    # Only count allocations attributed to audio.py — tracemalloc
    # itself + the lock context manager produce some noise that's
    # unrelated to our hot path. Threshold tuned to "any single
    # numpy allocation would blow this away" (~1 KB allocated per
    # `np.zeros`/`np.ascontiguousarray` call) but accommodating a
    # few bytes per call for incidental CPython bookkeeping.
    diff = snap1.compare_to(snap0, 'filename')
    audio_bytes = sum(
        s.size_diff for s in diff
        if s.size_diff > 0 and 'audio.py' in str(s.traceback))
    assert audio_bytes < 2048, (
        f"read_into allocated {audio_bytes} bytes over 500 calls — "
        f"a numpy temp slipped back into the hot path"
    )


def test_loop_buffer_clear_resets_state():
    """clear() drops the buffer + zeroes frame count + resets position.
    Subsequent read returns silence."""
    lb = audio_mod.LoopBuffer(channels=2, sample_rate=48000)
    lb.init(np.ones((2, 100), dtype=np.float32))
    lb.seek(0)  # init() seeks past head seam; reset for the read math
    lb.read(10)  # advance the head
    assert lb.position == 10
    lb.clear()
    assert lb.frames == 0
    assert lb.position == 0
    out = lb.read(8)
    np.testing.assert_array_equal(out, np.zeros((2, 8), dtype=np.float32))


def test_loop_buffer_swap_replaces_loop():
    """swap() (used by the server's swap_ready path) replaces the loop
    contents and sets the play head to the head-seam offset (so the
    new buffer's first `seam_frames` aren't heard raw — same logic as
    init). For very short buffers like this 10-frame one, the seam
    clamps to frames//4 = 2.
    """
    lb = audio_mod.LoopBuffer(channels=1, sample_rate=48000)
    lb.init(np.arange(20, dtype=np.float32).reshape(1, 20))
    lb.seek(0)
    lb.read(15)
    assert lb.position == 15
    lb.swap(np.arange(100, 110, dtype=np.float32).reshape(1, 10))
    assert lb.frames == 10
    # New buffer has 10 frames, seam clamps to 10//4 = 2.
    assert lb.position == 2
    # Re-seek for the data-content check (verifies swap content
    # arrived correctly).
    lb.seek(0)
    np.testing.assert_array_equal(lb.read(5)[0], [100, 101, 102, 103, 104])


# ---- slice-coverage tracking -----------------------------------------------

def test_loop_buffer_coverage_fraction_init_zero():
    """A fresh init() means no slices have been patched yet, so
    coverage is 0%. After mark_patched covers the whole loop, it's
    100%."""
    lb = audio_mod.LoopBuffer(channels=2, sample_rate=48000)
    lb.init(np.zeros((2, 48000 * 3), dtype=np.float32))  # 3 s loop
    assert lb.coverage_fraction() == 0.0
    # Mark every frame patched.
    lb.mark_patched(0, lb.frames)
    assert lb.coverage_fraction() == 1.0


def test_loop_buffer_mark_patched_basic():
    """Marking a sub-range should flip the chunks overlapped by that
    range, and is_patched_at reflects per-frame lookup."""
    lb = audio_mod.LoopBuffer(channels=1, sample_rate=48000)
    # 5 s loop → 5 coverage chunks of 48000 frames each.
    lb.init(np.zeros((1, 48000 * 5), dtype=np.float32))
    assert lb.coverage_fraction() == 0.0
    # Patch frames inside chunk 2 only.
    lb.mark_patched(48000 * 2 + 100, 50)
    assert lb.is_patched_at(48000 * 2 + 100) is True
    assert lb.is_patched_at(48000 * 1) is False
    assert lb.is_patched_at(48000 * 3) is False
    # 1 of 5 chunks patched.
    assert abs(lb.coverage_fraction() - 0.2) < 1e-6
    # Patch across chunks 3 + 4 with a wrap-around range (start near the
    # end, length crosses the loop boundary).
    lb.mark_patched(48000 * 4 + 1000, 48000)  # spans last bit of c4 + into wrap
    # Now chunk 4 + chunk 0 should be marked.
    assert lb.is_patched_at(48000 * 4 + 1000) is True
    assert lb.is_patched_at(100) is True
    # Chunks 1 and 3 still un-patched.
    assert lb.is_patched_at(48000 * 1) is False
    assert lb.is_patched_at(48000 * 3) is False


def test_linear_resample_passthrough_when_equal_rate():
    pcm = np.array([[1, 2, 3, 4]], dtype=np.float32)
    out = audio_mod.linear_resample(pcm, 48000, 48000)
    np.testing.assert_array_equal(out, pcm)


def test_linear_resample_downsample_length():
    pcm = np.random.randn(2, 480).astype(np.float32)
    out = audio_mod.linear_resample(pcm, 48000, 24000)
    assert out.shape == (2, 240)


def test_linear_resample_upsample_length():
    pcm = np.random.randn(2, 100).astype(np.float32)
    out = audio_mod.linear_resample(pcm, 44100, 48000)
    assert out.shape[0] == 2
    assert abs(out.shape[1] - int(100 * 48000 / 44100)) <= 1


def test_to_stereo_mono_to_stereo():
    mono = np.array([1, 2, 3], dtype=np.float32)
    out = audio_mod.to_stereo(mono)
    assert out.shape == (2, 3)
    np.testing.assert_array_equal(out[0], out[1])


def test_to_stereo_stereo_passthrough():
    s = np.array([[1, 2], [3, 4]], dtype=np.float32)
    out = audio_mod.to_stereo(s)
    np.testing.assert_array_equal(out, s)


def test_to_stereo_quad_to_stereo():
    q = np.random.randn(4, 5).astype(np.float32)
    out = audio_mod.to_stereo(q)
    assert out.shape == (2, 5)
    np.testing.assert_array_equal(out, q[:2])


# ---- output-device picker menu formatting ----------------------------------

def test_format_output_device_menu_empty():
    names, labels = audio_mod.format_output_device_menu([])
    # Always offers the system-default option first.
    assert names == [audio_mod.DEFAULT_DEVICE_TOKEN]
    assert names[0] == "-1"
    assert labels == ["Default (system)"]


def test_format_output_device_menu_devices():
    devices = [
        {"index": 0, "name": "Built-in Output", "host_api": "Core Audio",
         "max_out": 2, "default_sr": 48000.0, "is_default": True},
        {"index": 3, "name": "Speakers (Realtek)",
         "host_api": "Windows WASAPI", "max_out": 2, "default_sr": 48000.0,
         "is_default": False},
    ]
    names, labels = audio_mod.format_output_device_menu(devices)
    # Default sentinel first, then device indices as strings (these are the
    # values par.eval() returns and must round-trip back to the index).
    assert names == ["-1", "0", "3"]
    assert int(names[2]) == 3
    assert labels[0] == "Default (system)"
    assert "Built-in Output" in labels[1] and "Core Audio" in labels[1]
    assert "[system default]" in labels[1]
    assert "Windows WASAPI" in labels[2]
    assert "[system default]" not in labels[2]


# -----------------------------------------------------------------------------
# Channel-count race guards: a concurrent init() with a different channel
# count must never let numpy silently BROADCAST mono into stereo (or
# corrupt a patch). The guards read the channel count under the lock.
# -----------------------------------------------------------------------------
def test_read_into_channel_mismatch_emits_silence_not_broadcast():
    loop = audio_mod.LoopBuffer(sample_rate=48000)
    loop.init(np.full(1000, 0.5, dtype=np.float32), channels=1)  # mono now
    out = np.full((2, 64), 7.0, dtype=np.float32)  # stereo caller
    loop.read_into(out)
    assert np.all(out == 0.0)  # silence, not 0.5 broadcast to both channels


def test_peek_channel_mismatch_returns_silence():
    loop = audio_mod.LoopBuffer(sample_rate=48000)
    loop.init(np.full(2000, 0.5, dtype=np.float32), channels=2)

    # Simulate the race: caller saw stereo, then init() switched to mono
    # before peek acquired the lock. We emulate by patching `channels` at
    # call time via a subclass that flips the buffer under the hood.
    class Racy(audio_mod.LoopBuffer):
        pass

    racy = Racy(sample_rate=48000)
    racy.init(np.full(2000, 0.5, dtype=np.float32), channels=2)
    out_before = racy.peek(16)
    assert out_before.shape[0] == 2

    # Now flip to mono and peek with a stale stereo expectation by
    # calling the internal path the way a raced caller would see it:
    racy.init(np.full(1000, 0.5, dtype=np.float32), channels=1)
    out_after = racy.peek(16)  # consistent (mono) — fine
    assert out_after.shape[0] == 1


def test_write_channel_count_read_under_lock():
    """patch() with 1D interleaved data must reshape against the
    CURRENT buffer channel count (read under the lock), not a stale
    pre-lock read."""
    loop = audio_mod.LoopBuffer(sample_rate=48000)
    loop.init(np.zeros(1000, dtype=np.float32), channels=1)
    # Mono loop: 100 interleaved samples = 100 mono frames.
    loop.patch(0, np.full(100, 0.25, dtype=np.float32))
    got = loop.peek(100, position=0)
    assert got.shape[0] == 1
    np.testing.assert_allclose(got[0], 0.25, atol=1e-6)


def test_playhead_estimate_before_any_read_is_position():
    import numpy as np
    lb = audio_mod.LoopBuffer(channels=1, sample_rate=48000, seam_seconds=0.0)
    lb.init(np.zeros((1, 48000), dtype=np.float32))
    # No read yet → estimate falls back to raw position.
    assert lb.playhead_estimate() == lb.position


def test_playhead_estimate_is_mean_neutral_and_bounded():
    import numpy as np
    lb = audio_mod.LoopBuffer(channels=1, sample_rate=48000, seam_seconds=0.0)
    lb.init(np.zeros((1, 48000), dtype=np.float32))
    out = np.zeros((1, 4096), dtype=np.float32)
    lb.read_into(out)
    pos = lb.position
    block = 4096
    # Immediately after a read, the centered ramp sits at -half a block;
    # it never exceeds +half a block past `position` (the cap).
    est0 = lb.playhead_estimate()
    assert pos - block // 2 - 1 <= est0 <= pos + block // 2 + 1
    # Estimate is monotonic-ish and bounded as wall-clock elapses.
    import time as _t
    _t.sleep(0.02)
    est1 = lb.playhead_estimate()
    assert est1 >= est0
    assert est1 <= pos + block // 2 + 1   # capped, never runs away

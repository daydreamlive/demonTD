"""Tests for src/binary_router.py — recv-thread binary routing.

Builds protocol-accurate frames: ready/swap_ready/stem_assets JSON text,
a headerless float16 initial buffer, and 23-byte-header slices (RAW and
zstd DELTA where zstandard is importable).
"""

import json
import struct

import numpy as np
import pytest

import wire
from audio import LoopBuffer
from binary_router import BinaryRouter
from telemetry import SmoothnessStats

try:
    import zstandard as zstd
except Exception:  # pragma: no cover
    zstd = None

SR = wire.SAMPLE_RATE
CH = 2


def _initial_buffer_bytes(frames: int, value: float = 0.25) -> bytes:
    pcm = np.full(frames * CH, value, dtype=np.float16)
    return pcm.tobytes()


def _slice_bytes(start_sample: int, pcm_f32: np.ndarray, *,
                 flags: int = wire.SLICE_FLAG_RAW,
                 channels: int = CH) -> bytes:
    """23-byte header + float16 payload (zstd-compressed for DELTA)."""
    payload = pcm_f32.astype(np.float16).tobytes()
    if flags == wire.SLICE_FLAG_DELTA:
        assert zstd is not None
        payload = zstd.ZstdCompressor().compress(payload)
    n = pcm_f32.size // channels
    hdr = (struct.pack("<B", flags)
           + struct.pack("<II", start_sample, n)
           + struct.pack("<H", channels)
           + struct.pack("<ff", 1.0, 2.0)
           + struct.pack("<I", 1))
    assert len(hdr) == wire.SLICE_HDR_SIZE
    return hdr + payload


def _make_router(ring=None, stats=None, zstd_dec=None, debug=False):
    ring = ring if ring is not None else LoopBuffer(sample_rate=SR)
    events = []
    logs = []
    router = BinaryRouter(
        ring=ring,
        post_event=lambda k, p: events.append((k, p)),
        stats=stats,
        zstd_dec=zstd_dec,
        log=logs.append,
        is_debug=lambda: debug,
    )
    return router, ring, events, logs


def _ready_msg(channels=CH):
    return json.dumps({"type": "ready", "channels": channels,
                       "sample_rate": SR, "duration": 1.0})


# -- ready → initial → slices --------------------------------------------------


def test_ready_then_initial_inits_ring_and_posts_event():
    router, ring, events, _ = _make_router()
    router.sniff_text(_ready_msg())
    router.handle_binary(_initial_buffer_bytes(SR))
    assert ring.frames == SR
    assert events == [("loop-initialized",
                       {"frames": SR, "channels": CH, "bytes": SR * CH * 2})]


def test_binary_before_ready_is_treated_as_slice_not_initial():
    router, ring, events, logs = _make_router()
    # No ready sniffed — a headerless blob must NOT init the ring.
    router.handle_binary(_initial_buffer_bytes(SR))
    assert ring.frames == 0
    assert events == []


def test_raw_slice_patches_loop():
    router, ring, events, _ = _make_router()
    router.sniff_text(_ready_msg())
    router.handle_binary(_initial_buffer_bytes(SR, value=0.0))

    pcm = np.full(480 * CH, 0.5, dtype=np.float32)
    router.handle_binary(_slice_bytes(1000, pcm))
    got = ring.peek(480, position=1000)
    np.testing.assert_allclose(got, 0.5, atol=1e-3)
    assert router.n_slices == 1
    assert ring.is_patched_at(1000)


@pytest.mark.skipif(zstd is None, reason="zstandard not importable")
def test_delta_slice_adds_into_loop():
    router, ring, events, _ = _make_router(
        zstd_dec=zstd.ZstdDecompressor())
    router.sniff_text(_ready_msg())
    router.handle_binary(_initial_buffer_bytes(SR, value=0.25))

    pcm = np.full(480 * CH, 0.25, dtype=np.float32)
    router.handle_binary(_slice_bytes(2000, pcm,
                                      flags=wire.SLICE_FLAG_DELTA))
    got = ring.peek(480, position=2000)
    np.testing.assert_allclose(got, 0.5, atol=2e-3)  # 0.25 + 0.25


# -- swap_ready ----------------------------------------------------------------


def test_swap_ready_clears_then_next_binary_reinits():
    router, ring, events, _ = _make_router()
    router.sniff_text(_ready_msg())
    router.handle_binary(_initial_buffer_bytes(SR, value=0.25))
    assert ring.frames == SR

    router.sniff_text(json.dumps({"type": "swap_ready", "channels": CH}))
    assert ring.frames == 0  # cleared on the recv thread

    router.handle_binary(_initial_buffer_bytes(SR * 2, value=0.125))
    assert ring.frames == SR * 2
    assert [k for k, _ in events] == ["loop-initialized",
                                      "loop-initialized"]


def test_undecodable_frame_does_not_burn_expecting_initial():
    """TCP FIFO means a slice can't really arrive between swap_ready and
    the initial buffer — but a malformed frame in that slot (odd byte
    count, not valid float16) must NOT consume the expecting-initial
    flag, or the REAL initial buffer would be misrouted as a slice."""
    router, ring, events, _ = _make_router()
    router.sniff_text(_ready_msg())
    router.handle_binary(_initial_buffer_bytes(SR))
    router.sniff_text(json.dumps({"type": "swap_ready", "channels": CH}))
    assert ring.frames == 0
    # A 23-byte-header slice frame is odd-length → undecodable as the
    # headerless float16 initial buffer.
    pcm = np.full(480 * CH, 0.5, dtype=np.float32)
    router.handle_binary(_slice_bytes(0, pcm))
    assert ring.frames == 0  # flag preserved, nothing init'd
    router.handle_binary(_initial_buffer_bytes(SR, value=0.125))
    assert ring.frames == SR  # real initial landed


# -- stem blobs / unknown flags --------------------------------------------------


def test_stem_assets_skips_announced_blob_count():
    router, ring, events, _ = _make_router()
    router.sniff_text(_ready_msg())
    router.handle_binary(_initial_buffer_bytes(SR, value=0.0))
    router.sniff_text(json.dumps({"type": "stem_assets", "count": 2}))

    pcm = np.full(480 * CH, 0.5, dtype=np.float32)
    blob = _slice_bytes(1000, pcm)  # would patch if not skipped
    router.handle_binary(blob)
    router.handle_binary(blob)
    assert router.n_slices == 0  # both consumed as stem blobs

    router.handle_binary(blob)  # third one is a real slice again
    assert router.n_slices == 1


def test_unknown_flags_ignored_and_logged_once():
    router, ring, events, logs = _make_router()
    router.sniff_text(_ready_msg())
    router.handle_binary(_initial_buffer_bytes(SR))
    weird = b"\x07" + b"\x00" * 100
    router.handle_binary(weird)
    router.handle_binary(weird)
    flag_logs = [l for l in logs if "flags=0x07" in l]
    assert len(flag_logs) == 1
    assert router.n_slices == 0


def test_garbage_slice_logs_once_and_never_raises():
    router, ring, events, logs = _make_router()
    router.sniff_text(_ready_msg())
    router.handle_binary(_initial_buffer_bytes(SR))
    garbage = b"\x01" + b"\x00" * 10  # DELTA flag, truncated header
    router.handle_binary(garbage)
    router.handle_binary(garbage)
    rejects = [l for l in logs if "slice rejected" in l
               or "slice too short" in l]
    assert len(rejects) == 1


# -- detach ----------------------------------------------------------------------


def test_detach_makes_router_inert():
    router, ring, events, _ = _make_router()
    router.sniff_text(_ready_msg())
    router.detach()
    router.handle_binary(_initial_buffer_bytes(SR))
    assert ring.frames == 0
    assert events == []
    # sniff after detach is also inert
    router.sniff_text(json.dumps({"type": "swap_ready", "channels": CH}))
    assert ring.frames == 0


def test_detached_router_does_not_clear_new_sessions_ring():
    """The stale-recv-thread race: old router detached, ring re-init'd by
    the new session — old router's late frames must not touch it."""
    router_old, ring, _, _ = _make_router()
    router_old.sniff_text(_ready_msg())
    router_old.handle_binary(_initial_buffer_bytes(SR))

    router_new, _, events_new, _ = _make_router(ring=ring)
    router_old.detach()
    router_new.sniff_text(_ready_msg())
    router_new.handle_binary(_initial_buffer_bytes(SR * 2, value=0.5))
    assert ring.frames == SR * 2

    # Old recv thread limps in with a stale swap_ready + blob.
    router_old.sniff_text(json.dumps({"type": "swap_ready",
                                      "channels": CH}))
    router_old.handle_binary(_initial_buffer_bytes(SR, value=0.0))
    assert ring.frames == SR * 2  # untouched


# -- telemetry --------------------------------------------------------------------


def test_late_patch_recorded_in_stats():
    stats = SmoothnessStats()
    router, ring, _, _ = _make_router(stats=stats)
    router.sniff_text(_ready_msg())
    router.handle_binary(_initial_buffer_bytes(SR * 10))

    # Move the playhead forward 5 s, then patch 1 s BEHIND it.
    ring.read(5 * SR)
    pcm = np.full(480 * CH, 0.5, dtype=np.float32)
    router.handle_binary(_slice_bytes(int(ring.position) - SR, pcm))
    snap = stats.drain()
    assert snap["patches"] == 1
    assert snap["patches_late"] == 1

    # And one comfortably ahead is not late.
    router.handle_binary(_slice_bytes(int(ring.position) + SR, pcm))
    snap = stats.drain()
    assert snap["patches_late"] == 0
    assert snap["patch_lead_min_s"] == pytest.approx(1.0, abs=0.1)


def test_sniff_ignores_non_routing_messages_cheaply():
    router, ring, events, logs = _make_router()
    router.sniff_text(json.dumps({"type": "params_echo", "raw": {}}))
    router.sniff_text("not even json {{{")
    router.sniff_text(json.dumps({"type": "prompt_applied"}))
    # No state change, no crash.
    router.handle_binary(_initial_buffer_bytes(SR))
    assert ring.frames == 0  # still not expecting initial

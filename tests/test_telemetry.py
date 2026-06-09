"""Tests for src/telemetry.py — SmoothnessStats + compute_patch_lead."""

import threading

from telemetry import SmoothnessStats, compute_patch_lead


SR = 48000


# -- compute_patch_lead -------------------------------------------------------


def test_lead_ahead_of_playhead():
    # Playhead at 0, slice lands 1 s ahead in a 10 s loop.
    lead_s, late = compute_patch_lead(SR, 0, 10 * SR, SR)
    assert lead_s == 1.0
    assert late is False


def test_lead_wraps_across_loop_end():
    # Playhead near loop end; slice lands just past the wrap → small
    # positive lead, not a huge negative one.
    frames = 10 * SR
    lead_s, late = compute_patch_lead(SR // 2, frames - SR, frames, SR)
    assert abs(lead_s - 1.5) < 1e-9
    assert late is False


def test_lead_behind_playhead_is_late():
    # Slice lands 1 s BEHIND the playhead → late, negative lead.
    frames = 10 * SR
    lead_s, late = compute_patch_lead(4 * SR, 5 * SR, frames, SR)
    assert late is True
    assert abs(lead_s - (-1.0)) < 1e-9


def test_lead_exactly_at_playhead_is_late():
    lead_s, late = compute_patch_lead(5 * SR, 5 * SR, 10 * SR, SR)
    assert late is True
    assert lead_s == 0.0


def test_lead_uninitialized_loop_is_benign():
    assert compute_patch_lead(100, 0, 0, SR) == (0.0, False)


# -- SmoothnessStats ----------------------------------------------------------


def test_params_gap_max_tracks_maximum():
    s = SmoothnessStats()
    s.note_params_send(10.0)
    s.note_params_send(50.0)
    s.note_params_send(20.0)
    snap = s.drain()
    assert snap["params_sends"] == 3
    assert snap["params_gap_max_ms"] == 50.0


def test_patch_counters_and_min_lead():
    s = SmoothnessStats()
    s.note_patch(1.0, False)
    s.note_patch(0.25, False)
    s.note_patch(-0.5, True)
    snap = s.drain()
    assert snap["patches"] == 3
    assert snap["patches_late"] == 1
    assert snap["patch_lead_min_s"] == -0.5


def test_heartbeat_stats():
    s = SmoothnessStats()
    s.note_heartbeat(120.0, ok=True)
    s.note_heartbeat(450.0, ok=False)
    s.note_heartbeat(90.0, ok=True)
    snap = s.drain()
    assert snap["hb_count"] == 3
    assert snap["hb_last_ms"] == 90.0
    assert snap["hb_max_ms"] == 450.0
    assert snap["hb_fails"] == 1


def test_drain_resets_everything():
    s = SmoothnessStats()
    s.note_params_send(10.0)
    s.note_patch(-1.0, True)
    s.note_drain_gap(300.0)
    s.note_heartbeat(100.0, ok=False)
    s.drain()
    snap = s.drain()
    assert snap["params_sends"] == 0
    assert snap["params_gap_max_ms"] == 0.0
    assert snap["patches"] == 0
    assert snap["patches_late"] == 0
    assert snap["patch_lead_min_s"] is None
    assert snap["drain_gap_max_ms"] == 0.0
    assert snap["hb_count"] == 0
    assert snap["hb_fails"] == 0


def test_format_line_renders_all_fields():
    s = SmoothnessStats()
    s.note_params_send(12.0)
    s.note_patch(0.8, False)
    s.note_drain_gap(120.0)
    s.note_heartbeat(200.0, ok=True)
    line = SmoothnessStats.format_line(s.drain(), underruns_since=2)
    for token in ("params=1", "late=0", "lead_min=0.80s",
                  "drain_gap_max=120ms", "hb=200ms", "underruns=+2"):
        assert token in line, f"missing {token!r} in {line!r}"


def test_format_line_handles_no_patches():
    s = SmoothnessStats()
    line = SmoothnessStats.format_line(s.drain())
    assert "lead_min=n/a" in line


def test_concurrent_updates_do_not_explode():
    """Producers hammer from threads while the main thread drains.
    We only assert no exceptions and plausible totals (telemetry
    tolerates small races by design)."""
    s = SmoothnessStats()
    stop = threading.Event()
    errors = []

    def producer():
        try:
            while not stop.is_set():
                s.note_params_send(5.0)
                s.note_patch(0.5, False)
                s.note_drain_gap(10.0)
                s.note_heartbeat(50.0, ok=True)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=producer) for _ in range(4)]
    for t in threads:
        t.start()
    total = 0
    for _ in range(50):
        total += s.drain()["params_sends"]
    stop.set()
    for t in threads:
        t.join(timeout=2.0)
    total += s.drain()["params_sends"]
    assert not errors
    assert total > 0

"""Smoothness telemetry — cheap cross-thread counters for audio health.

Why this exists
---------------
"Occasionally choppy" has at least four distinct failure modes in demonTD:

  * PortAudio underruns (audio callback missed its deadline)
  * slices patched late — at/behind the playhead — so the loop plays
    stale content (server pacing fell behind, or patching stalled)
  * the params keepalive (the server's pacing signal) stopped flowing
  * the TD main thread hitched (heavy cook, UI, blocking I/O)

Each failure mode updates a counter here from whatever thread it lives
on; the main thread drains a snapshot ~every few seconds and logs one
`[health]` line. When chop is audible, that line says which subsystem
did it.

Thread-safety: plain int/float attributes only. CPython's GIL makes
single attribute reads/writes atomic; the worst case under racing
updates is one slightly-stale max — fine for telemetry (same argument
as SpeakerOut's callback counters). No locks, so noting an event from
the audio/recv/pacer threads costs nanoseconds and can never block.
"""

from __future__ import annotations


def compute_patch_lead(start_sample: int, position: int, frames: int,
                       sample_rate: int) -> tuple[float, bool]:
    """How far ahead of the playhead a slice landed, wrap-normalized.

    Returns (lead_seconds, late). The loop wraps, so a raw difference is
    ambiguous; anything more than half a loop "ahead" is really behind.
    `late` means the slice landed at/behind the playhead — the playhead
    already passed that region, so the patch won't be heard until the
    next loop iteration (the audible symptom is the playhead playing
    STALE content where this patch should have been).
    """
    if frames <= 0 or sample_rate <= 0:
        return 0.0, False
    lead_frames = (start_sample - position) % frames
    late = lead_frames == 0 or lead_frames > frames // 2
    if late and lead_frames > 0:
        lead_frames -= frames  # express as negative seconds behind
    return lead_frames / float(sample_rate), late


class SmoothnessStats:
    """Counters + maxima updated from any thread, drained by the main
    thread. All note_* methods are allocation-light and never raise."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        # Params pacer (the keepalive / server pacing signal).
        self.params_send_count = 0
        self.params_gap_max_ms = 0.0
        # Slice patches (binary router / _on_binary).
        self.patch_count = 0
        self.patch_late_count = 0
        self.patch_lead_min_s: float | None = None
        # Main-thread frame cadence (measured in _drain_inbound).
        self.drain_gap_max_ms = 0.0
        # Queue heartbeat HTTP (worker thread).
        self.hb_count = 0
        self.hb_last_ms = 0.0
        self.hb_max_ms = 0.0
        self.hb_fail_count = 0

    # -- producers (any thread) --------------------------------------------

    def note_params_send(self, gap_ms: float) -> None:
        self.params_send_count += 1
        if gap_ms > self.params_gap_max_ms:
            self.params_gap_max_ms = gap_ms

    def note_patch(self, lead_s: float, late: bool) -> None:
        """`lead_s` = how far ahead of the playhead the slice landed, in
        seconds (wrap-normalized by the caller). `late` = the slice
        landed at/behind the playhead — its audio will never be heard
        this loop iteration; the playhead already passed it."""
        self.patch_count += 1
        if late:
            self.patch_late_count += 1
        prev = self.patch_lead_min_s
        if prev is None or lead_s < prev:
            self.patch_lead_min_s = lead_s

    def note_drain_gap(self, gap_ms: float) -> None:
        if gap_ms > self.drain_gap_max_ms:
            self.drain_gap_max_ms = gap_ms

    def note_heartbeat(self, duration_ms: float, ok: bool) -> None:
        self.hb_count += 1
        self.hb_last_ms = duration_ms
        if duration_ms > self.hb_max_ms:
            self.hb_max_ms = duration_ms
        if not ok:
            self.hb_fail_count += 1

    # -- consumer (main thread) --------------------------------------------

    def drain(self) -> dict:
        """Snapshot + reset. Tiny race window with producer threads (an
        event landing between snapshot and reset is lost) — acceptable
        for telemetry."""
        snap = {
            "params_sends": self.params_send_count,
            "params_gap_max_ms": self.params_gap_max_ms,
            "patches": self.patch_count,
            "patches_late": self.patch_late_count,
            "patch_lead_min_s": self.patch_lead_min_s,
            "drain_gap_max_ms": self.drain_gap_max_ms,
            "hb_count": self.hb_count,
            "hb_last_ms": self.hb_last_ms,
            "hb_max_ms": self.hb_max_ms,
            "hb_fails": self.hb_fail_count,
        }
        self.reset()
        return snap

    @staticmethod
    def format_line(snap: dict, underruns_since: int = 0) -> str:
        """One-line human summary of a drained snapshot."""
        lead = snap.get("patch_lead_min_s")
        lead_str = f"{lead:.2f}s" if lead is not None else "n/a"
        return (
            f"params={snap['params_sends']} "
            f"(gap_max={snap['params_gap_max_ms']:.0f}ms)  "
            f"patches={snap['patches']} "
            f"late={snap['patches_late']} lead_min={lead_str}  "
            f"drain_gap_max={snap['drain_gap_max_ms']:.0f}ms  "
            f"hb={snap['hb_last_ms']:.0f}ms"
            f"(max={snap['hb_max_ms']:.0f} fails={snap['hb_fails']})  "
            f"underruns=+{underruns_since}"
        )

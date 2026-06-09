"""Params pacer — a dedicated thread for the continuous params stream.

Why this exists
---------------
After `ready`, the continuous `{type:"params", playback_pos}` stream is
BOTH the WS keepalive (the pod has no other) AND the server's pacing
signal: `playback_pos` tells the generation pipeline where the listener
is, so slices land ahead of the playhead. The server's lead buffer floor
is only 0.25 s.

This stream used to be driven from TD's frame loop (frame_exec →
_drain_inbound / OnTick), which means ANY main-thread hitch — a heavy
cook, a UI interaction, a file dialog — silenced it for the duration.
A >250 ms hitch ate the entire server lead: slices landed behind the
playhead and the loop played stale content (audible chop).

The pacer is a daemon thread on a ~16 ms cadence (the web client sends
every 8 ms; frame-rate used to give us ~16 ms — we keep that, now
immune to main-thread stalls).

Threading contract
------------------
* `build_message()` runs on the pacer thread and must be TD-free: it
  reads `_dirty`/`_params_snapshot` under DemonExt._lock, the LoopBuffer
  position (its own lock), and pre-computed caches. Returns the encoded
  JSON string, or None when there's nothing to send (not connected).
  An EMPTY params dict is still a message — that's the keepalive.
* `send(msg)` must be enqueue-only (WSClient.send_text): the recv
  thread is the only thread that touches the socket. NEVER wire this to
  DemonExt._send_text — its failure handling calls Disconnect(), which
  writes TD pars (main-thread only). Failures surface via
  `send_fail_streak`, which the main thread polls.

The main thread watches the pacer the same way frame_exec watches the
old tick path (belt-and-suspenders): restart if the thread died, tear
the connection down if sends keep failing.
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class ParamsPacer:
    def __init__(
        self,
        build_message: Callable[[], str | None],
        send: Callable[[str], bool],
        stats=None,
        interval_s: float = 0.016,
        log: Callable[[str], None] = print,
    ):
        self._build_message = build_message
        self._send = send
        self._stats = stats
        self._interval_s = float(interval_s)
        self._log = log
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_send_t: float = 0.0
        # Read by the main thread (GIL-atomic int) to detect a dead
        # socket: consecutive failures, reset on any success.
        self.send_fail_streak: int = 0
        self._tick_err_logged = False

    # -- lifecycle (main thread) -----------------------------------------------

    @property
    def is_alive(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def start(self) -> None:
        """Idempotent: no-op if the thread is already running."""
        if self.is_alive:
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run, name="demon-params-pacer", daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 1.0) -> None:
        self._stop_evt.set()
        t = self._thread
        if (t is not None and t.is_alive()
                and threading.current_thread() is not t):
            t.join(timeout=join_timeout)

    def last_send_age(self) -> float:
        """Seconds since the last successful send; +inf if none yet.
        Used by the main-thread watchdog."""
        if self._last_send_t <= 0.0:
            return float("inf")
        return time.monotonic() - self._last_send_t

    # -- pacer loop (background thread) -----------------------------------------

    def _run(self) -> None:
        while not self._stop_evt.wait(self._interval_s):
            try:
                self.tick_once()
            except Exception as e:
                # Must never die silently — the params stream is the
                # keepalive. Log once (not per-tick: 60/s spam).
                if not self._tick_err_logged:
                    self._tick_err_logged = True
                    try:
                        self._log(f"[pacer] tick raised (logging once): "
                                  f"{type(e).__name__}: {e}")
                    except Exception:
                        pass

    def tick_once(self) -> bool:
        """One pacer cycle: build + send. Public so tests can drive it
        without sleeping. Returns True if a message was sent OK."""
        msg = self._build_message()
        if msg is None:
            return False
        ok = bool(self._send(msg))
        if ok:
            now = time.monotonic()
            if self._stats is not None:
                gap_ms = ((now - self._last_send_t) * 1000.0
                          if self._last_send_t > 0.0 else 0.0)
                self._stats.note_params_send(gap_ms)
            self._last_send_t = now
            self.send_fail_streak = 0
        else:
            self.send_fail_streak += 1
        return ok

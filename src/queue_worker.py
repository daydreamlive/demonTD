"""Background queue-heartbeat worker — keeps HTTP off the TD main thread.

Why this exists
---------------
The hosted-mode session heartbeat (`GET /api/queue/status` every 5 s) and
auto-extend (`POST /api/queue/extend`) used to run synchronously on the TD
main thread (OnHeartbeat). Each poll opened a FRESH https connection
(TCP + TLS handshake) with a 10 s urlopen timeout — blocking TD's frame
loop 100-400 ms typical, seconds worst-case, every 5 seconds. While
blocked, no params reached the server (its pacing signal) and no slices
were patched: the periodic "occasionally choppy" audio.

This worker does ONLY the HTTP. Results are marshalled back to the main
thread via `post_event` (DemonExt's `_inbound` queue), where all the TD
par reads/writes and state transitions happen exactly as before.

Threading contract
------------------
* `get_state()` is called each cycle and must be cheap + TD-free. It
  returns None (idle — no hosted session) or (base_url, api_key,
  session_id) read from plain Python attributes.
* `pop_extend_flag()` returns True at most once per requested extend
  (main thread sets the flag, only the worker clears it).
* `post_event(kind, payload)` must be thread-safe (queue.Queue.put).
* The worker NEVER touches TD pars, the WS socket, or DemonExt state.

The worker is persistent: it survives reconnects/failovers because it
re-reads `get_state()` every cycle, and simply idles (idle_poll_s) while
there is no session.
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class QueueHeartbeatWorker:
    def __init__(
        self,
        get_state: Callable[[], tuple | None],
        pop_extend_flag: Callable[[], bool],
        post_event: Callable[[str, object], None],
        client_factory: Callable[[str, str | None], object],
        stats=None,
        interval_s: float = 5.0,
        idle_poll_s: float = 0.5,
        log: Callable[[str], None] = print,
    ):
        self._get_state = get_state
        self._pop_extend_flag = pop_extend_flag
        self._post_event = post_event
        self._client_factory = client_factory
        self._stats = stats
        self._interval_s = float(interval_s)
        self._idle_poll_s = float(idle_poll_s)
        self._log = log
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_poll_t = 0.0

    # -- lifecycle (main thread) ---------------------------------------------

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
            target=self._run, name="demon-hb-worker", daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 2.0) -> None:
        self._stop_evt.set()
        t = self._thread
        if (t is not None and t.is_alive()
                and threading.current_thread() is not t):
            t.join(timeout=join_timeout)

    # -- worker loop (background thread) ---------------------------------------

    def _run(self) -> None:
        while not self._stop_evt.wait(self._idle_poll_s):
            try:
                now = time.monotonic()
                if now - self._last_poll_t >= self._interval_s:
                    if self.poll_once():
                        self._last_poll_t = now
            except Exception as e:  # the worker must never die silently
                try:
                    self._log(f"[hb-worker] cycle raised: "
                              f"{type(e).__name__}: {e}")
                except Exception:
                    pass

    def poll_once(self) -> bool:
        """One heartbeat cycle: status poll + optional extend. Public so
        tests can drive it without sleeping. Returns True if a poll was
        attempted (i.e. there was an active session), False if idle."""
        state = self._get_state()
        if state is None:
            return False
        base, api_key, session_id = state

        t0 = time.monotonic()
        try:
            client = self._client_factory(base, api_key)
            resp = client.status(session_id)
        except Exception as e:
            dur_ms = (time.monotonic() - t0) * 1000.0
            if self._stats is not None:
                self._stats.note_heartbeat(dur_ms, ok=False)
            self._post_event("hb-error", (str(e), dur_ms))
            return True
        dur_ms = (time.monotonic() - t0) * 1000.0
        if self._stats is not None:
            self._stats.note_heartbeat(dur_ms, ok=True)
        self._post_event("hb-status", (resp, dur_ms))

        # Extend only when the main thread requested it (auto-extend
        # decision or the user's Still Playing pulse). The flag is
        # consumed exactly once per request.
        if self._pop_extend_flag():
            try:
                client = self._client_factory(base, api_key)
                eresp = client.extend(session_id)
            except Exception as e:
                self._post_event("hb-extend", ("err", str(e)))
            else:
                self._post_event("hb-extend", ("ok", eresp))
        return True

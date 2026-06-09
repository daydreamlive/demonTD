"""Tests for src/queue_worker.py — the background heartbeat worker."""

import threading
import time

from queue_worker import QueueHeartbeatWorker
from telemetry import SmoothnessStats


class FakeResp:
    def __init__(self, status="active"):
        self.status = status


class FakeClient:
    def __init__(self, calls, fail_status=False, fail_extend=False):
        self._calls = calls
        self._fail_status = fail_status
        self._fail_extend = fail_extend

    def status(self, sid):
        self._calls.append(("status", sid))
        if self._fail_status:
            raise RuntimeError("boom")
        return FakeResp()

    def extend(self, sid):
        self._calls.append(("extend", sid))
        if self._fail_extend:
            raise RuntimeError("extend boom")
        return FakeResp()


def _make_worker(state=("https://q", "key", "sid-1"), *, calls=None,
                 events=None, extend_flag=None, fail_status=False,
                 fail_extend=False, stats=None):
    calls = calls if calls is not None else []
    events = events if events is not None else []
    flag = {"v": bool(extend_flag)}

    def pop_extend():
        if flag["v"]:
            flag["v"] = False
            return True
        return False

    worker = QueueHeartbeatWorker(
        get_state=lambda: state,
        pop_extend_flag=pop_extend,
        post_event=lambda k, p: events.append((k, p)),
        client_factory=lambda base, key: FakeClient(
            calls, fail_status=fail_status, fail_extend=fail_extend),
        stats=stats,
        interval_s=0.01,
        idle_poll_s=0.005,
        log=lambda m: None,
    )
    return worker, calls, events, flag


def test_poll_once_polls_status_and_posts_event():
    worker, calls, events, _ = _make_worker()
    assert worker.poll_once() is True
    assert calls == [("status", "sid-1")]
    assert len(events) == 1
    kind, (resp, dur_ms) = events[0]
    assert kind == "hb-status"
    assert resp.status == "active"
    assert dur_ms >= 0.0


def test_poll_once_idle_when_no_state():
    worker, calls, events, _ = _make_worker(state=None)
    assert worker.poll_once() is False
    assert calls == []
    assert events == []


def test_status_error_posts_hb_error_and_continues():
    stats = SmoothnessStats()
    worker, calls, events, _ = _make_worker(fail_status=True, stats=stats)
    assert worker.poll_once() is True
    kind, (msg, dur_ms) = events[0]
    assert kind == "hb-error"
    assert "boom" in msg
    assert stats.drain()["hb_fails"] == 1
    # A second poll still works (worker didn't die).
    assert worker.poll_once() is True


def test_extend_only_after_flag_and_consumed_once():
    worker, calls, events, flag = _make_worker(extend_flag=False)
    worker.poll_once()
    assert ("extend", "sid-1") not in calls

    flag["v"] = True
    worker.poll_once()
    assert calls.count(("extend", "sid-1")) == 1
    kinds = [k for k, _ in events]
    assert kinds == ["hb-status", "hb-status", "hb-extend"]
    ok, resp = events[-1][1]
    assert ok == "ok"

    # Flag was consumed — no further extends.
    worker.poll_once()
    assert calls.count(("extend", "sid-1")) == 1


def test_extend_error_posts_err_payload():
    worker, calls, events, flag = _make_worker(extend_flag=True,
                                               fail_extend=True)
    worker.poll_once()
    kind, payload = events[-1]
    assert kind == "hb-extend"
    assert payload[0] == "err"
    assert "extend boom" in payload[1]


def test_heartbeat_duration_recorded_in_stats():
    stats = SmoothnessStats()
    worker, _, _, _ = _make_worker(stats=stats)
    worker.poll_once()
    snap = stats.drain()
    assert snap["hb_count"] == 1
    assert snap["hb_fails"] == 0


def test_thread_lifecycle_start_idempotent_stop_joins():
    worker, calls, events, _ = _make_worker()
    worker.start()
    t1 = worker._thread
    worker.start()  # idempotent — same thread
    assert worker._thread is t1
    deadline = time.monotonic() + 2.0
    while not events and time.monotonic() < deadline:
        time.sleep(0.005)
    worker.stop()
    assert not worker.is_alive
    assert events, "worker thread never polled"


def test_worker_survives_get_state_exception():
    """The loop's outer try/except must keep the thread alive."""
    boom_then_ok = {"n": 0}

    def get_state():
        boom_then_ok["n"] += 1
        if boom_then_ok["n"] < 3:
            raise RuntimeError("transient")
        return ("https://q", None, "sid-9")

    calls, events = [], []
    worker = QueueHeartbeatWorker(
        get_state=get_state,
        pop_extend_flag=lambda: False,
        post_event=lambda k, p: events.append((k, p)),
        client_factory=lambda base, key: FakeClient(calls),
        interval_s=0.0,
        idle_poll_s=0.001,
        log=lambda m: None,
    )
    worker.start()
    deadline = time.monotonic() + 2.0
    while not events and time.monotonic() < deadline:
        time.sleep(0.005)
    worker.stop()
    assert events, "worker died on a get_state exception"


def test_restart_after_stop_creates_fresh_thread():
    worker, _, events, _ = _make_worker()
    worker.start()
    worker.stop()
    assert not worker.is_alive
    worker.start()
    assert worker.is_alive
    worker.stop()

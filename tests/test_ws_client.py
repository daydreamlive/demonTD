"""Unit tests for src/ws_client.py — the outbound-queue send path.

The critical invariant: send_text/send_binary only ENQUEUE; the socket is
touched solely by the recv thread (via _flush_outbound). This is what
eliminates the concurrent SSL read/write that was corrupting the
connection and causing client-side disconnects with no server error.

We don't open a real socket — we drive the queue + flush logic directly
with a fake WebSocket.
"""

from __future__ import annotations

import os
import sys

# ws_client does `import websocket` at module top; the vendored
# websocket-client lives under vendor/ (added to sys.path by DemonExt at
# runtime). Mirror that here so the import resolves in the test env.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WSC_VENDOR = os.path.join(_REPO, "vendor", "websocket-client")
if os.path.isdir(_WSC_VENDOR) and _WSC_VENDOR not in sys.path:
    sys.path.insert(0, _WSC_VENDOR)

import websocket  # noqa: E402
import ws_client as wsc_mod  # noqa: E402


class FakeWS:
    """Records sends; emulates the bits of websocket.WebSocket we touch."""

    def __init__(self, fail_on=None):
        self.connected = True
        self.sent: list[tuple] = []
        self._fail_on = fail_on  # raise on the Nth send (1-based), or None
        self._n = 0
        self.closed_with = None

    def settimeout(self, _t):
        pass  # _flush_outbound bumps the timeout around each send

    def send(self, payload, opcode=None):
        self._n += 1
        if self._fail_on is not None and self._n == self._fail_on:
            raise OSError("simulated write failure")
        self.sent.append((payload, opcode))

    def close(self, status=None, reason=b""):
        self.closed_with = status


def _make_client():
    c = wsc_mod.WSClient("ws://test/")
    c._ws = FakeWS()  # bypass connect(); we only exercise queue + flush
    return c


def test_send_enqueues_does_not_touch_socket():
    c = _make_client()
    assert c.send_text("hello") is True
    assert c.send_binary(b"\x00\x01") is True
    # Nothing written to the socket yet — only queued.
    assert c._ws.sent == []
    assert c._outbound.qsize() == 2


def test_flush_outbound_sends_fifo_on_one_thread():
    c = _make_client()
    c.send_text("a")
    c.send_binary(b"b")
    c.send_text("c")
    err = c._flush_outbound()
    assert err is None
    payloads = [p for (p, _op) in c._ws.sent]
    assert payloads == ["a", b"b", "c"]          # FIFO order preserved
    opcodes = [op for (_p, op) in c._ws.sent]
    assert opcodes[0] == websocket.ABNF.OPCODE_TEXT
    assert opcodes[1] == websocket.ABNF.OPCODE_BINARY
    assert c._n_sent == 3
    assert c._outbound.qsize() == 0


def test_flush_outbound_reports_write_failure():
    c = _make_client()
    c._ws = FakeWS(fail_on=2)  # second send raises
    c.send_text("ok")
    c.send_text("boom")
    err = c._flush_outbound()
    assert err is not None
    assert "send failed" in err
    assert c._n_sent == 1  # only the first one counted


def test_send_returns_false_when_no_socket():
    c = wsc_mod.WSClient("ws://test/")
    # _ws is None before connect — enqueue must refuse (caller sees False).
    assert c.send_text("x") is False
    assert c._outbound.qsize() == 0


def test_send_returns_false_when_closing():
    c = _make_client()
    c._closing = True
    assert c.send_text("x") is False


def test_outbound_overflow_drops_oldest():
    c = _make_client()
    cap = c._outbound.maxsize
    for i in range(cap):
        assert c.send_text(f"m{i}") is True
    assert c._outbound.qsize() == cap
    # One more than capacity: drops the oldest, still enqueues the newest.
    assert c.send_text("newest") is True
    assert c._n_dropped == 1
    assert c._outbound.qsize() == cap
    # Flush and confirm the newest survived and the very oldest ("m0") was
    # the one dropped.
    c._flush_outbound()
    payloads = [p for (p, _op) in c._ws.sent]
    assert "newest" in payloads
    assert "m0" not in payloads


def test_close_then_connect_does_not_resurrect_closing_flag_race():
    """connect()/close() lifecycle is serialized by _lifecycle_lock:
    after close() (no live thread), connect() may legitimately start a
    NEW session — but close() must always leave _closing=True until a
    deliberate connect(), and connect() while a live thread runs must
    not touch the flag."""
    c = _make_client()
    c.close()
    assert c._closing is True
    # A deliberate reconnect resets the flag under the lock.
    c.connect()
    try:
        assert c._closing is False
    finally:
        c.close()


def test_connect_ignored_while_thread_alive_preserves_closing_request():
    import threading as _threading

    c = wsc_mod.WSClient("ws://test/")
    # Fake an alive recv thread so connect() takes the ignore branch.
    ev = _threading.Event()
    t = _threading.Thread(target=ev.wait, daemon=True)
    t.start()
    c._thread = t
    c._closing = True  # a close() was requested
    c.connect()
    # connect() must NOT have reset the in-flight close request.
    assert c._closing is True
    ev.set()
    t.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Close-frame parsing (the pod's last words)
# ---------------------------------------------------------------------------

def test_parse_close_frame_code_and_reason():
    payload = (1011).to_bytes(2, "big") + b"TRT engine not built"
    assert wsc_mod.parse_close_frame(payload) == (
        1011, "TRT engine not built")


def test_parse_close_frame_code_only():
    assert wsc_mod.parse_close_frame((1000).to_bytes(2, "big")) == (1000, "")


def test_parse_close_frame_empty():
    assert wsc_mod.parse_close_frame(b"") == (None, "")
    assert wsc_mod.parse_close_frame(b"\x03") == (None, "")


def test_parse_close_frame_bad_utf8_never_raises():
    payload = (1011).to_bytes(2, "big") + b"\xff\xfe broken"
    code, reason = wsc_mod.parse_close_frame(payload)
    assert code == 1011
    assert "broken" in reason


# ---------------------------------------------------------------------------
# Large-send fragmentation (keepalive pings answered mid-upload)
# ---------------------------------------------------------------------------

class FakeWSFrag(FakeWS):
    """FakeWS + the fragmented-send surface (send_frame / recv_data)."""

    def __init__(self, recv_results=None):
        super().__init__()
        self.frames: list[tuple[bytes, int, int]] = []  # (data, opcode, fin)
        self.recv_calls = 0
        # Each _service_socket_once pops one entry; empty -> timeout
        # (the common "nothing pending on the socket" case).
        self._recv_results = list(recv_results or [])

    def send_frame(self, frame):
        self.frames.append((frame.data, frame.opcode, frame.fin))

    def recv_data(self, control_frame=False):
        self.recv_calls += 1
        if self._recv_results:
            return self._recv_results.pop(0)
        raise websocket.WebSocketTimeoutException()


def test_big_send_is_fragmented_and_services_socket():
    c = _make_client()
    c._ws = FakeWSFrag()
    payload = bytes(range(256)) * (5 * 4096 + 13)  # ~5.0 MB, odd tail
    assert c.send_binary(payload) is True
    err = c._flush_outbound()
    assert err is None

    frames = c._ws.frames
    assert len(frames) > 1, "payload above the threshold must fragment"
    # First frame opens the message; the rest are continuations; only
    # the last has fin set.
    assert frames[0][1] == websocket.ABNF.OPCODE_BINARY and frames[0][2] == 0
    for _data, op, fin in frames[1:-1]:
        assert op == websocket.ABNF.OPCODE_CONT and fin == 0
    assert frames[-1][1] == websocket.ABNF.OPCODE_CONT
    assert frames[-1][2] == 1
    # Reassembly is byte-identical and chunks respect the fragment size.
    assert b"".join(d for d, _o, _f in frames) == payload
    assert all(len(d) <= c._SEND_FRAGMENT_BYTES for d, _o, _f in frames)
    # The socket was serviced between every pair of fragments — that's
    # where a pending server PING gets auto-ponged.
    assert c._ws.recv_calls == len(frames) - 1
    # No monolithic send for the big payload.
    assert c._ws.sent == []


def test_small_send_stays_monolithic():
    c = _make_client()
    c._ws = FakeWSFrag()
    c.send_binary(b"x" * 1024)
    assert c._flush_outbound() is None
    assert c._ws.frames == []
    assert [p for p, _ in c._ws.sent] == [b"x" * 1024]


def test_server_close_mid_upload_aborts_send():
    c = _make_client()
    close_payload = (1011).to_bytes(2, "big") + b"keepalive ping timeout"
    c._ws = FakeWSFrag(
        recv_results=[(websocket.ABNF.OPCODE_CLOSE, close_payload)])
    c.send_binary(b"y" * (3 << 20))  # 3 MiB -> >= 3 fragments
    err = c._flush_outbound()
    assert err == "server sent close: keepalive ping timeout"
    assert c._server_close_code == 1011
    # Upload stopped early — far fewer frames than the full payload.
    assert len(c._ws.frames) < 3


# ---------------------------------------------------------------------------
# Write-readiness gate (don't wedge the recv thread when the pod stalls)
# ---------------------------------------------------------------------------

def test_flush_defers_when_socket_not_writable():
    c = _make_client()
    c._socket_writable = lambda: False  # pod momentarily not reading
    c.send_text("p1")
    c.send_text("p2")
    err = c._flush_outbound()
    # Nothing went out, the in-hand frame is held, the loop returns
    # promptly (so the caller can go answer pings) — no error.
    assert err is None
    assert c._ws.sent == []
    assert c._pending_send == (websocket.ABNF.OPCODE_TEXT, "p1")


def test_deferred_frame_sent_first_when_writable_again():
    c = _make_client()
    c._socket_writable = lambda: False
    c.send_text("p1")
    c.send_text("p2")
    c._flush_outbound()              # defers p1
    c._socket_writable = lambda: True  # pod reading again
    assert c._flush_outbound() is None
    # Deferred frame goes first, then the rest — order preserved, none lost.
    assert [p for p, _ in c._ws.sent] == ["p1", "p2"]
    assert c._pending_send is None


def test_discrete_message_not_dropped_under_backpressure():
    c = _make_client()
    c._socket_writable = lambda: False
    c.send_text('{"type":"enable_lora","id":"bach"}')  # must NOT be dropped
    c._flush_outbound()
    assert c._ws.sent == []
    # Still held for retry, not discarded.
    assert c._pending_send[1] == '{"type":"enable_lora","id":"bach"}'
    c._socket_writable = lambda: True
    c._flush_outbound()
    assert [p for p, _ in c._ws.sent] == ['{"type":"enable_lora","id":"bach"}']


def test_socket_writable_true_when_no_sock():
    c = wsc_mod.WSClient("ws://test/")
    c._ws = FakeWS()  # no .sock attribute
    assert c._socket_writable() is True


# ---------------------------------------------------------------------------
# Param coalescing (newest-wins; no backlog to head-of-line-block the pong)
# ---------------------------------------------------------------------------

def test_send_params_coalesces_newest_wins():
    c = _make_client()
    c.send_params("p1")
    c.send_params("p2")
    c.send_params("p3")          # only this should ever go out
    assert c._flush_outbound() is None
    assert [p for p, _ in c._ws.sent] == ["p3"]


def test_discretes_flushed_before_coalesced_params():
    c = _make_client()
    c.send_text("d1")            # discrete FIFO
    c.send_binary(b"d2")
    c.send_params("p1")          # coalesced slot
    assert c._flush_outbound() is None
    # Discretes first (in order), then the single latest params.
    assert [p for p, _ in c._ws.sent] == ["d1", b"d2", "p1"]


def test_params_held_when_not_writable_then_newest_sent():
    c = _make_client()
    c._socket_writable = lambda: False
    c.send_params("p1")
    assert c._flush_outbound() is None
    assert c._ws.sent == []                 # nothing went out
    assert c._latest_params == "p1"         # held, not dropped
    c.send_params("p2")                     # pacer overwrites with fresher
    c._socket_writable = lambda: True
    assert c._flush_outbound() is None
    assert [p for p, _ in c._ws.sent] == ["p2"]   # only the newest
    assert c._latest_params is None


def test_send_params_rejected_once_closing():
    c = _make_client()
    c._closing = True
    assert c.send_params("p1") is False
    assert c._latest_params is None

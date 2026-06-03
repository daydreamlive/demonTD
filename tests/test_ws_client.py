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

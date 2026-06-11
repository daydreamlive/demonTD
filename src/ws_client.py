"""
Background-thread WebSocket client backed by the `websocket-client` library.

Why this exists
---------------
TouchDesigner's built-in WebSocket DAT can't actually transmit large binary
frames (~9 MB+) — it reports success but never delivers the bytes, so the
DEMON server drops us. Verified independently: an identical request from a
plain Python `websocket-client` succeeds and the server returns `ready`.

This module wraps `websocket-client` (vendored under
`vendor/websocket-client/`) in a background thread so TD can use a working
WebSocket from inside a COMP without going through the broken DAT.

Public API
----------
    ws = WSClient(
        url="ws://host:port/",
        on_open=lambda: ...,
        on_text=lambda s: ...,
        on_binary=lambda b: ...,
        on_close=lambda code, reason: ...,
        log=print,
    )
    ws.connect()
    ws.send_text("...")
    ws.send_binary(b"...")
    ws.close()

All callbacks run on the background recv thread. The owner (DemonExt) must
not block in callbacks and must marshal anything TD-touching to the cook
thread via `tdu.Dependency` / `op.cook()` if needed.
"""

from __future__ import annotations

import queue
import select
import threading
import time
from typing import Callable

# Lazy import — wired in DemonExt's _prepend_vendor_path step.
# Outside TD: websocket-client must be on PYTHONPATH.
import websocket  # type: ignore[import-not-found]


def parse_close_frame(data: bytes | str) -> tuple[int | None, str]:
    """Parse a raw WS close-frame payload into (code, reason).

    RFC 6455 §5.5.1: an optional 2-byte big-endian status code followed
    by optional UTF-8 reason text. Empty/short payload -> (None, "")."""
    if isinstance(data, str):
        data = data.encode("utf-8", errors="replace")
    if not data or len(data) < 2:
        return None, ""
    code = int.from_bytes(data[:2], "big")
    reason = data[2:].decode("utf-8", errors="replace").strip()
    return code, reason


class WSClient:
    # Socket timeout for the recv loop. Short so the loop wakes ~10×/s to
    # flush queued outbound sends; large sends temporarily raise it (see
    # _flush_outbound).
    _RECV_TIMEOUT = 0.1

    def __init__(
        self,
        url: str,
        on_open: Callable[[], None] | None = None,
        on_text: Callable[[str], None] | None = None,
        on_binary: Callable[[bytes], None] | None = None,
        on_close: Callable[[int | None, str | None], None] | None = None,
        log: Callable[[str], None] = print,
        timeout: float = 30.0,
        ping_interval: float = 0.0,  # deprecated/ignored; see note below
    ):
        self.url = url
        self._on_open = on_open
        self._on_text = on_text
        self._on_binary = on_binary
        self._on_close = on_close
        self._log = log
        self._timeout = timeout
        # NOTE: app-level WS ping was REMOVED. Python's ssl.SSLSocket is not
        # safe for simultaneous read+write from two threads, and a ping sent
        # from a side-task while the recv loop is mid-read corrupted the SSL
        # record layer (SSL: BAD_LENGTH) → client-side disconnect with no
        # server error. We now keep the connection alive purely via the
        # outbound message stream (the param keepalive), exactly like the
        # browser web client — and ALL sends are funneled onto the single
        # recv thread (see _outbound), so the socket is only ever touched by
        # one thread. `ping_interval` is accepted for call-site compat but
        # ignored.

        # Outbound queue: send_text/send_binary ENQUEUE from any thread; the
        # recv thread is the ONLY thread that touches the socket (drains this
        # queue between recvs). Bounded so a stalled socket can't balloon
        # memory; params are idempotent so dropping the oldest on overflow is
        # safe.
        self._outbound: "queue.Queue[tuple[int, bytes | str]]" = queue.Queue(
            maxsize=512)

        # Coalesced params slot. The params stream is the keepalive AND the
        # pod's pacing signal, sent at ~62 Hz — but params SUPERSEDE each
        # other (only the newest playback_pos + knob values matter). Queuing
        # them FIFO let a pod that drains slower than 62 Hz accumulate a deep
        # backlog (observed: 199 messages), which head-of-line-blocks the
        # PONG behind it in the TCP stream → the pod sees our pong past its
        # 20 s keepalive window → 1011. So params get ONE slot (newest wins),
        # drained after the discrete FIFO. At most one params frame ever sits
        # ahead of a pong. Discrete messages (prompt, enable_lora, audio)
        # still go FIFO and are never dropped.
        self._params_lock = threading.Lock()
        self._latest_params: str | None = None

        # Diagnostics (read in the close log).
        self._n_sent = 0
        self._n_recv = 0
        self._n_dropped = 0
        self._connect_t = 0.0
        self._last_recv_t = 0.0

        self._ws: websocket.WebSocket | None = None
        self._thread: threading.Thread | None = None
        self._closing = False
        # Close code parsed out of a server-sent close frame (see
        # _dispatch_frame); preferred over websocket-client's
        # close_status_code, which stays None under control_frame=False.
        self._server_close_code: int | None = None
        # A frame pulled off the queue but deferred because the socket
        # wasn't write-ready (pod momentarily not reading). Retried at
        # the front of the next flush so ordering + discrete messages
        # are never dropped. See _flush_outbound.
        self._pending_send: tuple[int, bytes | str] | None = None
        # Close code the recv thread should use when it tears the socket
        # down (set by close()). Kept so the actual socket .close() happens
        # on the recv thread — never concurrently with its recv.
        self._req_close_code = 1000
        # Serializes connect()/close() lifecycle transitions. Without it
        # a connect() racing a close() could reset `_closing` AFTER
        # close() set it and start a thread that ignores the close
        # request. (The socket itself stays single-threaded — this lock
        # only covers the flag + thread start.)
        self._lifecycle_lock = threading.Lock()

    # --- state ----------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        ws = self._ws
        return ws is not None and ws.connected

    # --- lifecycle ------------------------------------------------------------

    def connect(self) -> None:
        """Open the WS and start the recv thread. Non-blocking."""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                self._log(
                    f"[ws_client] connect ignored — thread already running")
                return
            self._closing = False
            self._thread = threading.Thread(
                target=self._run, name=f"ws_client[{self.url}]", daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        self._server_close_code = None
        self._pending_send = None
        with self._params_lock:
            self._latest_params = None
        try:
            self._log(f"[ws_client] dialing {self.url}")
            self._ws = websocket.create_connection(self.url, timeout=self._timeout)
            self._log(f"[ws_client] connected to {self.url}")
        except Exception as e:
            self._log(f"[ws_client] connect failed: {type(e).__name__}: {e}")
            if self._on_close:
                try:
                    self._on_close(None, str(e))
                except Exception:
                    pass
            return

        if self._on_open:
            try:
                self._on_open()
            except Exception as e:
                self._log(f"[ws_client] on_open raised: {e}")

        # Short socket timeout so the loop wakes ~10×/s to flush queued
        # outbound sends (params keepalive etc.). All socket I/O — both
        # recv AND send — happens ONLY on this thread, so the SSLSocket is
        # never read and written concurrently.
        try:
            self._ws.settimeout(self._RECV_TIMEOUT)
        except Exception:
            pass
        self._connect_t = time.monotonic()
        self._last_recv_t = self._connect_t

        # Recv loop
        close_code: int | None = None
        close_reason: str | None = None
        try:
            while not self._closing:
                # 1. Flush all queued outbound sends FIRST (same thread as
                #    recv → no concurrent SSL read/write). Drain fully so a
                #    burst of param messages goes out promptly.
                flush_err = self._flush_outbound()
                if flush_err is not None:
                    close_reason = flush_err
                    break
                if self._closing:
                    break

                # 2. Recv (blocks up to the 0.1 s socket timeout).
                try:
                    opcode, data = self._ws.recv_data(control_frame=False)
                    self._n_recv += 1
                    self._last_recv_t = time.monotonic()
                except websocket.WebSocketConnectionClosedException as e:
                    close_reason = f"closed: {e}"
                    break
                except websocket.WebSocketTimeoutException:
                    # Normal during quiet periods; loop back to flush
                    # outbound + recv again.
                    continue
                except Exception as e:
                    close_reason = f"recv error: {type(e).__name__}: {e}"
                    break

                reason = self._dispatch_frame(opcode, data)
                if reason is not None:
                    close_reason = reason
                    break
                # Ping/Pong handled by recv_data's control_frame=False filter.
        finally:
            try:
                if self._ws is not None:
                    if close_code is None:
                        close_code = (self._server_close_code
                                      if self._server_close_code is not None
                                      else getattr(self._ws,
                                                   "close_status_code", None))
                    self._ws.close(status=self._req_close_code)
            except Exception:
                pass
            # Rich close diagnostics — when the pod logs no error, these
            # numbers tell us whether WE closed (send fail / corruption)
            # vs the server, and how alive the link was.
            now = time.monotonic()
            uptime = now - self._connect_t if self._connect_t else 0.0
            since_recv = now - self._last_recv_t if self._last_recv_t else 0.0
            self._log(
                f"[ws_client] closed (code={close_code}, "
                f"reason={close_reason!r}) — uptime={uptime:.1f}s "
                f"sent={self._n_sent} recv={self._n_recv} "
                f"dropped={self._n_dropped} since_last_recv={since_recv:.1f}s "
                f"queued={self._outbound.qsize()}"
            )
            if self._on_close:
                try:
                    self._on_close(close_code, close_reason)
                except Exception:
                    pass
            self._ws = None

    def close(self, code: int = 1000, reason: str = "") -> None:
        """Stop the recv thread and close the socket.

        We do NOT touch the socket here — we signal `_closing` and let the
        recv thread perform the actual `ws.close()` in its finally block,
        so the SSLSocket is never accessed from two threads at once (the
        whole point of this client's single-thread-socket design). The recv
        loop wakes within RECV_TIMEOUT, sees the flag, and closes with
        `_req_close_code`.
        """
        with self._lifecycle_lock:
            self._req_close_code = code
            self._closing = True
            t = self._thread
        # Join OUTSIDE the lock (it can take up to 2 s; connect() must
        # not block that long — it'll just see _closing/thread state
        # consistently). Don't join from the recv thread itself (e.g.
        # when close() is reached via the on_close callback) — that
        # raises RuntimeError.
        if (t is not None and t.is_alive()
                and threading.current_thread() is not t):
            t.join(timeout=2.0)

    # --- send -----------------------------------------------------------------
    #
    # Callers (any thread) ENQUEUE here; the recv thread is the only thread
    # that touches the socket. Returns True if the message was queued (not
    # if it was actually written) — real write failures surface via the
    # recv loop -> on_close. This is what eliminates the concurrent
    # SSL read/write that was corrupting the connection.

    def _enqueue(self, opcode: int, payload: bytes | str, kind: str) -> bool:
        if self._closing or self._ws is None:
            return False
        try:
            self._outbound.put_nowait((opcode, payload))
            return True
        except queue.Full:
            # Socket is stalled and the queue backed up. Drop the OLDEST
            # (params are idempotent — the next snapshot supersedes it)
            # to make room, so a stall can't OOM or block the caller.
            self._n_dropped += 1
            try:
                self._outbound.get_nowait()
                self._outbound.put_nowait((opcode, payload))
                return True
            except Exception:
                self._log(f"[ws_client] {kind}: outbound queue full, dropped")
                return False

    def send_text(self, msg: str) -> bool:
        """Queue a text frame for the recv thread to send. Returns True if
        queued."""
        return self._enqueue(websocket.ABNF.OPCODE_TEXT, msg, "send_text")

    def send_binary(self, payload: bytes) -> bool:
        """Queue a binary frame for the recv thread to send. Returns True if
        queued."""
        return self._enqueue(websocket.ABNF.OPCODE_BINARY, payload,
                             "send_binary")

    def send_params(self, msg: str) -> bool:
        """Queue a params frame, COALESCED: overwrites any unsent params
        rather than appending. The newest playback_pos + knob snapshot is
        the only one worth sending, so a pod draining slower than the
        ~62 Hz pacer never accumulates a stale backlog that would
        head-of-line-block the keepalive pong. Drained after the discrete
        FIFO in _flush_outbound. Enqueue-only (recv thread sends); returns
        False only once closing."""
        if self._closing:
            return False
        with self._params_lock:
            self._latest_params = msg
        return True

    def _dispatch_frame(self, opcode: int, data) -> str | None:
        """Handle one received data/close frame. Shared by the main recv
        loop and the between-fragments socket servicing in
        _send_fragmented. Returns a close-reason string when the
        connection should stop, else None."""
        if opcode == websocket.ABNF.OPCODE_TEXT:
            if isinstance(data, bytes):
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    text = data.decode("utf-8", errors="replace")
            else:
                text = data
            if self._on_text:
                try:
                    self._on_text(text)
                except Exception as e:
                    self._log(f"[ws_client] on_text raised: {e}")
            return None
        if opcode == websocket.ABNF.OPCODE_BINARY:
            if self._on_binary:
                try:
                    self._on_binary(data)
                except Exception as e:
                    self._log(f"[ws_client] on_binary raised: {e}")
            return None
        if opcode == websocket.ABNF.OPCODE_CLOSE:
            # The close frame payload carries the server's code + reason
            # (2-byte BE code, then UTF-8 text). Because we recv with
            # control_frame=False, websocket-client hands the raw frame
            # to US and never fills close_status_code — parse it here or
            # the pod's last words (e.g. `1011 "keepalive ping timeout"`)
            # are thrown away and every server close logs as code=None.
            code, why = parse_close_frame(data)
            self._server_close_code = code
            return (f"server sent close: {why}" if why
                    else "server sent close")
        return None

    # Outbound messages above this size are sent as continuation
    # fragments with a socket-service pause between them. A single
    # multi-MB ws.send() (the ~46 MB initial audio upload for a 120 s
    # stereo source) blocks this — the ONLY — socket thread for the
    # whole TLS push; on a slow uplink that outlives the server's
    # keepalive ping window and the pod closes with
    # 1011 "keepalive ping timeout".
    _SEND_FRAGMENT_BYTES = 1 << 20  # 1 MiB
    # How long each between-fragments service pause may wait on the
    # socket. Long enough to pick up a pending PING, short enough that
    # the pauses add <1 s to a 46-fragment upload.
    _SERVICE_TIMEOUT = 0.01

    def _send_fragmented(self, opcode: int, payload: bytes | str) -> str | None:
        """Send one large message as RFC 6455 continuation fragments,
        servicing the socket between fragments so server PINGs are
        auto-ponged mid-upload (recv_data answers them internally).
        Control frames may legally interleave with a fragmented message
        (§5.4); other queued DATA messages may NOT, so params wait for
        the final fragment. Runs ONLY on the recv thread."""
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        total = len(payload)
        off = 0
        first = True
        while off < total:
            chunk = payload[off:off + self._SEND_FRAGMENT_BYTES]
            off += len(chunk)
            fin = 1 if off >= total else 0
            op = opcode if first else websocket.ABNF.OPCODE_CONT
            first = False
            try:
                self._ws.settimeout(self._timeout)
                self._ws.send_frame(
                    websocket.ABNF.create_frame(chunk, op, fin))
            except Exception as e:
                return f"send failed: {type(e).__name__}: {e}"
            finally:
                try:
                    self._ws.settimeout(self._RECV_TIMEOUT)
                except Exception:
                    pass
            if not fin:
                reason = self._service_socket_once()
                if reason is not None:
                    return reason
        return None

    def _service_socket_once(self) -> str | None:
        """Briefly read the socket between fragments of a large send. A
        pending server PING is answered inside recv_data (auto-pong);
        a data/close frame is dispatched exactly like the main loop's.
        Returns a close-reason string when the connection should stop."""
        try:
            self._ws.settimeout(self._SERVICE_TIMEOUT)
            opcode, data = self._ws.recv_data(control_frame=False)
        except websocket.WebSocketTimeoutException:
            return None
        except websocket.WebSocketConnectionClosedException as e:
            return f"closed: {e}"
        except Exception as e:
            return f"recv error: {type(e).__name__}: {e}"
        finally:
            try:
                self._ws.settimeout(self._RECV_TIMEOUT)
            except Exception:
                pass
        self._n_recv += 1
        self._last_recv_t = time.monotonic()
        return self._dispatch_frame(opcode, data)

    def _socket_writable(self) -> bool:
        """Is the socket's send buffer ready to accept a write right now?

        A 0-timeout select on the underlying fd. False means the pod
        isn't draining its side (e.g. busy generating the first window
        of a long source) and the TCP send buffer is full — entering a
        blocking ws.send() here would wedge this thread for up to
        `_timeout` (30 s), during which recv_data is never called and
        the server's keepalive PINGs go unanswered → 1011 / dropped
        connection. select failing (rare) is treated as writable so we
        fall through to a normal timed send rather than stalling sends
        forever."""
        sock = getattr(self._ws, "sock", None)
        if sock is None:
            return True
        try:
            _r, wr, _e = select.select([], [sock], [], 0)
            return bool(wr)
        except Exception:
            return True

    def _flush_outbound(self) -> str | None:
        """Send queued outbound frames. Runs ONLY on the recv thread.
        Returns a close-reason string on write failure, else None.

        Bails the moment the socket isn't write-ready, leaving the
        in-hand frame in `_pending_send` and the rest queued, so the
        recv loop promptly returns to recv_data and keeps answering the
        server's keepalive PINGs even while the pod is briefly not
        reading. This is what lets demonTD ride through a pod that's
        busy on first-window generation instead of wedging in a 30 s
        blocking send and getting 1011'd — the disconnect the VST never
        sees (its transport answers pings on an independent thread)."""
        # 1. Discrete FIFO (prompt / enable_lora / audio): ordered, never
        #    dropped.
        while True:
            if self._pending_send is not None:
                opcode, payload = self._pending_send
            else:
                try:
                    opcode, payload = self._outbound.get_nowait()
                except queue.Empty:
                    break
            if not self._socket_writable():
                # Defer this frame (don't drop it — could be a prompt /
                # enable_lora, not a droppable param) and yield to recv.
                self._pending_send = (opcode, payload)
                return None
            self._pending_send = None
            if len(payload) > self._SEND_FRAGMENT_BYTES and opcode in (
                    websocket.ABNF.OPCODE_TEXT,
                    websocket.ABNF.OPCODE_BINARY):
                err = self._send_fragmented(opcode, payload)
                if err is not None:
                    return err
                self._n_sent += 1
                continue
            try:
                # The recv loop runs a short socket timeout (RECV_TIMEOUT)
                # for responsiveness. A send can be moderately large and
                # would spuriously time out at that short value, so give
                # each write the full timeout, then restore the short one
                # for the next recv. Same thread, so no concurrency
                # concern. (Sends above _SEND_FRAGMENT_BYTES take the
                # fragmented path above instead.)
                self._ws.settimeout(self._timeout)
                self._ws.send(payload, opcode=opcode)
                self._n_sent += 1
            except Exception as e:
                return f"send failed: {type(e).__name__}: {e}"
            finally:
                try:
                    self._ws.settimeout(self._RECV_TIMEOUT)
                except Exception:
                    pass

        # 2. Coalesced params: at most ONE, the newest. Only pulled when
        #    the socket is write-ready, so it never sits ahead of a pong in
        #    a stuffed pipe. If not writable we leave it in the slot — the
        #    pacer keeps overwriting it with fresher values meanwhile.
        if self._socket_writable():
            with self._params_lock:
                params = self._latest_params
                self._latest_params = None
            if params is not None:
                try:
                    self._ws.settimeout(self._timeout)
                    self._ws.send(params, opcode=websocket.ABNF.OPCODE_TEXT)
                    self._n_sent += 1
                except Exception as e:
                    return f"send failed: {type(e).__name__}: {e}"
                finally:
                    try:
                        self._ws.settimeout(self._RECV_TIMEOUT)
                    except Exception:
                        pass
        return None

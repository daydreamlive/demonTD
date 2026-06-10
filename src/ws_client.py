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
import threading
import time
from typing import Callable

# Lazy import — wired in DemonExt's _prepend_vendor_path step.
# Outside TD: websocket-client must be on PYTHONPATH.
import websocket  # type: ignore[import-not-found]


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

        # Diagnostics (read in the close log).
        self._n_sent = 0
        self._n_recv = 0
        self._n_dropped = 0
        self._connect_t = 0.0
        self._last_recv_t = 0.0

        self._ws: websocket.WebSocket | None = None
        self._thread: threading.Thread | None = None
        self._closing = False
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
                elif opcode == websocket.ABNF.OPCODE_BINARY:
                    if self._on_binary:
                        try:
                            self._on_binary(data)
                        except Exception as e:
                            self._log(f"[ws_client] on_binary raised: {e}")
                elif opcode == websocket.ABNF.OPCODE_CLOSE:
                    close_reason = "server sent close"
                    break
                # Ping/Pong handled by recv_data's control_frame=False filter.
        finally:
            try:
                if self._ws is not None:
                    close_code = getattr(self._ws, "close_status_code", None)
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

    def _flush_outbound(self) -> str | None:
        """Send every queued outbound frame. Runs ONLY on the recv thread.
        Returns a close-reason string on write failure, else None."""
        while True:
            try:
                opcode, payload = self._outbound.get_nowait()
            except queue.Empty:
                return None
            try:
                # The recv loop runs a short socket timeout (RECV_TIMEOUT)
                # for responsiveness. A send can be large (the multi-MB
                # initial audio upload) and would spuriously time out at
                # that short value, so give each write the full timeout,
                # then restore the short one for the next recv. Same
                # thread, so no concurrency concern.
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

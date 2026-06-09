"""
DEMON queue API client.

Pure HTTP, no TouchDesigner dependencies — uses stdlib urllib so it runs in
TD's bundled Python with no extra wheels.

Endpoints (from demon-public-demo/lib/queue/client.ts):
  POST /api/queue/join     -> allocate or queue a session
  GET  /api/queue/status   -> poll position; bumps server heartbeat
  POST /api/queue/extend   -> bump expiry ("Still playing?")
  POST /api/queue/leave    -> release a session
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

# Single-source User-Agent (see src/version.py). In TD, sibling modules
# load via the `mod()` global; in unit tests they're importable from src/
# on sys.path. Mirrors demon_ext.py's sibling-import shim. Falls back to a
# literal so a missing module can never break a request — the UA header is
# advisory (it only lets the orchestrator tag the session as TouchDesigner).
try:
    USER_AGENT = mod('version').USER_AGENT  # type: ignore[name-defined]  # noqa: F821
except Exception:
    try:
        from version import USER_AGENT  # type: ignore
    except Exception:
        USER_AGENT = "DaydreamDEMON-TD/unknown"


@dataclass
class QueueResponse:
    # Status values observed from the server:
    #   "active"       - session has a wsUrl; play
    #   "queued"       - waiting; position / estimated_wait_ms populated
    #   "over_budget"  - paywall; deny_reason populated
    #   "unknown"      - default fallback for missing/unparseable bodies
    status: str
    session_id: str | None = None
    position: int | None = None      # 1-based when queued
    estimated_wait_ms: int | None = None
    session_duration_ms: int | None = None
    ws_url: str | None = None        # server-signed; only set when active
    pod_id: str | None = None
    expires_at: int | None = None    # absolute ms timestamp
    extensions_used: int | None = None
    deny_reason: str | None = None   # populated when status == "over_budget"
    soft_warning: str | None = None
    trial_seconds_remaining: int | None = None
    raw: dict[str, Any] = None       # type: ignore[assignment]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "QueueResponse":
        return cls(
            status=d.get("status", "unknown"),
            session_id=d.get("sessionId"),
            position=d.get("position"),
            estimated_wait_ms=d.get("estimatedWaitMs"),
            session_duration_ms=d.get("sessionDurationMs"),
            ws_url=d.get("wsUrl"),
            pod_id=d.get("podId"),
            expires_at=d.get("expiresAt"),
            extensions_used=d.get("extensionsUsed"),
            deny_reason=d.get("denyReason"),
            soft_warning=d.get("softWarning"),
            trial_seconds_remaining=d.get("trialSecondsRemaining"),
            raw=d,
        )


class QueueError(Exception):
    pass


class QueueClient:
    """Minimal HTTP client for the DEMON queue endpoints.

    Stateless apart from the configured base URL + optional API key.
    Each call is a one-shot request; reconnect logic lives in DemonExt.

    Timeouts default to 10s per request. Network errors raise QueueError.
    """

    def __init__(self, base_url: str, api_key: str | None = None,
                 timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or None
        self.timeout = timeout

    # ----- helpers ------------------------------------------------------------

    def _headers(self, json_body: bool = False) -> dict[str, str]:
        h = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if json_body:
            h["Content-Type"] = "application/json"
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _request(self, method: str, path: str, *,
                 body: dict[str, Any] | None = None,
                 query: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urlparse.urlencode(query)

        data = None
        headers = self._headers(json_body=body is not None)
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        req = urlrequest.Request(url, data=data, method=method, headers=headers)
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urlerror.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = "(unable to read error body)"
            raise QueueError(f"HTTP {e.code} on {method} {path}: {err_body}") from e
        except urlerror.URLError as e:
            raise QueueError(f"Network error on {method} {path}: {e}") from e

        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise QueueError(f"Bad JSON from {path}: {raw[:200]}") from e

    # ----- public API ---------------------------------------------------------

    def join(self, device_id: str | None = None,
             pod_id: str | None = None) -> QueueResponse:
        """POST /api/queue/join. Allocates or queues a session.

        Parameters
        ----------
        device_id
            Stable per-machine UUID. The server uses it for analytics and
            rate-limiting. Mirrors the RTMG VST's RTMGSession::start.
        pod_id
            Optional admin override — pins the join to a specific pod via
            the `?pod=` query string. Same semantics as the webapp's
            ?pod= URL override.
        """
        body: dict[str, Any] = {}
        if device_id:
            body["deviceId"] = device_id
        query = {"pod": pod_id} if pod_id else None
        d = self._request("POST", "/api/queue/join", body=body, query=query)
        return QueueResponse.from_dict(d)

    def status(self, session_id: str) -> QueueResponse:
        """GET /api/queue/status?token=<sessionId>. Also bumps server heartbeat."""
        d = self._request("GET", "/api/queue/status", query={"token": session_id})
        return QueueResponse.from_dict(d)

    def claim(self, session_id: str) -> None:
        """POST /api/queue/claim with {sessionId}. Best-effort.

        Called once after the WS opens in active state — cancels the
        server-side reservation-eviction timer. The RTMG VST does this
        in RTMGSession::applyResult when transitioning to Active.
        """
        try:
            self._request("POST", "/api/queue/claim", body={"sessionId": session_id})
        except QueueError:
            pass

    def extend(self, session_id: str) -> QueueResponse:
        """POST /api/queue/extend with {sessionId}. The 'Still playing?' button."""
        d = self._request("POST", "/api/queue/extend", body={"sessionId": session_id})
        return QueueResponse.from_dict(d)

    def leave(self, session_id: str) -> None:
        """POST /api/queue/leave with {sessionId}. Best-effort, ignores errors."""
        try:
            self._request("POST", "/api/queue/leave", body={"sessionId": session_id})
        except QueueError:
            pass

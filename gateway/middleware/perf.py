"""Request-timing middleware.

Every response carries an ``X-Response-Time-ms`` header (useful for
client-side perf budgets + for Cloudflare-side slow-request alerting).
Requests that cross SLOW_REQUEST_THRESHOLD_MS get a row appended to
``slow_request_log`` for the admin performance dashboard.

Design constraints that drive the shape of this module:

* **Never block the hot path.** The DB write is wrapped in a narrow
  try/except — a write failure logs and drops the row rather than
  serving a 500. Sampling is free (skip on fast requests), so the
  cost on p50 traffic is two time.time() calls + one header set.
* **Never leak PII.** We store the path (which can be a URL with an
  identifier in it — the /admin dashboard doesn't care about the
  user's username; it cares about which *handler* is slow), the
  user_id if the session has attached one, a hashed IP, and a coarse
  user-agent bucket (desktop/mobile/bot/other). Query strings are
  dropped entirely — they can contain tokens.
* **Cheap bucketing.** The admin page wants to ask "what's the p95
  latency of /api/feed over the last hour?" — that's why ``path`` is
  stored without query strings (groupable) and an index on
  ``(path, timestamp DESC)`` exists in migration 096.
* **Order in the middleware stack matters.** This middleware must be
  outermost so it measures the *real* wall-clock for the response,
  including every downstream middleware. See server.py for the
  explicit add_middleware ordering comment.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


log = logging.getLogger("perf.timing")


SLOW_REQUEST_THRESHOLD_MS = int(
    os.environ.get("SLOW_REQUEST_THRESHOLD_MS", "500")
)

# Paths that are noisy but fast — skip slow-log even if they cross the
# threshold (e.g. a /healthz that took 600 ms once because the kernel
# paused; no signal there). The header still gets set. Empty by default
# to keep the signal inclusive; extend if a particular path dominates
# the log and isn't actionable.
_SKIP_PATHS = frozenset()

# User-agent bucketer. We only care about the coarse shape for the
# admin dashboard — not the full UA string (PII / payload-size).
def _ua_bucket(ua: str) -> str:
    ua_l = (ua or "").lower()
    if not ua_l:
        return "none"
    if "bot" in ua_l or "crawl" in ua_l or "spider" in ua_l:
        return "bot"
    if "mobile" in ua_l or "iphone" in ua_l or "android" in ua_l:
        return "mobile"
    return "desktop"


def _hash_ip(request: Request) -> str:
    # Same algorithm used by analytics_events — keeps slow-log IPs
    # cross-referenceable with the request log without ever persisting
    # the raw address.
    ip = (
        request.headers.get("cf-connecting-ip")
        or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "")
        or "unknown"
    )
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()[:16]


def _log_slow_request(
    *,
    path: str,
    method: str,
    status_code: int,
    duration_ms: int,
    user_id: int | None,
    ip_hash: str,
    ua_kind: str,
) -> None:
    """Append a row to slow_request_log. Silent on failure."""
    try:
        import db
        with db.conn() as c:
            c.execute(
                "INSERT INTO slow_request_log "
                "(timestamp, path, method, status_code, duration_ms, "
                " user_id, ip_hash, user_agent_kind) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(time.time()),
                    path,
                    method,
                    status_code,
                    duration_ms,
                    user_id,
                    ip_hash,
                    ua_kind,
                ),
            )
    except Exception as e:  # pragma: no cover
        # No user-facing fallout — the header is already set by the time
        # this runs. Log at warning so infra can diff against a real
        # spike, not info-level noise.
        log.warning("slow_request_log write failed: %s", e)


class RequestTimingMiddleware(BaseHTTPMiddleware):
    """Measure every request, header every response, log the slow ones."""

    async def dispatch(self, request: Request, call_next):
        t0 = time.perf_counter()
        response: Response
        try:
            response = await call_next(request)
        except Exception:
            # Let the downstream exception propagate — FastAPI's handler
            # chain already turns unhandled exceptions into 500s with a
            # structured payload. Still record the slow read so infra
            # spots abuse that forces long error paths.
            duration_ms = int((time.perf_counter() - t0) * 1000)
            path = request.url.path
            if (
                duration_ms >= SLOW_REQUEST_THRESHOLD_MS
                and path not in _SKIP_PATHS
            ):
                _log_slow_request(
                    path=path,
                    method=request.method,
                    status_code=500,
                    duration_ms=duration_ms,
                    user_id=None,
                    ip_hash=_hash_ip(request),
                    ua_kind=_ua_bucket(request.headers.get("user-agent", "")),
                )
            raise

        duration_ms = int((time.perf_counter() - t0) * 1000)
        response.headers["X-Response-Time-ms"] = str(duration_ms)

        path = request.url.path
        if (
            duration_ms >= SLOW_REQUEST_THRESHOLD_MS
            and path not in _SKIP_PATHS
        ):
            # Attach user_id when the hardened session middleware has
            # already populated it. Missing/anonymous requests get NULL.
            user_id = None
            hardened = getattr(request.state, "hardened_user", None)
            if hardened and isinstance(hardened, dict):
                user_id = hardened.get("user_id")

            _log_slow_request(
                path=path,
                method=request.method,
                status_code=response.status_code,
                duration_ms=duration_ms,
                user_id=user_id,
                ip_hash=_hash_ip(request),
                ua_kind=_ua_bucket(request.headers.get("user-agent", "")),
            )
        return response

"""Request-timing middleware that feeds observability.perf_stats.

Registered after every other middleware so it wraps the whole stack — the
`duration_seconds` recorded is the full wall time from request-received to
response-sent, matching what a user experiences. Status codes that the
gate/staging/security middlewares return (e.g. 302 to /gate) are tracked
too, which is intentional: a spike of 3xx still shows up as slow requests
if, say, gate checks regress.

Cheap: one `time.perf_counter` on entry, one on exit, one dict lookup to
record. No per-request allocations beyond the stats bucket update.
"""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware

from observability.perf_stats import endpoint_stats

log = logging.getLogger("perf.middleware")


class PerformanceMiddleware(BaseHTTPMiddleware):
    """Records `(method, path, duration, status)` into `endpoint_stats`.

    Any exception in the handler chain is re-raised so existing Sentry /
    global-exception-handler behaviour is untouched — the perf sample is
    still recorded with status=500 so we can see where errors concentrate.
    """

    async def dispatch(self, request, call_next):
        t0 = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration = time.perf_counter() - t0
            try:
                endpoint_stats.record(
                    method=request.method,
                    path=request.url.path,
                    duration_seconds=duration,
                    status_code=status_code,
                )
            except Exception as exc:
                # Never let stat recording break the response.
                log.debug("perf middleware record failed: %s", exc)

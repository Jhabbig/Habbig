"""Session middleware — runs on EVERY request.

Reads the hardened `narve_session` cookie, validates it against the
`user_sessions` table (which stores SHA-256 hashes at rest), and
attaches the resolved user to `request.state.user`. On invalid or
missing cookies, `request.state.user` is set to None rather than
raising — route handlers and guard dependencies decide what to do
with that.

Also bumps `user_sessions.last_active_at` on valid sessions so the
admin panel's "Last active" column and the active-sessions UI stay
fresh without any extra DB writes from route handlers.

Installed from server.py via `app.add_middleware(SessionMiddleware)`.
Runs AFTER the SecurityHeadersMiddleware and AFTER the GateMiddleware
so redirects from the gate go out without the session lookup cost.
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware

from auth.guards import attach_session_to_request


log = logging.getLogger("auth.middleware")


# Paths that never need the session lookup. Static assets and the
# pre-release entry points are hit by anonymous traffic and would
# otherwise generate a wasted DB read on every request.
_SKIP_PREFIXES = (
    "/_gateway_static/",
    "/.well-known/",
)
_SKIP_EXACT = frozenset({
    "/",
    "/health",
    # Obscure sitemap path (not /sitemap.xml) — see server._SITEMAP_PATH.
    "/497951413996680578.xml",
    "/robots.txt",
    "/token",
    "/gate",
    "/terms",
    "/privacy",
    "/dpa",
    "/unsubscribe",
})


class SessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        # Static + always-public paths skip the DB lookup entirely.
        if path in _SKIP_EXACT or any(path.startswith(p) for p in _SKIP_PREFIXES):
            request.state.user = None
            request.state.hardened_user = None
            return await call_next(request)
        # Never lets a lookup failure kill a request — just leaves
        # request.state.user = None and the guards decide what to do.
        try:
            attach_session_to_request(request)
        except Exception as e:
            log.warning("session middleware failed: %s", e)
            request.state.hardened_user = None
        # Mirror to request.state.user for the spec's naming convention.
        request.state.user = getattr(request.state, "hardened_user", None)
        return await call_next(request)

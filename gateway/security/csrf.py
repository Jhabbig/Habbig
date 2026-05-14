"""
CSRF protection — double-submit cookie with session-based validation.

For authenticated users: token is stored in the session DB row and validated
against the submitted token (header or form field).

For unauthenticated users: falls back to pure double-submit cookie pattern
(cookie value vs submitted value).

Token properties:
  - 32 bytes, URL-safe base64 encoded (43 chars)
  - Rotated on login, every 2 hours, and invalidated on logout
  - Non-HttpOnly cookie (JS must read it for fetch/XHR/HTMX)
  - SameSite=Lax, Secure in production
"""

from __future__ import annotations

import hmac
import html
import logging
import os
import secrets
import time
from typing import Optional

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger("gateway")

CSRF_TOKEN_LENGTH = 32
CSRF_ROTATION_SECONDS = 7200  # 2 hours
CSRF_COOKIE_NAME = "_csrf"
CSRF_HEADER_NAME = "x-csrf-token"
CSRF_FORM_FIELD = "_csrf"

# Paths that skip CSRF validation entirely (static files, websocket, etc.)
_CSRF_SKIP_PREFIXES = ("/_gateway_static", "/ws")

# Paths exempt from CSRF because they use API-key or JWT auth, not cookies.
# POST to these paths will NOT be CSRF-validated.
#
# Audit guidance: keep this list as narrow as exact-match paths. Prefix-style
# exemptions are fragile — any future `/foo/<anything>` route silently inherits
# the bypass and the only reviewer who notices is the next pentester. List the
# specific endpoint(s) you want exempt and add a comment explaining why CSRF
# doesn't apply (Bearer auth, webhook signature, etc.).
_CSRF_EXEMPT_PATHS = frozenset({
    "/stripe/webhook",
    "/health",
    # Public prerelease newsletter signup — no user session to anchor CSRF to.
    # Still protected by per-IP rate limit + email format validation.
    "/api/newsletter",
    # Scraper service → main server push of freshly scraped posts. Authenticated
    # via the X-Scraper-API-Key header (see scraper/transmission/pusher.py); a
    # forged cross-origin request can't read or replay that header, so CSRF
    # adds nothing. This is the only `/api/scraper/*` path the main gateway
    # actually serves — the admin panel's other "/api/scraper/<x>" calls hit
    # the same X-Scraper-API-Key boundary (also Bearer-equivalent), but those
    # routes don't exist on the main app today so listing them here would be
    # speculative. Add them explicitly if/when they're introduced.
    "/api/scraper/ingest",
})

# Prefix-style exemptions are intentionally empty. The previous broad
# "/api/scraper/" entry let any `/api/scraper/<anything>` POST bypass CSRF
# even if such a route was later added with cookie auth — audit MED #3.
_CSRF_EXEMPT_PREFIXES: tuple[str, ...] = ()

CSRF_ENABLED = os.environ.get("CSRF_ENABLED", "true").lower() not in ("0", "false", "no", "off")

# Phase-1 rollout flag for PATCH/PUT/DELETE enforcement. When false (default),
# the middleware still inspects these verbs but lets failing requests through
# with a warning log — useful while we verify every client sends x-csrf-token
# on non-POST mutating requests. When true, behaves identically to POST.
# Mirror of the same flag in gateway/server.py — keep the two in lockstep.
CSRF_PATCH_DELETE_ENFORCE = os.environ.get(
    "CSRF_PATCH_DELETE_ENFORCE", "false"
).lower() in ("1", "true", "yes", "on")


def generate_csrf_token() -> str:
    """Generate a cryptographically secure CSRF token."""
    return secrets.token_urlsafe(CSRF_TOKEN_LENGTH)


def set_csrf_cookie(response, token: str, request, *, is_production: bool = False,
                    cookie_domain_fn=None) -> None:
    """Set the non-HttpOnly CSRF cookie so JavaScript can read it."""
    domain = cookie_domain_fn(request) if cookie_domain_fn and is_production else None
    kwargs = dict(
        key=CSRF_COOKIE_NAME,
        value=token,
        max_age=CSRF_ROTATION_SECONDS,
        httponly=False,
        samesite="lax",
        secure=is_production,
        path="/",
    )
    if domain:
        kwargs["domain"] = domain
    response.set_cookie(**kwargs)


def validate_csrf_token(cookie_token: Optional[str], submitted_token: Optional[str],
                        session_token: Optional[str] = None,
                        session_csrf_created_at: Optional[int] = None) -> tuple[bool, str]:
    """
    Validate a CSRF token.

    Checks (in order):
    1. A submitted token exists (from header or form field)
    2. If a session token is available, compare against that (session-based)
    3. Otherwise compare against the cookie token (double-submit fallback)
    4. Token is not expired (< 2 hours old) if timestamp available

    Returns (is_valid, error_reason).
    """
    if not submitted_token:
        return False, "missing"

    # Prefer session-based validation for authenticated users
    reference_token = session_token if session_token else cookie_token

    if not reference_token:
        return False, "no_reference"

    # Constant-time comparison
    if not hmac.compare_digest(reference_token, submitted_token):
        return False, "mismatch"

    # Check expiry if timestamp available
    if session_csrf_created_at:
        age = int(time.time()) - session_csrf_created_at
        if age > CSRF_ROTATION_SECONDS:
            return False, "expired"

    return True, ""


def csrf_hidden_field(token: str) -> str:
    """Return an HTML hidden input for the CSRF token."""
    return f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{html.escape(token)}">'


class CSRFMiddleware(BaseHTTPMiddleware):
    """
    Double-submit cookie CSRF protection with session-based validation.

    - On GET requests to HTML pages: ensures _csrf cookie is set.
    - On POST/PUT/PATCH/DELETE: validates submitted token against cookie/session.
    - Skips static files, WebSocket, and reverse-proxied subdomain routes.
    - Skips API-key authenticated endpoints (scraper, stripe webhook).
    - Logs all CSRF failures with IP, path, method.
    """

    def __init__(self, app, *, is_production: bool = False, domain: str = "",
                 cookie_domain_fn=None, get_client_ip_fn=None,
                 get_session_csrf_fn=None):
        super().__init__(app)
        self.is_production = is_production
        self.domain = domain
        self.cookie_domain_fn = cookie_domain_fn
        self.get_client_ip_fn = get_client_ip_fn or (lambda r: r.client.host if r.client else "unknown")
        self.get_session_csrf_fn = get_session_csrf_fn

    async def dispatch(self, request, call_next):
        if not CSRF_ENABLED:
            return await call_next(request)

        path = request.url.path

        # Skip CSRF for static/ws paths
        if any(path.startswith(p) for p in _CSRF_SKIP_PREFIXES):
            return await call_next(request)

        # Skip for subdomain-proxied requests
        host = request.headers.get("host", "")
        if self.is_production and self.domain and host != self.domain and host.endswith(f".{self.domain}"):
            return await call_next(request)

        # Skip exempt paths (API-key auth, webhooks)
        if path in _CSRF_EXEMPT_PATHS:
            return await call_next(request)
        if any(path.startswith(p) for p in _CSRF_EXEMPT_PREFIXES):
            return await call_next(request)

        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            # Phase-1 rollout: PATCH/PUT/DELETE failures only produce a 403
            # when CSRF_PATCH_DELETE_ENFORCE is true. POST always enforces.
            enforce_failure = (request.method == "POST") or CSRF_PATCH_DELETE_ENFORCE
            # Extract submitted token from form field or header
            content_type = request.headers.get("content-type", "")
            submitted_token = None

            if "application/json" in content_type:
                submitted_token = request.headers.get(CSRF_HEADER_NAME)
            elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
                form = await request.form()
                submitted_token = form.get(CSRF_FORM_FIELD)
                # Also check header as fallback
                if not submitted_token:
                    submitted_token = request.headers.get(CSRF_HEADER_NAME)
            else:
                submitted_token = request.headers.get(CSRF_HEADER_NAME)

            # Origin/Referer check as secondary defense
            origin = request.headers.get("origin")
            if origin and self.is_production and self.domain:
                from urllib.parse import urlparse
                parsed = urlparse(origin)
                if parsed.hostname and parsed.hostname != self.domain and not parsed.hostname.endswith(f".{self.domain}"):
                    ip = self.get_client_ip_fn(request)
                    log.warning("CSRF origin mismatch: origin=%s path=%s ip=%s", origin, path, ip)
                    from security.logger import log_csrf_failure
                    log_csrf_failure(request, reason="origin_mismatch",
                                    ip=ip)
                    return JSONResponse(
                        {"error": "Invalid origin"},
                        status_code=403,
                        headers={"X-CSRF-Error": "origin"},
                    )

            # Get session-based CSRF token if available
            session_token = None
            session_csrf_created_at = None
            if self.get_session_csrf_fn:
                session_csrf = self.get_session_csrf_fn(request)
                if session_csrf:
                    session_token = session_csrf.get("csrf_token")
                    session_csrf_created_at = session_csrf.get("csrf_created_at")

            cookie_token = request.cookies.get(CSRF_COOKIE_NAME)

            valid, reason = validate_csrf_token(
                cookie_token=cookie_token,
                submitted_token=submitted_token,
                session_token=session_token,
                session_csrf_created_at=session_csrf_created_at,
            )

            if not valid:
                ip = self.get_client_ip_fn(request)
                log.warning("CSRF validation failed: reason=%s path=%s method=%s ip=%s",
                            reason, path, request.method, ip)
                from security.logger import log_csrf_failure
                log_csrf_failure(request, reason=reason, ip=ip)
                if enforce_failure:
                    return JSONResponse(
                        {"error": "CSRF validation failed"},
                        status_code=403,
                        headers={"X-CSRF-Error": reason},
                    )
                # Soft-warn mode: let PATCH/PUT/DELETE through during Phase 1
                # rollout so we get telemetry without breaking clients that
                # haven't been migrated yet. Flip CSRF_PATCH_DELETE_ENFORCE
                # in Phase 2 once the warning rate is zero.
                log.warning(
                    "CSRF soft-warn: %s %s reason=%s ip=%s "
                    "(CSRF_PATCH_DELETE_ENFORCE=false)",
                    request.method, path, reason, ip,
                )

        # Pre-generate CSRF token for first-visit GET requests
        if request.method == "GET" and not request.cookies.get(CSRF_COOKIE_NAME):
            request.state.csrf_token = generate_csrf_token()

        response = await call_next(request)

        # Set CSRF cookie on GET HTML responses if not present
        csrf_token = getattr(request.state, "csrf_token", None)
        if csrf_token:
            ct = response.headers.get("content-type", "")
            if "text/html" in ct:
                set_csrf_cookie(response, csrf_token, request,
                                is_production=self.is_production,
                                cookie_domain_fn=self.cookie_domain_fn)

        return response

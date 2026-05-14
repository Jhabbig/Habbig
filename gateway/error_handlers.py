"""Centralised FastAPI exception handlers.

Every error — HTTPException, validation error, unhandled exception —
flows through this module so:

  1. JSON API callers get the same shape every time.
  2. HTML visitors get a branded error page with a request-id they
     can quote to support.
  3. Nothing leaks the internal message / stack trace / DB detail.
  4. Every 5xx is logged with traceback + request_id for triage.

Error envelope (JSON):

    {
      "error":       "slug_code",           # machine-readable
      "message":     "Human-readable.",     # safe to show users
      "request_id":  "abc12345",            # for support tickets
      "details":     {...}                  # optional: validation fields
    }

Slug codes are documented in ERROR_HANDLING.md.
"""

from __future__ import annotations

import logging
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException


log = logging.getLogger("gateway.errors")


# ── Error code slugs ──────────────────────────────────────────────────
# Stable machine-readable names. Clients switch on these, not status
# codes, so the wire contract survives status-code refactors.

_STATUS_TO_SLUG: dict[int, str] = {
    400: "bad_request",
    401: "authentication_required",
    402: "subscription_required",
    403: "authorization_required",
    404: "resource_not_found",
    405: "method_not_allowed",
    409: "duplicate_resource",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_failed",
    429: "rate_limit_exceeded",
    500: "internal_error",
    502: "upstream_error",
    503: "service_unavailable",
    504: "upstream_timeout",
}

_STATUS_TO_TITLE: dict[int, str] = {
    400: "Bad request",
    401: "Sign in to continue",
    402: "Subscription required",
    403: "You don't have access",
    404: "This page does not exist",
    409: "Already exists",
    422: "Check your input",
    429: "Slow down a touch",
    500: "Something broke on our end",
    502: "Upstream error",
    503: "Temporarily down for maintenance",
    504: "Upstream timeout",
}

# Safe generic messages. Never echoes internal detail.
#
# Copy notes:
#   - 401 vs 403 vs 404 are three distinct states. 401 = you're not signed
#     in; 403 = signed in but lacking access; 404 = page does not exist for
#     you (which, per the existence-hide rule, may overlap with 403 on
#     private resources). Each message has to make the distinction clear.
#   - 402 sells the Pro tier honestly: 13 subproducts + platform, one plan.
#   - 429 talks to a human clicking too fast, not a misbehaving bot.
#   - 500 apologises and points to the request id below.
_STATUS_TO_MESSAGE: dict[int, str] = {
    400: "That request was malformed. Double-check and try again.",
    401: "You need to sign in to see this. If you already have an account, the link below will take you there.",
    402: "This is a Pro feature. narve.ai Pro unlocks all 13 subproducts and the full platform on one subscription.",
    403: "You're signed in, but this account doesn't have access to this resource.",
    404: "This page does not exist. It may have been moved, the link may be wrong, or you may not have access to it.",
    409: "A resource with the same identifier already exists.",
    422: "Some fields need attention.",
    429: "You're clicking faster than our servers can keep up. Give it a moment and try again.",
    500: "Sorry — something broke on our end. The error has been reported and we're looking at it. Quote the request id below if you contact support.",
    502: "One of our upstream services returned an error.",
    503: "narve.ai is briefly offline for maintenance. We'll be back shortly — live updates at /status.",
    504: "An upstream service took too long to respond.",
}


# ── 404-only curated top links ────────────────────────────────────────
# Hand-picked destinations users are most likely to want when a URL
# 404s. Order matters — these render top-to-bottom in the grid.
#
# The list deliberately points at platform-wide surfaces rather than
# specific subproducts: a 404'd user has no idea what they were after,
# so we send them to the hub (/dashboards), the price page, what's-new,
# the about page, plus one or two subproduct roots that double as
# entrypoints (collections + users). Subproduct slugs live in
# subproduct.py — if that list grows we update here too.

_TOP_LINKS_404: list[tuple[str, str]] = [
    ("Dashboards — main hub", "/dashboards"),
    ("Pricing — see plans", "/pricing"),
    ("Changelog — what's new", "/changelog"),
    ("About narve.ai", "/about"),
    ("Browse collections", "/c/"),
    ("Browse users", "/u/"),
]


def slug_for_status(status: int) -> str:
    return _STATUS_TO_SLUG.get(status, "error")


def generate_request_id() -> str:
    """8-char opaque request id users can quote to support."""
    return secrets.token_hex(4)


def get_request_id(request: Request) -> str:
    """Pull the request_id off request.state, or mint a fresh one.

    The RequestIDMiddleware (see below) stamps every incoming request;
    error paths invoked outside that middleware still get a valid id.
    """
    existing = getattr(request.state, "request_id", None) if hasattr(request, "state") else None
    if isinstance(existing, str) and existing:
        return existing
    fresh = generate_request_id()
    try:
        request.state.request_id = fresh
    except Exception:
        pass
    return fresh


def is_api_request(request: Request) -> bool:
    """True when the caller wants JSON.

    A request counts as API if any one of these holds:
      - path starts with /api/
      - Accept header prefers application/json
      - Content-Type is application/json
    """
    path = request.url.path or ""
    if path.startswith("/api/"):
        return True
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept and "text/html" not in accept:
        return True
    ct = (request.headers.get("content-type") or "").lower()
    if ct.startswith("application/json"):
        return True
    return False


# ── Error page rendering ──────────────────────────────────────────────
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_ERROR_TEMPLATE_PATH = _STATIC_DIR / "error_page.html"


def _load_template() -> str:
    try:
        return _ERROR_TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError:
        # Fall back to a minimal inline shell; still branded but bare.
        return _FALLBACK_TEMPLATE


_FALLBACK_TEMPLATE = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<title>{title} — narve.ai</title>"
    "<style>body{font-family:system-ui;padding:48px;max-width:560px;margin:auto;color:#111}"
    "h1{font-weight:600;letter-spacing:-0.01em}a{color:#111}"
    ".rid{color:#9ca3af;font-size:11px;font-family:ui-monospace,monospace}</style>"
    "</head><body><h1>{title}</h1><p>{message}</p>"
    "<p class='rid'>Request ID: {request_id}</p></body></html>"
)


def render_error_page(
    request: Request,
    *,
    status: int,
    title: Optional[str] = None,
    message: Optional[str] = None,
    retry_after: Optional[int] = None,
) -> HTMLResponse:
    """Render the shared error-page template with status-specific copy."""
    request_id = get_request_id(request)
    title_str = title or _STATUS_TO_TITLE.get(status, "Error")
    message_str = message or _STATUS_TO_MESSAGE.get(status, "Something went wrong.")

    tpl = _load_template()
    # Template placeholders use {{ name }} so we can swap them with str.replace
    # without pulling in jinja — this page must never fail to render.
    actions_html = _action_buttons_for_status(status)
    retry_line = ""
    if status == 429 and isinstance(retry_after, int) and retry_after > 0:
        retry_line = (
            f'<p class="nv-error__retry">Try again in '
            f'{int(retry_after)} seconds.</p>'
        )
    extra_line = ""
    if status == 402:
        extra_line = (
            '<p class="nv-error__extra">'
            'narve.ai Pro bundles all 13 subproducts plus the full platform '
            'on a single subscription — <a href="/pricing">see pricing</a>.'
            '</p>'
        )
    elif status == 503:
        # Maintenance window — copy is intentionally vague (we don't
        # always know ETA when 503 fires) and points at the status page.
        extra_line = (
            '<p class="nv-error__extra">Follow recovery progress on the '
            '<a href="/status">status page</a>.</p>'
        )
    elif status == 401:
        # 401 is the one place we can hint at distinct 403 / 404 cases —
        # if the user lands here without an account, the invite flow.
        extra_line = (
            '<p class="nv-error__extra">No account yet? '
            '<a href="/enquire">Request an invite</a>.</p>'
        )

    # 404 gets a search box — quickest way to get unstuck. The canonical
    # site search lives at /signal-search (post-redesign there is no
    # bare /search HTML route); it accepts ?q= and renders matching
    # markets, sources, and predictions.
    search_block = ""
    if status == 404:
        search_block = (
            '<form class="nv-error__search" action="/signal-search" '
            'method="get" role="search" aria-label="Site search">'
            '<label for="nv-error-q" class="nv-sr-only">'
            'Search markets, sources, predictions</label>'
            '<input id="nv-error-q" type="search" name="q" '
            'placeholder="Search markets, sources, predictions" '
            'autocomplete="off" autofocus>'
            '<button type="submit">Search</button>'
            '</form>'
        )

    # Curated "try these instead" — only 404. Other statuses don't need
    # the cognitive load: their actions block already points home.
    links_block = ""
    if status == 404 and _TOP_LINKS_404:
        items = "\n".join(
            f'  <li><a href="{_html_escape(href)}">{_html_escape(label)}</a></li>'
            for label, href in _TOP_LINKS_404
        )
        links_block = (
            '<div class="nv-error__links">'
            '<h3>Try these instead</h3>'
            f'<ul>\n{items}\n</ul>'
            '</div>'
        )

    # Request ID is only useful when there's a server-side incident
    # worth quoting in a support ticket — 5xx. Showing it for 404 / 403
    # adds noise without action.
    meta_block = ""
    if status >= 500:
        meta_block = (
            '<p class="nv-error__meta">Request ID: '
            f'<code>{_html_escape(request_id)}</code></p>'
        )

    body = (
        tpl
        .replace("{{ status }}", str(status))
        .replace("{{ title }}", _html_escape(title_str))
        .replace("{{ message }}", _html_escape(message_str))
        .replace("{{ request_id }}", _html_escape(request_id))
        .replace("{{ actions }}", actions_html)
        .replace("{{ retry_line }}", retry_line)
        .replace("{{ extra_line }}", extra_line)
        .replace("{{ search_block }}", search_block)
        .replace("{{ links_block }}", links_block)
        .replace("{{ meta_block }}", meta_block)
    )
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return HTMLResponse(body, status_code=status, headers=headers)


def _action_buttons_for_status(status: int) -> str:
    """CTA buttons tailored per status. Kept in one place so copy stays
    consistent across the template."""
    buttons: list[tuple[str, str]] = []
    if status == 401:
        buttons = [("/login", "Sign in"), ("/enquire", "Request an invite")]
    elif status == 402:
        buttons = [("/pricing", "View pricing"),
                   ("/dashboards", "Back to dashboard")]
    elif status == 403:
        # No emoji, no SVG icon — the audit flagged 403 as overdesigned.
        # Two clean buttons, primary first.
        buttons = [("/pricing", "View pricing"),
                   ("/dashboards", "Back to dashboard")]
    elif status == 404:
        buttons = [("/dashboards", "Dashboard"),
                   ("/dashboard/markets", "Browse markets")]
    elif status == 429:
        # JS pseudo-link — the only sensible action when rate-limited is
        # to wait + go back. Pages still load with JS off; href just
        # becomes a no-op.
        buttons = [("javascript:history.back()", "Back")]
    elif status == 500:
        buttons = [
            ("javascript:location.reload()", "Retry"),
            ("/dashboards", "Dashboard"),
        ]
    elif status == 503:
        buttons = [("/status", "Status page")]
    else:
        buttons = [("/dashboards", "Back to dashboard")]

    parts: list[str] = []
    for href, label in buttons:
        parts.append(
            f'<a class="err-btn" href="{_html_escape(href)}">{_html_escape(label)}</a>'
        )
    return "\n".join(parts)


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ── Envelope builders ─────────────────────────────────────────────────

def _json_envelope(
    *,
    status: int,
    slug: str,
    message: str,
    request_id: str,
    details: Optional[Any] = None,
    headers: Optional[dict] = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "error": slug,
        "message": message,
        "request_id": request_id,
    }
    if details is not None:
        body["details"] = details
    return JSONResponse(body, status_code=status, headers=headers or {})


# ── Handlers ──────────────────────────────────────────────────────────

async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
    """Known HTTP errors (raised by app code as HTTPException)."""
    request_id = get_request_id(request)
    status = exc.status_code
    slug = slug_for_status(status)
    # exc.detail may be a short human string — if safe, echo it.
    # Treat anything over 200 chars or containing trace-like substrings
    # as unsafe and fall back to the generic copy.
    message = _STATUS_TO_MESSAGE.get(status, "Something went wrong.")
    details: Optional[Any] = None
    if isinstance(exc.detail, str) and exc.detail and not _looks_like_trace(exc.detail):
        message = exc.detail
    elif isinstance(exc.detail, dict) and exc.detail:
        # Structured input-hygiene errors come as {"error": "msg", "field": "x"}.
        inner = exc.detail.get("error") or exc.detail.get("message")
        if isinstance(inner, str) and inner and not _looks_like_trace(inner):
            message = inner
        # Pass through extra context (e.g. field name) without leaking the
        # full dict — only fields the handler explicitly recognises.
        extras = {k: v for k, v in exc.detail.items() if k in ("field", "code")}
        if extras:
            details = extras
    retry_after: Optional[int] = None
    headers = dict(exc.headers or {})
    if "Retry-After" in headers:
        try:
            retry_after = int(headers["Retry-After"])
        except ValueError:
            retry_after = None

    if is_api_request(request):
        return _json_envelope(
            status=status,
            slug=slug,
            message=message,
            request_id=request_id,
            details=details,
            headers=headers,
        )
    return render_error_page(
        request,
        status=status,
        message=message,
        retry_after=retry_after,
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> Response:
    """Pydantic validation → 422 with per-field details."""
    request_id = get_request_id(request)
    errors = []
    for e in exc.errors():
        loc = e.get("loc") or []
        # Strip the first item ("body", "query", etc.) for a tidier path.
        field_parts = [str(x) for x in loc[1:]] or [str(x) for x in loc]
        errors.append({
            "field": ".".join(field_parts),
            "message": _sanitize_validation_msg(str(e.get("msg") or "Invalid")),
        })
    if is_api_request(request):
        return _json_envelope(
            status=422,
            slug="validation_failed",
            message="Some fields need attention.",
            request_id=request_id,
            details={"errors": errors},
        )
    return render_error_page(request, status=422, message="Some fields need attention.")


async def app_exception_handler(request: Request, exc: Exception) -> Response:
    """Catch-all for anything an app handler raises that isn't an
    HTTPException or ValidationError. Logs the traceback; returns a
    generic 500 to the client. Never leaks the exception's message.
    """
    request_id = get_request_id(request)
    log.exception(
        "unhandled exception",
        extra={
            "request_id": request_id,
            "path": request.url.path,
            "method": request.method,
        },
    )
    if is_api_request(request):
        return _json_envelope(
            status=500,
            slug="internal_error",
            message=_STATUS_TO_MESSAGE[500],
            request_id=request_id,
        )
    return render_error_page(request, status=500)


# ── Heuristics ────────────────────────────────────────────────────────

def _looks_like_trace(s: str) -> bool:
    """Spot obvious stack-trace / DB-error tokens that shouldn't leak
    into the `message` field. Conservative — false positives are fine
    (we fall back to a generic message), false negatives leak details."""
    if len(s) > 240:
        return True
    tokens = (
        "Traceback",
        "traceback",
        " at 0x",
        "sqlite3.",
        "IntegrityError",
        "OperationalError",
        "psycopg",
        "column ",
        "FOREIGN KEY",
        "UNIQUE constraint",
        "NOT NULL constraint",
    )
    return any(t in s for t in tokens)


def _sanitize_validation_msg(msg: str) -> str:
    """Trim pydantic's chatty messages for end users."""
    if _looks_like_trace(msg):
        return "Invalid value."
    return msg[:200]


# ── Request ID middleware ─────────────────────────────────────────────

from starlette.middleware.base import BaseHTTPMiddleware


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Stamps every request with a short id on request.state and echoes
    it in the X-Request-ID response header.

    Respects an incoming X-Request-ID (trust proxy / tests / retries)
    as long as it's short + ascii-clean. Otherwise mints a fresh one.
    """

    MAX_INBOUND_LEN = 64

    async def dispatch(self, request, call_next):
        incoming = request.headers.get("x-request-id", "") or ""
        if incoming and len(incoming) <= self.MAX_INBOUND_LEN and incoming.isprintable() and " " not in incoming:
            rid = incoming
        else:
            rid = generate_request_id()
        request.state.request_id = rid
        response: Response = await call_next(request)
        # Don't stomp an upstream-set header.
        response.headers.setdefault("X-Request-ID", rid)
        return response


# ── Registration ──────────────────────────────────────────────────────

def register(app) -> None:
    """Attach every handler + the RequestID middleware to a FastAPI app."""
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, app_exception_handler)
    # Middleware last so it wraps everything else.
    app.add_middleware(RequestIDMiddleware)
    log.info("error_handlers registered (http + validation + fallback + request_id)")

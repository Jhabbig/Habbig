"""Cookie helpers for the hardened session.

After the 2026-05-15 auth refactor the only cookie this module manages
is the long-lived hardened session cookie. The dead ``pending_token``
machinery (used by the removed ``/token`` invite flow) is gone.

  - session : long-lived (7 days), HttpOnly, Secure, SameSite=Strict.
              Holds the raw hardened session token; server-side it
              hashes and looks up in ``user_sessions``.

The cookie is domain-scoped to the apex in production so it covers
every subdomain (crypto.narve.ai, etc). In dev the Domain attribute is
omitted so localhost works.
"""

from __future__ import annotations

import os
import secrets
from typing import Optional

from fastapi import Request, Response


SESSION_COOKIE = "narve_session"  # NB: new cookie, NOT pm_gateway_session

# Session cookie lifetime. Override with SESSION_COOKIE_TTL_DAYS env var.
SESSION_COOKIE_TTL = int(os.environ.get("SESSION_COOKIE_TTL_DAYS", "7")) * 24 * 60 * 60


# Anonymous visitor-tracking cookie. Opaque 22-char URL-safe ID minted on
# first visit and persisted for 1 year so analytics events can be linked
# across sessions without PII. Readable by JS (HttpOnly=False) because
# the static analytics tracker echoes the value back as ``session_id`` on
# every ping. Not signed — it's an opaque correlator, not a credential.
VISITOR_COOKIE = "narve_visitor"
VISITOR_TTL = 365 * 86400  # 1 year


def _is_production() -> bool:
    return os.environ.get("PRODUCTION", "").lower() in ("1", "true", "yes", "on")


def _cookie_domain_for(request: Request) -> Optional[str]:
    """In production scope cookies to the apex; in dev leave unset."""
    if not _is_production():
        return None
    domain = os.environ.get("GATEWAY_COOKIE_DOMAIN", "").strip()
    if domain:
        return domain
    # Fall back to reading from config.json via the main server module.
    try:
        import json
        from pathlib import Path
        cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text())
        apex = cfg.get("domain", "")
        if apex:
            return f".{apex}"
    except Exception:
        return None
    return None


def set_session_cookie_hardened(response: Response, raw_session_token: str, request: Request) -> None:
    kwargs = dict(
        key=SESSION_COOKIE,
        value=raw_session_token,
        max_age=SESSION_COOKIE_TTL,
        httponly=True,
        samesite="strict",
        secure=_is_production(),
        path="/",
    )
    domain = _cookie_domain_for(request)
    if domain:
        kwargs["domain"] = domain
    response.set_cookie(**kwargs)


def clear_session_cookie_hardened(response: Response, request: Request) -> None:
    kwargs = dict(key=SESSION_COOKIE, path="/")
    domain = _cookie_domain_for(request)
    if domain:
        kwargs["domain"] = domain
    response.delete_cookie(**kwargs)


def read_visitor_cookie(request: Request) -> Optional[str]:
    """Return the existing ``narve_visitor`` cookie value, or None."""
    val = request.cookies.get(VISITOR_COOKIE)
    if not val:
        return None
    # Light sanity guard: opaque IDs are URL-safe base64 (~22 chars). Reject
    # anything wildly out of range so a tampered/oversized value can't get
    # written through to analytics joins downstream.
    if len(val) > 64:
        return None
    return val


def set_visitor_cookie(response: Response, request: Request) -> str:
    """Mint a fresh opaque visitor ID and set it on ``response``.

    Returns the new value so callers can echo it into a body inject or log.
    ``HttpOnly=False`` because the analytics tracker (analytics.js) reads
    it from ``document.cookie`` and ships it as ``session_id`` on every
    event ping. ``SameSite=Lax`` keeps it on top-level navigations from
    other origins (shared links, OG previews) without leaking on
    cross-site POSTs.
    """
    value = secrets.token_urlsafe(16)  # 22-char URL-safe opaque ID
    kwargs = dict(
        key=VISITOR_COOKIE,
        value=value,
        max_age=VISITOR_TTL,
        httponly=False,  # JS reads this; intentional
        samesite="lax",
        secure=_is_production(),
        path="/",
    )
    domain = _cookie_domain_for(request)
    if domain:
        kwargs["domain"] = domain
    response.set_cookie(**kwargs)
    return value


# Public alias so other modules can import the cookie-domain helper
# without reaching into a private name. Returns ".narve.ai" in production
# (or whatever GATEWAY_COOKIE_DOMAIN / config.json apex resolves to) and
# None in dev so localhost cookies work.
def cookie_domain_for(request: Request) -> Optional[str]:
    """Public wrapper around ``_cookie_domain_for`` for cross-module reuse."""
    return _cookie_domain_for(request)


# TODO(cookie-domain-migration): the following routes still set cookies
# without an explicit Domain attribute and need to be migrated to call
# ``cookie_domain_for(request)`` (post-2026-05-15 CSP/cookie audit):
#
#   - gateway/routes_sharing.py        — 3 cookie sets:
#       narve_share_attribution, narve_shared_view, (one more share cookie)
#   - gateway/saved_views_routes.py    — saved-view scoping cookie
#   - gateway/affiliate_routes.py      — affiliate_code cookie
#
# Each migration is owned by a separate agent / follow-up task; do NOT
# inline-migrate them from this file. The helper is exported here so
# those modules can ``from auth.cookies import cookie_domain_for``.

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
from typing import Optional

from fastapi import Request, Response


SESSION_COOKIE = "narve_session"  # NB: new cookie, NOT pm_gateway_session

# Session cookie lifetime. Override with SESSION_COOKIE_TTL_DAYS env var.
SESSION_COOKIE_TTL = int(os.environ.get("SESSION_COOKIE_TTL_DAYS", "7")) * 24 * 60 * 60


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

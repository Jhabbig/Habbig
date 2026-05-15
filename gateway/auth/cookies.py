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

"""Cookie helpers for the token-first auth flow.

Two cookies coexist:

  - pending_token : short-lived (30 min), NOT httpOnly, SameSite=Strict.
                    Holds the validated invite token while the user walks
                    the /token → /register-or-/login → submit path. Signed
                    with GATEWAY_COOKIE_SECRET so a client cannot forge it
                    even though it's readable by JS.

  - session       : long-lived (7 days), HttpOnly, Secure, SameSite=Strict.
                    Holds the raw hardened session token; server-side it
                    hashes and looks up in `user_sessions`.

Both cookies are domain-scoped to the apex in production so they cover
every subdomain (crypto.narve.ai, etc). In dev the Domain attribute is
omitted so localhost works.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Optional

from fastapi import Request, Response


PENDING_TOKEN_COOKIE = "pending_token"
SESSION_COOKIE = "narve_session"  # NB: new cookie, NOT pm_gateway_session

PENDING_TOKEN_TTL = 1800  # 30 minutes
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


def _secret() -> bytes:
    val = os.environ.get("GATEWAY_COOKIE_SECRET", "")
    if not val:
        if _is_production():
            # Startup guard in server.py should have prevented this; fail loudly
            # if something slips the gate so we never sign with a known constant.
            raise RuntimeError("GATEWAY_COOKIE_SECRET must be set in production")
        val = "narve-pending-token-dev"
    return val.encode()


def sign_pending_token(raw_token: str) -> str:
    """HMAC-sign the raw invite token. Output is `token.sig`."""
    sig = hmac.new(_secret(), raw_token.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw_token}.{sig}"


def verify_pending_token(cookie_value: str) -> Optional[str]:
    """Validate the signed cookie value. Returns the raw token or None."""
    if not cookie_value or "." not in cookie_value:
        return None
    raw, _, sig = cookie_value.rpartition(".")
    if not raw or not sig:
        return None
    expected = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, sig):
        return None
    return raw


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

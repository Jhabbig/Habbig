"""FastAPI request guards for the token-first flow.

These are called from route handlers (not middleware) so we can return
plain RedirectResponse objects and avoid the trap where raising 302
inside middleware clashes with FastAPI's exception machinery.

  - attach_session_to_request : reads the hardened session cookie and
    sets request.state.hardened_user (a dict) if valid.
  - read_hardened_session     : one-shot lookup without mutating state.
  - require_pending_token     : used by /register + /login — redirects
    to /token if the short-lived invite cookie is missing.
  - require_hardened_session  : used by protected HTML routes — redirects
    to /token if the user is not authenticated.
  - require_hardened_admin    : same, plus admin check.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Request
from fastapi.responses import RedirectResponse

import db

from auth.cookies import (
    SESSION_COOKIE,
    read_pending_token,
)


def read_hardened_session(request: Request) -> Optional[dict]:
    """Look up the current user from the hardened session cookie.

    Returns a plain dict the rest of the codebase can `.get()`, or None.
    """
    raw = request.cookies.get(SESSION_COOKIE, "")
    if not raw:
        return None
    row = db.validate_user_session(raw)
    if not row:
        return None
    admin_level = row["is_admin"] or 0
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "email": row["email"],
        "is_admin": bool(admin_level),
        "is_super_admin": admin_level >= 2,
        "admin_level": admin_level,
        "session_id": row["id"],
        "session_token_hash": row["token_hash"],
    }


def attach_session_to_request(request: Request) -> None:
    """Middleware-friendly helper: set request.state.hardened_user."""
    try:
        request.state.hardened_user = read_hardened_session(request)
    except Exception:
        request.state.hardened_user = None


def require_pending_token(request: Request) -> Optional[RedirectResponse]:
    """Return a redirect to /token if the pending_token cookie is missing.

    Also validates the invite token still exists and is not revoked. If
    the user cleared cookies or the token was revoked since the gate,
    bounce back to /token so they can re-enter.
    """
    raw = read_pending_token(request)
    if not raw:
        return RedirectResponse("/token", status_code=302)
    invite = db.get_invite_token(raw)
    if not invite or invite["status"] == "revoked":
        return RedirectResponse("/token", status_code=302)
    return None


def require_hardened_session(request: Request) -> Optional[RedirectResponse]:
    """Return a redirect to /token if the user is not authenticated.

    Prefers the hardened session cookie. Falls back to the legacy
    `current_user` helper so existing routes protected only by the old
    cookie still work during the rollout.
    """
    user = read_hardened_session(request)
    if user:
        return None
    # Legacy fallback
    try:
        import server
        legacy = server.current_user(request)
        if legacy:
            return None
    except Exception:
        pass
    return RedirectResponse("/token", status_code=302)


def require_hardened_admin(request: Request) -> Optional[RedirectResponse]:
    user = read_hardened_session(request)
    if user and user["is_admin"]:
        return None
    # Legacy fallback for admins using the old cookie.
    try:
        import server
        legacy = server.current_user(request)
        if legacy and legacy.get("is_admin"):
            return None
    except Exception:
        pass
    return RedirectResponse("/token", status_code=302)


# ── Spec-exact aliases ──────────────────────────────────────────────────
#
# The spec under STEP 5 names its dependency functions `require_auth` and
# `require_admin`. Export those names pointing at the hardened
# implementations so route code can import either naming convention.

require_auth = require_hardened_session
require_admin = require_hardened_admin

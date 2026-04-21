"""User impersonation — admin "view as" support tool.

Design notes
------------
Impersonation is a **separate cookie** (narve_impersonation) layered on top
of the admin's normal session cookie. The admin's real session is never
modified. When both cookies are present and the impersonation cookie is
valid:

  - current_user() returns the TARGET user (so pages render as that user)
  - request.state.impersonation holds {session_id, admin_user_id, admin_email}
  - middleware blocks any state-changing request whose path matches
    BLOCKED_PATH_PATTERNS
  - every request is recorded in impersonation_actions for audit

Ending the session clears the cookie and marks the DB row ended_at — the
admin's own session cookie is untouched, so they're immediately back as
themselves without a re-login.
"""

from __future__ import annotations

import logging
import re
from typing import Optional


log = logging.getLogger("impersonation")


IMPERSONATION_COOKIE = "narve_impersonation"
IMPERSONATION_COOKIE_TTL = 4 * 60 * 60  # 4 hours — longer than a typical support ticket,
                                        # short enough that forgotten sessions expire


# ── Blocked action patterns ───────────────────────────────────────────────
#
# Destructive or irreversible actions the admin must NOT be able to trigger
# while viewing as another user. Matched against request.url.path with
# fullmatch — use (?P<…>) groups only for clarity, not for routing. Ordered
# roughly by expected frequency so the common case is fast.

_BLOCKED_PATTERNS = [
    # Account-level
    r"/account/delete.*",
    r"/auth/logout.*",            # Must use /admin/impersonations/end instead
    r"/account/password.*",
    r"/account/change-email.*",
    r"/account/email.*",
    r"/settings/password.*",
    r"/settings/email.*",
    r"/settings/2fa.*",           # Even though 2FA was removed, defend if re-added
    r"/api/v\d+/account/delete.*",
    r"/api/account/delete.*",
    r"/api/v\d+/account/password.*",
    r"/api/account/password.*",

    # Billing / subscriptions
    r"/billing/cancel.*",
    r"/billing/checkout.*",
    r"/subscribe.*",              # Prevent starting a real Stripe checkout
    r"/checkout.*",
    r"/api/billing/cancel.*",
    r"/api/billing/checkout.*",
    r"/api/v\d+/billing/.*",      # Entire billing API off-limits

    # Content the impersonated user "owns"
    r"/predictions/.+/delete.*",
    r"/api/predictions/.+/delete.*",
    r"/api/v\d+/predictions/.+/delete.*",
    r"/widgets/.*",               # Embed widgets
    r"/api/widgets/.*",
    r"/api/v\d+/widgets/.*",

    # AI / Intelligence (would burn user's token quota)
    r"/intelligence/.*",
    r"/api/intelligence/.*",
    r"/api/v\d+/intelligence/.*",
    r"/api/ai/.*",
    r"/api/v\d+/ai/.*",
]

_BLOCKED_RE = [re.compile(p) for p in _BLOCKED_PATTERNS]


# Methods considered state-changing. GET/HEAD/OPTIONS pass through untouched
# so the admin can still *view* the account.
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Paths that MUST remain reachable during impersonation even though they
# look state-changing — chiefly the "end impersonation" endpoint itself.
_ALWAYS_ALLOWED = frozenset({"/admin/impersonations/end"})


def is_action_blocked(method: str, path: str) -> bool:
    """Return True if this request should be blocked due to impersonation.

    Only state-changing requests are evaluated — GETs always pass through so
    the admin can see the user's account state.
    """
    if method.upper() not in _STATE_CHANGING_METHODS:
        return False
    if path in _ALWAYS_ALLOWED:
        return False
    for pattern in _BLOCKED_RE:
        if pattern.fullmatch(path):
            return True
    return False


# ── Banner HTML ───────────────────────────────────────────────────────────


def banner_html(
    *,
    target_display: str,
    admin_email: str,
    started_at: int,
    csrf_field: str = "",
) -> str:
    """The always-visible impersonation banner injected into every rendered
    page. Kept inline (no external CSS) so it works on dashboards we can't
    easily inject stylesheets into.
    """
    import html as _html
    import time as _time

    elapsed_s = max(0, int(_time.time()) - int(started_at or 0))
    if elapsed_s < 60:
        elapsed = f"{elapsed_s}s"
    elif elapsed_s < 3600:
        elapsed = f"{elapsed_s // 60}m"
    else:
        h = elapsed_s // 3600
        m = (elapsed_s % 3600) // 60
        elapsed = f"{h}h {m}m"

    return (
        '<div id="narve-impersonation-banner" role="alert" '
        'style="position:fixed;top:0;left:0;right:0;z-index:99999;'
        'background:#7c2d12;color:#fff;padding:10px 16px;'
        'font-size:13px;font-weight:600;'
        'display:flex;align-items:center;justify-content:center;gap:16px;'
        'box-shadow:0 2px 8px rgba(0,0,0,0.3);'
        'font-family:-apple-system,BlinkMacSystemFont,\'Inter\',sans-serif">'
        f'<span>⚠ Impersonating <u>{_html.escape(target_display)}</u> '
        f'as {_html.escape(admin_email)} · started {_html.escape(elapsed)} ago</span>'
        '<form method="post" action="/admin/impersonations/end" style="margin:0">'
        f'{csrf_field}'
        '<button type="submit" style="background:#fff;color:#7c2d12;'
        'border:0;border-radius:6px;padding:6px 14px;font-weight:700;'
        'cursor:pointer;font-size:12px">End session</button>'
        '</form>'
        '</div>'
        # Push body content down so the banner doesn't overlay the nav.
        '<style>body{padding-top:44px !important}</style>'
    )


def blocked_response_html(method: str, path: str) -> str:
    """HTML shown when a blocked action is attempted. Short, clear, actionable."""
    import html as _html
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<title>Action blocked — impersonation</title>'
        '<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;'
        'background:#0d0d0d;color:#fff;padding:64px 24px;text-align:center}'
        'h1{font-size:22px;margin-bottom:12px}p{color:#aaa;line-height:1.6}'
        'a{color:#7dd3fc}</style></head><body>'
        '<h1>Action blocked</h1>'
        f'<p>You are impersonating another user and cannot perform '
        f'<code>{_html.escape(method)} {_html.escape(path)}</code>.<br>'
        'Destructive actions (account deletion, billing, AI usage, etc.) '
        'are disabled during impersonation.</p>'
        '<p><a href="/admin/impersonations">Back to admin</a></p>'
        '</body></html>'
    )


def display_name_for(user_row) -> str:
    """Best-effort human-readable name for the target user in banners."""
    if user_row is None:
        return "(unknown user)"
    try:
        if user_row["username"]:
            return f"{user_row['username']} ({user_row['email']})"
    except (KeyError, IndexError):
        pass
    try:
        return user_row["email"] or "(no email)"
    except (KeyError, IndexError):
        return "(unknown user)"

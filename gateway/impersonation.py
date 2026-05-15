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
# re.search (NOT fullmatch) — so a prefix like `/account/password` catches
# `/account/password-reset`, `/account/password/change`, and any query-less
# variant without having to enumerate suffixes. Ordered roughly by expected
# frequency so the common case is fast.

_BLOCKED_PATTERNS = [
    # Account-level — deliberately bare prefixes so any sub-path matches.
    r"/account/password",         # catches /account/password, /account/password-reset, /account/password/change
    r"/account/email",            # change-email, email/verify, etc.
    r"/account/delete",
    r"/account/2fa",              # 2FA setup/disable — blocked even on GET
    r"/account/api-keys",         # API key create/rotate/revoke
    r"/account/payment",          # saved payment methods
    r"/auth/logout",              # Must use /admin/impersonations/end instead
    r"/profile/password",         # CRIT — admin->user password change via /profile/password
    r"/settings/password",
    r"/settings/email",
    r"/settings/2fa",             # Even though 2FA was removed, defend if re-added
    r"/settings/disconnect/",     # HIGH — deletes positions + market credentials

    # Billing / subscriptions — entire surface is off-limits.
    r"/billing",                  # /billing/cancel, /billing/checkout, /billing/portal, etc.
    r"/subscribe",                # Prevent starting a real Stripe checkout
    r"/checkout",
    r"/api/billing",
    r"/api/v\d+/billing",

    # Subproduct signup — starts signup flow under user identity.
    r"/subproduct-signup",        # HIGH — start a signup under the user identity

    # Admin — impersonated sessions should never hit admin routes at all.
    # (The /admin/impersonations/end endpoint is whitelisted below.)
    r"/admin",

    # Content the impersonated user "owns"
    r"/predictions/.+/delete",
    r"/api/predictions/.+/delete",
    r"/api/v\d+/predictions/.+/delete",
    r"/widgets",                  # Embed widgets
    r"/api/widgets",
    r"/api/v\d+/widgets",
    r"/api/embeds",               # HIGH — create/delete/rotate-token widget endpoints

    # Trading addon — toggles user trading integration.
    r"/api/trading-addon/config",

    # Sharing / saved / follow / preferences — write surfaces under user identity.
    r"/api/share/",
    r"/api/saved/",
    r"/api/sources/.+/follow",
    r"/api/notifications/email-preferences",
    r"/api/feedback",             # submit/vote/comment under user identity

    # AI / Intelligence (would burn user's token quota)
    r"/intelligence",
    r"/api/intelligence",
    r"/api/v\d+/intelligence",
    r"/api/ai",
    r"/api/v\d+/ai",
]

_BLOCKED_RE = [re.compile(p) for p in _BLOCKED_PATTERNS]

# Paths whose mere existence leaks sensitive UX (e.g. "delete my account"
# confirmation pages, 2FA QR codes). These are blocked on ALL methods
# including GET/HEAD, so the admin can't screenshot a 2FA secret or
# stumble onto a destructive-looking confirmation page as someone else.
_READ_ALSO_BLOCKED_PATTERNS = [
    r"/account/delete",
    r"/account/2fa",
    r"/account/api-keys",
    r"/account/payment",
    r"/admin",
    # GET /api/embeds returns widget tokens (include_token=True) — a read
    # alone is a credential leak, so block read methods too.
    r"/api/embeds",
]
_READ_ALSO_BLOCKED_RE = [re.compile(p) for p in _READ_ALSO_BLOCKED_PATTERNS]


# Methods considered state-changing. GET/HEAD/OPTIONS normally pass through
# untouched so the admin can still *view* the account — but a few GET paths
# leak info (see _READ_ALSO_BLOCKED_RE above) and are blocked separately.
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Paths that MUST remain reachable during impersonation even though they
# look state-changing — chiefly the "end impersonation" endpoint itself.
_ALWAYS_ALLOWED = frozenset({"/admin/impersonations/end"})


def is_action_blocked(method: str, path: str) -> bool:
    """Return True if this request should be blocked due to impersonation.

    State-changing methods (POST/PUT/PATCH/DELETE) are checked against the
    full blocklist. A smaller set of paths is also blocked on GET/HEAD so
    the admin can't view e.g. a 2FA setup QR code or an account-delete
    confirmation page as the impersonated user.
    """
    if path in _ALWAYS_ALLOWED:
        return False

    method_upper = method.upper()
    # Always-on block: these paths are sensitive even for read methods.
    for pattern in _READ_ALSO_BLOCKED_RE:
        if pattern.search(path):
            return True

    if method_upper not in _STATE_CHANGING_METHODS:
        return False

    for pattern in _BLOCKED_RE:
        if pattern.search(path):
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

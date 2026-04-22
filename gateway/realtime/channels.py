"""Channel names, shapes, and per-channel auth rules.

A channel string is ``namespace:subject``. The namespace decides the
auth rule; the subject is free-form (market slug, user id, subproduct
slug, etc.) with a light syntactic check to keep hot-wired clients from
squatting on arbitrary names.

Shape:
    market:{slug}      — any authenticated user
    user:{user_id}     — only user with id == user_id (no impersonator leak)
    feed:global        — any authenticated user
    admin:security     — admin_level >= 1
    subproduct:{slug}  — has_subproduct_access(user, slug)

All other names are refused. Keep the allowlist tight — unknown names
silently passing would give a compromised client a lateral-move vector
(subscribing to someone else's private channel by guessing its pattern).
"""

from __future__ import annotations

import re


# Connection + subscription caps. Enforced at the route layer because
# the hub is channel-agnostic.
MAX_CONNECTIONS_PER_USER = 3
MAX_CHANNELS_PER_CONN = 50
MAX_MESSAGES_PER_SEC = 30


# Public channel patterns — what the docs + admin panel render.
CHANNEL_PATTERNS = {
    "market": "market:{slug}",
    "user": "user:{user_id}",
    "feed": "feed:global",
    "admin": "admin:security",
    "subproduct": "subproduct:{slug}",
}


# Subject validation — keep channel names ASCII-safe so sending them back
# in JSON doesn't require escape-handling and so log grep stays sane.
_SLUG_RE = re.compile(r"^[A-Za-z0-9_\-:]{1,120}$")
_UID_RE = re.compile(r"^[0-9]{1,10}$")


def _has_subproduct_access(user: dict, slug: str) -> bool:
    """Check subproduct access via the same helper that gates dashboard routes."""
    try:
        from subproduct_access import has_subproduct_access as _check
        return bool(_check(user.get("user_id"), slug))
    except Exception:
        # If the module is unavailable or raises, fail closed.
        return False


def is_channel_allowed(user: dict | None, channel: str) -> bool:
    """Return True if ``user`` may subscribe to ``channel``.

    ``user`` may be None, but every channel requires authentication, so
    None always returns False. The caller should close the socket with
    4401 before we ever reach this function — this is belt-and-braces.
    """
    if not user or "user_id" not in user:
        return False
    if not channel or not _SLUG_RE.match(channel):
        return False

    ns, _, subject = channel.partition(":")
    if not ns or not subject:
        # Accept the fixed-name channels (feed:global, admin:security) — they
        # have a non-empty subject. Reject anything shaped like "foo:".
        return False

    if ns == "market":
        # Any authenticated user can subscribe to any market channel.
        # The market id space is validated loosely (alphanum / _ / - / :);
        # unknown-but-well-formed names are allowed so the channel can be
        # lazy-created — this matches the spec's "subscribe to market:unknown
        # → accepted" test.
        return bool(_SLUG_RE.match(subject))

    if ns == "user":
        # Only the user themselves. Impersonators are logged in as the
        # ADMIN user (current_user returns the admin row when impersonating
        # via the narve_impersonation cookie), so this check correctly
        # prevents an impersonator from listening on the target user's
        # private channel.
        if not _UID_RE.match(subject):
            return False
        return int(subject) == int(user["user_id"])

    if ns == "feed":
        return subject == "global"

    if ns == "admin":
        if subject != "security":
            return False
        return int(user.get("is_admin") or 0) >= 1

    if ns == "subproduct":
        return _has_subproduct_access(user, subject)

    # Any other namespace is rejected.
    return False

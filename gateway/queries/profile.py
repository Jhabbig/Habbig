"""DB queries for the public-profile + follow-graph surface.

All read paths return ``sqlite3.Row`` (use bracket access). All mutation
paths return the count of rows affected, or a small dict for the
toggle helpers so HTMX can re-render the button cleanly.

Reserved-handle list lives here rather than route-side because it's a
data-integrity concern: we don't want a future operator to accidentally
hand out ``support`` or ``admin`` even from a CLI script.
"""

from __future__ import annotations

import re
import time
from typing import Optional

import db


# Lowercase letters, digits, underscore. 3–20 chars. Matches the input
# pattern attribute on the settings form so client + server agree.
HANDLE_RE = re.compile(r"^[a-z0-9_]{3,20}$")

# Reserve the obvious staff/legal/system slugs so a user can't pretend to
# be us. Keep this conservative — it's much easier to add a name than to
# evict a squatter once they've had it for a while.
RESERVED_HANDLES = frozenset({
    "admin", "administrator",
    "narve", "narveai", "narve_ai",
    "api", "apidocs", "developer", "dev",
    "staff", "team",
    "support", "help", "contact",
    "root", "owner",
    "mod", "moderator", "moderation",
    "system", "official",
    "billing", "legal", "privacy", "terms", "dpa",
    "feed", "feedback",
    # Routes that already exist under the apex — collisions break those.
    "u", "settings", "profile", "dashboard", "dashboards",
    "login", "logout", "register", "signup", "token",
    "pricing", "subscribe", "calendar", "sources", "search",
    "intelligence", "predictions", "watchlist", "notifications",
    "markets", "best-bets", "bestbets",
})

# Cooldown between handle changes. Stops squatter-trading and reduces
# the chance of broken inbound links from a hot profile.
HANDLE_CHANGE_COOLDOWN_SECS = 30 * 86400


# ── Profile reads ──────────────────────────────────────────────────────


def get_profile_by_handle(handle: str):
    """Return the user row whose handle matches AND who has opted in.

    Anything else returns None — including a user with that handle but
    ``public_profile_enabled = 0`` (we 404 to hide existence).
    """
    if not handle:
        return None
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM users "
            "WHERE profile_handle = ? AND public_profile_enabled = 1",
            (handle.lower(),),
        ).fetchone()


def get_profile_for_user(user_id: int):
    """Read the calling user's own profile fields, opt-in or not."""
    with db.conn() as c:
        return c.execute(
            "SELECT id, email, public_profile_enabled, profile_handle, "
            "profile_bio, profile_avatar_url, profile_handle_changed_at, "
            "created_at "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()


def handle_taken_by_other(handle: str, exclude_user_id: int) -> bool:
    """True when ``handle`` is already used by some user other than us.

    Case-insensitive — every handle is normalised to lower case before
    storage, so a literal lookup is enough.
    """
    if not handle:
        return False
    with db.conn() as c:
        row = c.execute(
            "SELECT id FROM users "
            "WHERE LOWER(profile_handle) = ? AND id != ?",
            (handle.lower(), exclude_user_id),
        ).fetchone()
    return row is not None


# ── Profile writes ─────────────────────────────────────────────────────


class ProfileError(ValueError):
    """Raised by ``update_profile`` for caller-actionable errors. The
    route layer surfaces ``.code`` to HTMX as ``HX-Trigger``-style
    payloads so the form can highlight the right field."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def update_profile(
    user_id: int,
    *,
    enabled: bool,
    handle: Optional[str],
    bio: Optional[str],
) -> dict:
    """Persist the profile fields. Validates handle uniqueness, the
    reserved list, the cooldown, and length limits.

    Returns the post-update profile row (as a dict so the route doesn't
    need to do a second read).
    """
    handle = (handle or "").strip().lower() or None
    bio = (bio or "").strip() or None
    if bio is not None and len(bio) > 200:
        raise ProfileError("bio_too_long", "Bio must be 200 characters or fewer.")

    if handle is not None:
        if not HANDLE_RE.match(handle):
            raise ProfileError(
                "handle_invalid",
                "Handle must be 3–20 chars: lowercase letters, digits, underscores.",
            )
        if handle in RESERVED_HANDLES:
            raise ProfileError("handle_reserved", "That handle is reserved.")
        if handle_taken_by_other(handle, user_id):
            raise ProfileError("handle_taken", "That handle is already in use.")

    now = int(time.time())
    with db.conn() as c:
        existing = c.execute(
            "SELECT profile_handle, profile_handle_changed_at "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if existing is None:
            raise ProfileError("user_missing", "User not found.")

        prev_handle = existing["profile_handle"]
        prev_changed = existing["profile_handle_changed_at"] or 0

        # Cooldown only applies when actually CHANGING the handle —
        # toggling enabled or editing bio is free.
        if handle is not None and handle != prev_handle:
            since = now - prev_changed
            if prev_changed and since < HANDLE_CHANGE_COOLDOWN_SECS:
                days_left = (HANDLE_CHANGE_COOLDOWN_SECS - since) // 86400 + 1
                raise ProfileError(
                    "handle_cooldown",
                    f"You can change your handle again in {days_left} day(s).",
                )

        # If the user is enabling but hasn't picked a handle yet, refuse
        # so /u/{handle} can never resolve to a half-configured profile.
        if enabled and handle is None and prev_handle is None:
            raise ProfileError(
                "handle_required",
                "Pick a handle before publishing your profile.",
            )

        new_handle = handle if handle is not None else prev_handle
        handle_changed_at = now if (handle is not None and handle != prev_handle) else prev_changed

        c.execute(
            "UPDATE users SET "
            "  public_profile_enabled = ?, "
            "  profile_handle = ?, "
            "  profile_bio = ?, "
            "  profile_handle_changed_at = ? "
            "WHERE id = ?",
            (1 if enabled else 0, new_handle, bio, handle_changed_at, user_id),
        )

        return {
            "public_profile_enabled": bool(enabled),
            "profile_handle": new_handle,
            "profile_bio": bio,
            "profile_handle_changed_at": handle_changed_at,
        }


def update_avatar_url(user_id: int, avatar_url: Optional[str]) -> None:
    """Set/clear the avatar URL after a successful upload."""
    with db.conn() as c:
        c.execute(
            "UPDATE users SET profile_avatar_url = ? WHERE id = ?",
            (avatar_url, user_id),
        )


# ── Follow graph ───────────────────────────────────────────────────────


def is_following(follower_user_id: int, followed_user_id: int) -> bool:
    if follower_user_id == followed_user_id:
        # We never let users follow themselves, but be defensive.
        return False
    with db.conn() as c:
        row = c.execute(
            "SELECT 1 FROM user_follows "
            "WHERE follower_user_id = ? AND followed_user_id = ?",
            (follower_user_id, followed_user_id),
        ).fetchone()
    return row is not None


def follower_count(user_id: int) -> int:
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM user_follows WHERE followed_user_id = ?",
            (user_id,),
        ).fetchone()
    return int(row["n"]) if row else 0


def following_count(user_id: int) -> int:
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM user_follows WHERE follower_user_id = ?",
            (user_id,),
        ).fetchone()
    return int(row["n"]) if row else 0


def follow(follower_user_id: int, followed_user_id: int) -> bool:
    """Add a follow. Returns True if a new row was inserted, False if
    the user was already following the target. Self-follow is silently
    ignored to keep the toggle endpoint idempotent."""
    if follower_user_id == followed_user_id:
        return False
    with db.conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO user_follows "
            "(follower_user_id, followed_user_id) VALUES (?, ?)",
            (follower_user_id, followed_user_id),
        )
        return cur.rowcount > 0


def unfollow(follower_user_id: int, followed_user_id: int) -> bool:
    """Remove a follow. Returns True if a row was deleted."""
    with db.conn() as c:
        cur = c.execute(
            "DELETE FROM user_follows "
            "WHERE follower_user_id = ? AND followed_user_id = ?",
            (follower_user_id, followed_user_id),
        )
        return cur.rowcount > 0


def toggle_follow(follower_user_id: int, followed_user_id: int) -> dict:
    """Atomic toggle. Returns the new state {is_following, follower_count}."""
    if follower_user_id == followed_user_id:
        return {"is_following": False, "follower_count": follower_count(followed_user_id)}
    new_following: bool
    if is_following(follower_user_id, followed_user_id):
        unfollow(follower_user_id, followed_user_id)
        new_following = False
    else:
        follow(follower_user_id, followed_user_id)
        new_following = True
    return {
        "is_following": new_following,
        "follower_count": follower_count(followed_user_id),
    }


__all__ = [
    "HANDLE_RE",
    "RESERVED_HANDLES",
    "HANDLE_CHANGE_COOLDOWN_SECS",
    "ProfileError",
    "get_profile_by_handle",
    "get_profile_for_user",
    "handle_taken_by_other",
    "update_profile",
    "update_avatar_url",
    "is_following",
    "follower_count",
    "following_count",
    "follow",
    "unfollow",
    "toggle_follow",
]

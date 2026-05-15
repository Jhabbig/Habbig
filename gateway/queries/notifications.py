"""Queries extracted from gateway/db.py — notification bell.

These are the DB helpers behind ``notification_routes.py``. They were
referenced as ``db.create_notification`` / ``db.get_notifications`` etc.
across the codebase but had no actual implementation, leaving every
notification endpoint broken at runtime.

Schema lives in migration 026 (``notifications`` + ``notification_preferences``
tables). Re-exported onto ``db`` at import time so the call sites
(``notification_routes.py``, ``notifications.py``, the job-trigger
callers in ``jobs/*``) continue to work unchanged.

Functions provided:
  * NOTIFICATION_TYPES         — canonical tuple of valid type strings.
  * create_notification        — INSERT a row, return new id.
  * get_notifications          — paginated keyset list, newest-first.
  * get_unread_count           — fast count via partial index from m026.
  * mark_notification_read     — flip read_at, scoped by user_id.
  * mark_all_notifications_read — bulk read, returns rows touched.
  * archive_notification       — flip archived=1, scoped by user_id.
  * delete_notification        — hard DELETE, scoped by user_id.
  * get_notification_preferences — return prefs dict (defaults=all-on).
  * set_notification_preferences — partial-update prefs row.
  * notification_type_enabled  — per-type gate, defaults True.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import db


log = logging.getLogger("queries.notifications")


# Canonical type vocabulary. Adding a new type? Append it here and any
# preference UI keying on it will pick it up automatically. ``system`` is
# the coerce-target for unknown values — see notifications.create_notification.
NOTIFICATION_TYPES: tuple[str, ...] = (
    "system",                 # platform notices, billing, account events
    "prediction_resolved",    # a saved market just resolved
    "market_resolved",        # alias for prediction_resolved used by older callers
    "market_mover",           # large price movement on a tracked market
    "source_followed_post",   # a followed source published
    "ev_threshold",           # an EV signal crossed the user's bar
    "credibility_change",     # tracked source credibility moved
    "report_ready",           # weekly/morning report is generated
    "share_received",         # someone shared a market/take with you
    "comment_reply",          # reply to one of your takes
    "subscription_event",     # renewal / failed payment / cancellation
    "newsletter_confirmed",   # double-opt-in confirmation received
    "onboarding",             # tour reminders, first-week-goal nudges
    "admin",                  # admin-level pings (audit alerts, etc.)
)


# ── Create ─────────────────────────────────────────────────────────────────


def create_notification(
    user_id: int,
    type: str,
    title: str,
    body: str = "",
    *,
    link_url: Optional[str] = None,
    icon: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> int:
    """Insert a notification row and return its id.

    Accepts the four leading args positionally to match the call shape
    already used elsewhere in the codebase (notifications.py wrapper,
    pre-existing tests). The auxiliary fields are keyword-only so we
    can extend the row schema without breaking the call sites.

    Raises sqlite3 errors on bad input — callers in ``notifications.py``
    wrap this in try/except so a broken row never blocks the originating
    job. ``metadata`` is JSON-encoded; pass a dict or None.
    """
    if type not in NOTIFICATION_TYPES:
        # Defensive coerce — keeps the schema consistent even if a caller
        # passes a typo'd type. Matches the same posture in
        # notifications.create_notification.
        log.warning("create_notification: unknown type %r, coercing to 'system'", type)
        type = "system"
    now = int(time.time())
    metadata_json = json.dumps(metadata) if metadata else None
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO notifications "
            "(user_id, type, title, body, link_url, icon, metadata, created_at, archived) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                int(user_id),
                type,
                title or "",
                body or "",
                link_url,
                icon,
                metadata_json,
                now,
            ),
        )
        return int(cur.lastrowid)


# ── Read ───────────────────────────────────────────────────────────────────


def _row_to_dict(row) -> dict:
    """Materialise an sqlite3.Row into a plain dict + decode metadata JSON."""
    data = dict(row)
    raw_meta = data.get("metadata")
    if raw_meta:
        try:
            data["metadata"] = json.loads(raw_meta)
        except (TypeError, ValueError):
            data["metadata"] = {}
    else:
        data["metadata"] = {}
    data["archived"] = bool(data.get("archived"))
    return data


def get_notifications(
    user_id: int,
    *,
    unread_only: bool = False,
    type: Optional[str] = None,
    limit: int = 20,
    before_id: Optional[int] = None,
    include_archived: bool = False,
) -> list[dict]:
    """Newest-first list. Keyset pagination via ``before_id`` (uses created_at
    + id tiebreaker so two rows in the same second don't collide).

    Caller is responsible for verifying that ``before_id`` belongs to the
    same user — see notification_routes.api_notifications_list for the
    cross-user cursor leak guard.
    """
    limit = max(1, min(int(limit or 20), 100))  # absolute cap

    where = ["user_id = ?"]
    params: list[Any] = [int(user_id)]
    if unread_only:
        where.append("read_at IS NULL")
    if not include_archived:
        where.append("archived = 0")
    if type:
        where.append("type = ?")
        params.append(str(type))
    if before_id is not None:
        # Resolve the cursor row's created_at so we can paginate by the
        # (created_at, id) tuple. The route-level ownership check has
        # already verified before_id belongs to this user.
        with db.conn() as c:
            cursor_row = c.execute(
                "SELECT created_at, id FROM notifications WHERE id = ?",
                (int(before_id),),
            ).fetchone()
        if cursor_row:
            where.append("(created_at, id) < (?, ?)")
            params.extend([int(cursor_row["created_at"]), int(cursor_row["id"])])

    sql = (
        "SELECT id, user_id, type, title, body, link_url, icon, metadata, "
        "       created_at, read_at, archived "
        "FROM notifications WHERE " + " AND ".join(where) + " "
        "ORDER BY created_at DESC, id DESC LIMIT ?"
    )
    params.append(limit)
    with db.conn() as c:
        rows = c.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_unread_count(user_id: int) -> int:
    """Cheap badge count. Hits the partial index from migration 026."""
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM notifications "
            "WHERE user_id = ? AND read_at IS NULL AND archived = 0",
            (int(user_id),),
        ).fetchone()
    return int(row["n"]) if row else 0


# ── Read / archive / delete (mutating, all user-scoped) ────────────────────


def mark_notification_read(notif_id: int, user_id: int) -> bool:
    """Flip read_at on a single row. Returns True if a row was changed.

    The ``user_id`` clause is the only ACL — even though the route
    additionally checks ownership, defence-in-depth here means a future
    handler that forgets the check can't escalate.
    """
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "UPDATE notifications SET read_at = ? "
            "WHERE id = ? AND user_id = ? AND read_at IS NULL",
            (now, int(notif_id), int(user_id)),
        )
    return cur.rowcount > 0


def mark_all_notifications_read(user_id: int) -> int:
    """Bulk-read all unread+unarchived rows for this user. Returns row count."""
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "UPDATE notifications SET read_at = ? "
            "WHERE user_id = ? AND read_at IS NULL AND archived = 0",
            (now, int(user_id)),
        )
    return cur.rowcount


def archive_notification(notif_id: int, user_id: int) -> bool:
    """Flip archived=1. Returns True if a row was changed."""
    with db.conn() as c:
        cur = c.execute(
            "UPDATE notifications SET archived = 1 "
            "WHERE id = ? AND user_id = ? AND archived = 0",
            (int(notif_id), int(user_id)),
        )
    return cur.rowcount > 0


def delete_notification(notif_id: int, user_id: int) -> bool:
    """Hard DELETE one row. Returns True if a row was deleted."""
    with db.conn() as c:
        cur = c.execute(
            "DELETE FROM notifications WHERE id = ? AND user_id = ?",
            (int(notif_id), int(user_id)),
        )
    return cur.rowcount > 0


# ── Preferences ────────────────────────────────────────────────────────────


def _default_prefs_dict() -> dict:
    """All-on default preference dict.

    Missing notification_preferences rows are treated as defaults so we
    never need a backfill migration when a new user is created.
    """
    return {
        "inapp_enabled": True,
        "push_enabled":  False,   # off by default — requires explicit opt-in
        "email_enabled": True,
        "types":         {t: True for t in NOTIFICATION_TYPES},
    }


def get_notification_preferences(user_id: int) -> dict:
    """Return the full prefs dict. Falls back to defaults if no row."""
    with db.conn() as c:
        row = c.execute(
            "SELECT inapp_enabled, push_enabled, email_enabled, types_json "
            "FROM notification_preferences WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
    if not row:
        return _default_prefs_dict()
    try:
        types = json.loads(row["types_json"] or "{}")
    except (TypeError, ValueError):
        types = {}
    # Merge stored types over defaults so a new NOTIFICATION_TYPES entry
    # immediately defaults to True for users with a stale row.
    merged_types = {t: True for t in NOTIFICATION_TYPES}
    for k, v in types.items():
        if k in merged_types:
            merged_types[k] = bool(v)
    return {
        "inapp_enabled": bool(row["inapp_enabled"]),
        "push_enabled":  bool(row["push_enabled"]),
        "email_enabled": bool(row["email_enabled"]),
        "types":         merged_types,
    }


def set_notification_preferences(
    user_id: int,
    *,
    inapp_enabled: Optional[bool] = None,
    push_enabled:  Optional[bool] = None,
    email_enabled: Optional[bool] = None,
    types:         Optional[dict] = None,
) -> dict:
    """Partial update — None means "leave as-is". Returns the post-update
    full prefs dict so the caller can ship it back to the UI in one step.
    """
    current = get_notification_preferences(user_id)
    if inapp_enabled is not None:
        current["inapp_enabled"] = bool(inapp_enabled)
    if push_enabled is not None:
        current["push_enabled"] = bool(push_enabled)
    if email_enabled is not None:
        current["email_enabled"] = bool(email_enabled)
    if types is not None and isinstance(types, dict):
        for k, v in types.items():
            if k in current["types"]:
                current["types"][k] = bool(v)

    now = int(time.time())
    types_json = json.dumps(current["types"])
    with db.conn() as c:
        c.execute(
            "INSERT INTO notification_preferences "
            "(user_id, inapp_enabled, push_enabled, email_enabled, types_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "  inapp_enabled = excluded.inapp_enabled, "
            "  push_enabled = excluded.push_enabled, "
            "  email_enabled = excluded.email_enabled, "
            "  types_json = excluded.types_json, "
            "  updated_at = excluded.updated_at",
            (
                int(user_id),
                1 if current["inapp_enabled"] else 0,
                1 if current["push_enabled"]  else 0,
                1 if current["email_enabled"] else 0,
                types_json,
                now,
            ),
        )
    return current


def notification_type_enabled(user_id: int, type: str) -> bool:
    """Cheap gate used from the create-notification hot path. Defaults True."""
    prefs = get_notification_preferences(user_id)
    if not prefs.get("inapp_enabled", True):
        return False
    types = prefs.get("types") or {}
    return bool(types.get(type, True))


__all__ = (
    "NOTIFICATION_TYPES",
    "create_notification",
    "get_notifications",
    "get_unread_count",
    "mark_notification_read",
    "mark_all_notifications_read",
    "archive_notification",
    "delete_notification",
    "get_notification_preferences",
    "set_notification_preferences",
    "notification_type_enabled",
)

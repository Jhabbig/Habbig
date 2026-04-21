"""Queries extracted from gateway/db.py — onboarding domain.

Moved out of db.py to keep the connection-pooling/schema module small.
Re-exported back onto db.py at import time, so every existing
``import db; db.<name>`` call site keeps working unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time
from typing import Optional

import db


def get_onboarding_status(user_id: int) -> dict:
    """Return onboarding state for a user.

    Returns {completed, completed_at, categories, notify_push, notify_email,
             notify_ev_threshold, notify_cred_threshold}.
    """
    import json as _json
    with db.conn() as c:
        row = c.execute(
            "SELECT onboarding_completed, onboarding_completed_at, onboarding_categories, "
            "notify_push, notify_email, notify_ev_threshold, notify_cred_threshold "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"completed": False, "completed_at": None, "categories": []}
    cats = []
    if row["onboarding_categories"]:
        try:
            cats = _json.loads(row["onboarding_categories"])
        except Exception:
            cats = []
    return {
        "completed": bool(row["onboarding_completed"]),
        "completed_at": row["onboarding_completed_at"],
        "categories": cats,
        "notify_push": bool(row["notify_push"]),
        "notify_email": bool(row["notify_email"]),
        "notify_ev_threshold": row["notify_ev_threshold"],
        "notify_cred_threshold": row["notify_cred_threshold"],
    }


def set_onboarding_categories(user_id: int, categories: list[str]) -> None:
    import json as _json
    with db.conn() as c:
        c.execute(
            "UPDATE users SET onboarding_categories = ? WHERE id = ?",
            (_json.dumps(categories), user_id),
        )


def set_onboarding_notifications(
    user_id: int,
    push: bool,
    email: bool,
    ev_threshold: Optional[float] = None,
    cred_threshold: Optional[float] = None,
) -> None:
    with db.conn() as c:
        c.execute(
            "UPDATE users SET notify_push = ?, notify_email = ?, "
            "notify_ev_threshold = ?, notify_cred_threshold = ? WHERE id = ?",
            (1 if push else 0, 1 if email else 0, ev_threshold, cred_threshold, user_id),
        )


def complete_onboarding(user_id: int) -> None:
    with db.conn() as c:
        c.execute(
            "UPDATE users SET onboarding_completed = 1, onboarding_completed_at = ? WHERE id = ?",
            (int(time.time()), user_id),
        )


__all__ = [
    'get_onboarding_status',
    'set_onboarding_categories',
    'set_onboarding_notifications',
    'complete_onboarding',
]

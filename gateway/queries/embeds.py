"""Queries extracted from gateway/db.py — embeds domain.

Moved out of db.py to keep the connection-pooling/schema module small.
Re-exported back onto db.py at import time, so every existing
``import db; db.<name>`` call site keeps working unchanged.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Optional

import db


EMBED_WIDGET_TYPES = frozenset({"source_credibility", "market_probability", "best_bets"})


EMBED_WIDGET_THEMES = frozenset({"light", "dark", "auto"})


MAX_EMBED_WIDGETS_PER_USER = 10


def count_user_active_embed_widgets(user_id: int) -> int:
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM embed_widgets "
            "WHERE user_id = ? AND is_active = 1",
            (user_id,),
        ).fetchone()
    return row["n"] if row else 0


def create_embed_widget(
    user_id: int,
    widget_type: str,
    target: str,
    domain: str,
    theme: str = "auto",
) -> Optional[sqlite3.Row]:
    """Create a widget for ``user_id``. Returns the row or ``None`` if over limit.

    Caller validates ``widget_type``, ``target``, ``domain``, and ``theme``
    before calling. The limit check lives inside the same transaction as
    the insert so two concurrent creates can't both slip past.
    """
    import embed_tokens  # lazy import: avoids a cycle at module load
    widget_id = embed_tokens.new_widget_id()
    token_salt = embed_tokens.new_salt()
    now = int(time.time())
    with db.conn() as c:
        existing = c.execute(
            "SELECT COUNT(*) AS n FROM embed_widgets "
            "WHERE user_id = ? AND is_active = 1",
            (user_id,),
        ).fetchone()
        if existing and existing["n"] >= MAX_EMBED_WIDGETS_PER_USER:
            return None
        c.execute(
            "INSERT INTO embed_widgets "
            "(widget_id, user_id, widget_type, target, domain, token_salt, "
            " theme, created_at, is_active, impressions) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0)",
            (
                widget_id, user_id, widget_type, target, domain.lower(),
                token_salt, theme, now,
            ),
        )
        return c.execute(
            "SELECT * FROM embed_widgets WHERE widget_id = ?", (widget_id,)
        ).fetchone()


def list_user_embed_widgets(user_id: int, include_inactive: bool = True) -> list[sqlite3.Row]:
    """Return all widgets for the user, newest first.

    Deactivated widgets are included by default so the management UI can
    show historical impression counts. Pass ``include_inactive=False`` to
    scope to live widgets only.
    """
    with db.conn() as c:
        if include_inactive:
            return c.execute(
                "SELECT * FROM embed_widgets WHERE user_id = ? "
                "ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return c.execute(
            "SELECT * FROM embed_widgets WHERE user_id = ? AND is_active = 1 "
            "ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()


def get_embed_widget_by_widget_id(widget_id: str) -> Optional[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM embed_widgets WHERE widget_id = ?", (widget_id,)
        ).fetchone()


def get_user_embed_widget(user_id: int, widget_id: str) -> Optional[sqlite3.Row]:
    """Scoped lookup: returns the row only if ``user_id`` owns it."""
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM embed_widgets WHERE user_id = ? AND widget_id = ?",
            (user_id, widget_id),
        ).fetchone()


def deactivate_embed_widget(user_id: int, widget_id: str) -> bool:
    """Flip is_active=0 for a user's widget. Idempotent."""
    with db.conn() as c:
        cur = c.execute(
            "UPDATE embed_widgets SET is_active = 0 "
            "WHERE user_id = ? AND widget_id = ?",
            (user_id, widget_id),
        )
    return cur.rowcount > 0


def rotate_embed_widget_token(user_id: int, widget_id: str) -> Optional[sqlite3.Row]:
    """Replace token_salt with a fresh nonce. Returns the updated row or None.

    Only rotates tokens for active widgets — rotating a deactivated widget
    would be pointless and may indicate a mistake, so it's a no-op that
    returns ``None``.
    """
    import embed_tokens
    fresh_salt = embed_tokens.new_salt()
    with db.conn() as c:
        cur = c.execute(
            "UPDATE embed_widgets SET token_salt = ? "
            "WHERE user_id = ? AND widget_id = ? AND is_active = 1",
            (fresh_salt, user_id, widget_id),
        )
        if cur.rowcount == 0:
            return None
        return c.execute(
            "SELECT * FROM embed_widgets WHERE widget_id = ?", (widget_id,)
        ).fetchone()


def increment_embed_widget_impression(widget_id: str) -> None:
    """Bump impressions + last_used_at for a widget. Background-safe."""
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "UPDATE embed_widgets SET impressions = impressions + 1, "
            "last_used_at = ? WHERE widget_id = ? AND is_active = 1",
            (now, widget_id),
        )


def deactivate_all_user_embed_widgets(user_id: int) -> int:
    """Deactivate every live widget for a user. Called when a sub lapses.

    Returns the number of rows flipped — useful for telemetry and tests.
    """
    with db.conn() as c:
        cur = c.execute(
            "UPDATE embed_widgets SET is_active = 0 "
            "WHERE user_id = ? AND is_active = 1",
            (user_id,),
        )
    return cur.rowcount


__all__ = [
    'EMBED_WIDGET_TYPES',
    'EMBED_WIDGET_THEMES',
    'MAX_EMBED_WIDGETS_PER_USER',
    'count_user_active_embed_widgets',
    'create_embed_widget',
    'list_user_embed_widgets',
    'get_embed_widget_by_widget_id',
    'get_user_embed_widget',
    'deactivate_embed_widget',
    'rotate_embed_widget_token',
    'increment_embed_widget_impression',
    'deactivate_all_user_embed_widgets',
]

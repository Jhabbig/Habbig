"""Queries extracted from gateway/db.py — subscriptions domain.

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


def list_subscriptions(user_id: int) -> list[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)
        ).fetchall()


def has_active_subscription(user_id: int, dashboard_key: str) -> bool:
    now = int(time.time())
    with db.conn() as c:
        # Admins bypass subscription checks for all dashboards.
        admin_row = c.execute(
            "SELECT is_admin FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if admin_row and admin_row[0]:
            return True
        row = c.execute(
            "SELECT id FROM subscriptions "
            "WHERE user_id = ? AND dashboard_key = ? AND status = 'active' "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (user_id, dashboard_key, now),
        ).fetchone()
    return row is not None


def upsert_subscription(
    user_id: int,
    dashboard_key: str,
    plan: str,
    duration_days: Optional[int] = None,
    source: str = "placeholder",
    stripe_sub_id: Optional[str] = None,
) -> None:
    now = int(time.time())
    expires_at = now + duration_days * 86400 if duration_days else None
    with db.conn() as c:
        c.execute(
            """
            INSERT INTO subscriptions
                (user_id, dashboard_key, plan, status, started_at, expires_at, stripe_sub_id, source)
            VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
            ON CONFLICT(user_id, dashboard_key) DO UPDATE SET
                plan        = excluded.plan,
                status      = 'active',
                started_at  = excluded.started_at,
                expires_at  = excluded.expires_at,
                stripe_sub_id = excluded.stripe_sub_id,
                source      = excluded.source
            """,
            (user_id, dashboard_key, plan, now, expires_at, stripe_sub_id, source),
        )
    # Referral-conversion hook. A paid subscription means the referred user
    # "became paying" for reward purposes. We flag the referral row here so
    # the nightly process_referral_rewards job can grant the gift.
    # Import lazily to avoid a circular import at module load time and to
    # survive a missing db_referrals module (belt-and-braces).
    try:
        import db_referrals as _dbr
        _dbr.mark_referral_converted(user_id)
    except Exception:
        import logging as _logging
        _logging.getLogger("db").exception(
            "referral conversion marker failed for user %s", user_id,
        )


def cancel_subscription(user_id: int, dashboard_key: str) -> None:
    with db.conn() as c:
        c.execute(
            "UPDATE subscriptions SET status = 'cancelled' "
            "WHERE user_id = ? AND dashboard_key = ?",
            (user_id, dashboard_key),
        )


def list_all_subscriptions() -> list[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT s.*, u.email, u.username FROM subscriptions s "
            "JOIN users u ON u.id = s.user_id "
            "ORDER BY s.started_at DESC"
        ).fetchall()


def get_revenue_stats() -> dict:
    """Return subscription counts and breakdown by dashboard and plan."""
    now = int(time.time())
    with db.conn() as c:
        total = c.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        active = c.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE status = 'active' "
            "AND (expires_at IS NULL OR expires_at > ?)", (now,)
        ).fetchone()[0]
        cancelled = c.execute("SELECT COUNT(*) FROM subscriptions WHERE status = 'cancelled'").fetchone()[0]
        expired = c.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE status = 'active' "
            "AND expires_at IS NOT NULL AND expires_at <= ?", (now,)
        ).fetchone()[0]
        # Per-dashboard active counts
        per_dashboard = c.execute(
            "SELECT dashboard_key, plan, COUNT(*) as cnt FROM subscriptions "
            "WHERE status = 'active' AND (expires_at IS NULL OR expires_at > ?) "
            "GROUP BY dashboard_key, plan ORDER BY dashboard_key", (now,)
        ).fetchall()
        return {
            "total": total,
            "active": active,
            "cancelled": cancelled,
            "expired": expired,
            "per_dashboard": per_dashboard,
        }


def create_gift(
    user_id: int,
    gifted_by_admin_id: int,
    subscription_type: str,
    ends_at: Optional[int],
    is_permanent: bool,
    is_enterprise: bool = False,
    enterprise_config: Optional[dict] = None,
    internal_notes: Optional[str] = None,
) -> int:
    import json as _json
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO gifted_subscriptions "
            "(user_id, gifted_by_admin_id, subscription_type, is_enterprise, starts_at, ends_at, "
            "is_permanent, enterprise_config, internal_notes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                gifted_by_admin_id,
                subscription_type,
                1 if is_enterprise else 0,
                int(time.time()),
                ends_at,
                1 if is_permanent else 0,
                _json.dumps(enterprise_config) if enterprise_config else None,
                internal_notes,
                int(time.time()),
            ),
        )
        return cur.lastrowid


def list_active_gifts() -> list[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT g.*, u.email AS user_email, a.email AS granted_by_email "
            "FROM gifted_subscriptions g "
            "LEFT JOIN users u ON g.user_id = u.id "
            "LEFT JOIN users a ON g.gifted_by_admin_id = a.id "
            "WHERE g.revoked = 0 ORDER BY g.created_at DESC"
        ).fetchall()


def get_user_active_gifts(user_id: int) -> list[sqlite3.Row]:
    now = int(time.time())
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM gifted_subscriptions "
            "WHERE user_id = ? AND revoked = 0 AND (is_permanent = 1 OR ends_at IS NULL OR ends_at > ?)",
            (user_id, now),
        ).fetchall()


def revoke_gift(gift_id: int, admin_id: int) -> None:
    with db.conn() as c:
        c.execute(
            "UPDATE gifted_subscriptions SET revoked = 1, revoked_at = ?, revoked_by_admin_id = ? WHERE id = ?",
            (int(time.time()), admin_id, gift_id),
        )


def get_user_intelligence_addon_active(user_id: int) -> bool:
    """True if user has an active Intelligence add-on gift or flag."""
    with db.conn() as c:
        row = c.execute(
            "SELECT intelligence_addon_active, intelligence_addon_period_end FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row and row["intelligence_addon_active"]:
            if not row["intelligence_addon_period_end"] or row["intelligence_addon_period_end"] > int(time.time()):
                return True
    for g in get_user_active_gifts(user_id):
        if g["subscription_type"] == "intelligence_addon":
            return True
        if g["is_enterprise"] and g["enterprise_config"]:
            import json as _json
            try:
                cfg = _json.loads(g["enterprise_config"])
            except Exception:
                cfg = {}
            if cfg.get("intelligence_addon_included"):
                return True
    return False


def set_user_intelligence_addon(user_id: int, active: bool, period_end: Optional[int] = None) -> None:
    with db.conn() as c:
        c.execute(
            "UPDATE users SET intelligence_addon_active = ?, intelligence_addon_period_end = ? WHERE id = ?",
            (1 if active else 0, period_end, user_id),
        )
    # User's effective tier just shifted — drop cached per-user feed + all
    # tier-scoped best-bets pages. Import is deferred so a plain script that
    # only exercises this query helper doesn't pull the cache stack.
    try:
        from cache import ttl_invalidate
        ttl_invalidate.on_subscription_change(user_id)
    except Exception:  # pragma: no cover — cache layer is optional here
        logging.getLogger(__name__).exception(
            "ttl_invalidate.on_subscription_change failed (user=%s)", user_id,
        )


def get_user_subscription_tier(user_id: int) -> str:
    """Best-effort tier label: pro | trader | none (admins map to pro)."""
    with db.conn() as c:
        admin_row = c.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if admin_row and admin_row["is_admin"]:
            return "pro"
        subs = c.execute(
            "SELECT plan FROM subscriptions WHERE user_id = ? AND status = 'active'",
            (user_id,),
        ).fetchall()
    has_pro = any((s["plan"] or "").startswith("pro") for s in subs)
    has_trader = any((s["plan"] or "").startswith("trader") for s in subs)
    if has_pro:
        return "pro"
    if has_trader or subs:
        return "trader"
    return "none"


def has_any_active_subscription(user_id: int) -> bool:
    """True if the user has at least one active subscription on any dashboard.

    Admins bypass this check. Used by cross-dashboard features that require
    being a paying narve.ai customer but aren't scoped to a single product
    (e.g. embed widgets). Distinct from ``has_active_subscription`` which
    takes a dashboard_key.
    """
    now = int(time.time())
    with db.conn() as c:
        admin_row = c.execute(
            "SELECT is_admin FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if admin_row and admin_row[0]:
            return True
        row = c.execute(
            "SELECT 1 FROM subscriptions "
            "WHERE user_id = ? AND status = 'active' "
            "AND (expires_at IS NULL OR expires_at > ?) "
            "LIMIT 1",
            (user_id, now),
        ).fetchone()
    return row is not None


__all__ = [
    'list_subscriptions',
    'has_active_subscription',
    'upsert_subscription',
    'cancel_subscription',
    'list_all_subscriptions',
    'get_revenue_stats',
    'create_gift',
    'list_active_gifts',
    'get_user_active_gifts',
    'revoke_gift',
    'get_user_intelligence_addon_active',
    'set_user_intelligence_addon',
    'get_user_subscription_tier',
    'has_any_active_subscription',
]

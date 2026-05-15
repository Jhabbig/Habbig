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


def _is_user_subscription_paused(c: sqlite3.Connection, user_id: int) -> bool:
    """True if the user's subscription is currently in a pause window.

    AUDIT (HIGH): the four access-check functions in this module
    (``has_active_subscription``, ``has_any_active_subscription``,
    ``get_user_active_subproducts``, ``get_user_subscription_tier``)
    historically ignored ``users.subscription_paused_until``. A paused
    user kept gateway access until ``expires_at`` arrived because the
    queries only filtered on the subscription row, not the user-level
    pause flag.

    The pause column is stored as a DATETIME ISO string (see migration
    094). We compare against ``datetime('now')`` server-side so the
    semantics match the rest of the codebase (server.py /
    billing_routes.py both treat the column the same way), and we
    swallow exceptions so a missing column (pre-094) is read as "not
    paused" — matching ``server._subscription_pause_status``.
    """
    try:
        row = c.execute(
            "SELECT 1 FROM users WHERE id = ? "
            "AND subscription_paused_until IS NOT NULL "
            "AND subscription_paused_until > datetime('now')",
            (user_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        # Column missing — feature not migrated yet. Treat as not paused.
        return False
    return row is not None


def list_subscriptions(user_id: int) -> list[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)
        ).fetchall()


def has_active_subscription(user_id: int, dashboard_key: str) -> bool:
    """Gate-keeping access check for a specific dashboard.

    AUDIT (MED-1, queries/billing): historically this treated a NULL
    ``expires_at`` as "never expires", which papered over the fact that
    the Stripe webhook never wrote the column in the first place. A
    missed ``customer.subscription.deleted`` event therefore left the
    row perpetually-active. The closed-fail rule below treats NULL as
    "no known expiry → not active": every Stripe-sourced row now
    writes ``expires_at = current_period_end``, and any pre-fix NULL
    rows are repaired by migration 193. Manual ``upsert_subscription``
    calls (CLI grants, gift flows) still write explicit timestamps via
    ``duration_days``, so the only NULLs left are bug states that
    deserve to fail closed.
    """
    now = int(time.time())
    with db.conn() as c:
        # Admins bypass subscription checks for all dashboards.
        admin_row = c.execute(
            "SELECT is_admin FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if admin_row and admin_row[0]:
            return True
        # AUDIT (HIGH): a paused user must not retain access until
        # expires_at arrives. Check users.subscription_paused_until
        # before honouring the subscription row.
        if _is_user_subscription_paused(c, user_id):
            return False
        row = c.execute(
            "SELECT id FROM subscriptions "
            "WHERE user_id = ? AND dashboard_key = ? AND status = 'active' "
            "AND expires_at IS NOT NULL AND expires_at > ?",
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


def list_all_subscriptions(limit: int = 100, before_id: int | None = None) -> list[sqlite3.Row]:
    """Cursor-paginated subscription list (newest id first).

    Perf audit #5 — every admin revenue render was scanning the full
    table. Default page is 100, hard-capped at 500. ORDER BY s.id DESC
    keeps the cursor cheap (autoincrement PK).
    """
    capped = max(1, min(int(limit), 500))
    args: list = []
    q = (
        "SELECT s.*, u.email, u.username FROM subscriptions s "
        "JOIN users u ON u.id = s.user_id"
    )
    if before_id is not None:
        q += " WHERE s.id < ?"
        args.append(int(before_id))
    q += " ORDER BY s.id DESC LIMIT ?"
    args.append(capped)
    with db.conn() as c:
        return c.execute(q, args).fetchall()


def get_active_subscription_counts_by_dashboard() -> dict[str, int]:
    """Return {dashboard_key: active_count} for every dashboard.

    Perf audit #5 — SQL-side aggregation. /admin/subproducts no longer
    needs to pull every subscription row just to render counts.
    """
    now = int(time.time())
    with db.conn() as c:
        rows = c.execute(
            "SELECT dashboard_key, COUNT(*) AS cnt FROM subscriptions "
            "WHERE status = 'active' AND (expires_at IS NULL OR expires_at > ?) "
            "GROUP BY dashboard_key",
            (now,),
        ).fetchall()
    return {r["dashboard_key"]: int(r["cnt"]) for r in rows}


def count_active_subscribers(dashboard_key: str) -> int:
    """Count active subscribers for a specific subproduct dashboard_key.

    "Active" means status='active' AND (expires_at IS NULL OR expires_at > now).
    The synthetic ``__plan__`` row (Pro bundle marker) is treated like any
    other dashboard_key — callers that want subproduct-only totals must
    filter the key themselves.
    """
    now = int(time.time())
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS cnt FROM subscriptions "
            "WHERE dashboard_key = ? AND status = 'active' "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (dashboard_key, now),
        ).fetchone()
    return int(row["cnt"]) if row else 0


def get_mrr_by_dashboard() -> dict[str, int]:
    """Return ``{dashboard_key: mrr_cents}`` keyed off the subproduct catalogue.

    MRR is computed as ``active_subscribers * price_usd * 100`` (cents) for
    each subproduct slug. The catalogue (``subproduct.SUBPRODUCTS``) is the
    source of truth for per-product pricing; the main-apex ``__plan__``
    bundle row is not included here — callers that want Pro MRR should
    look up the ``__plan__`` count and apply the £180 bundle price separately.

    Missing subproducts (no active subs) appear with an MRR of 0 so the
    admin UI can render a complete row list.
    """
    counts = get_active_subscription_counts_by_dashboard()
    try:
        from subproduct import SUBPRODUCTS, DASHBOARD_KEY_FOR_SLUG
    except Exception:
        return {}
    out: dict[str, int] = {}
    for slug, cfg in SUBPRODUCTS.items():
        dk = DASHBOARD_KEY_FOR_SLUG.get(slug, slug)
        active = counts.get(dk, 0)
        price_cents = int(round(float(cfg.get("price_usd") or 0.0) * 100))
        out[dk] = active * price_cents
    return out


def get_churn_rate(window_days: int = 7, dashboard_key: Optional[str] = None) -> float:
    """Return rolling churn rate over the last ``window_days`` as a float in [0, 1].

    Churn here is defined operationally as the fraction of subscriptions
    that became *inactive* during the window — i.e. rows with status
    other than 'active', OR rows whose ``expires_at`` fell inside the
    window — over the average active base during the window.

    The schema lacks a ``cancelled_at`` column, so we use ``expires_at``
    as a churn timestamp proxy: a row whose ``expires_at`` is in the
    window is treated as having churned at that time. ``status =
    'cancelled'`` rows with no ``expires_at`` count against the most
    recent window so admins still see cancellations reflected.

    Returns 0.0 when there is no denominator (e.g. brand-new product
    with zero history) so callers can render the value without
    branching.
    """
    if window_days <= 0:
        return 0.0
    now = int(time.time())
    cutoff = now - window_days * 86400
    where_dk = ""
    args_dk: list = []
    if dashboard_key is not None:
        where_dk = " AND dashboard_key = ?"
        args_dk = [dashboard_key]
    with db.conn() as c:
        # Active base now — denominator.
        base_row = c.execute(
            "SELECT COUNT(*) AS cnt FROM subscriptions "
            "WHERE status = 'active' AND (expires_at IS NULL OR expires_at > ?)"
            + where_dk,
            [now] + args_dk,
        ).fetchone()
        base = int(base_row["cnt"]) if base_row else 0
        # Churned in window — numerator. Either:
        #   - expires_at fell inside (cutoff, now]
        #   - status='cancelled' (no expiry timestamp to bucket — count once)
        churn_row = c.execute(
            "SELECT COUNT(*) AS cnt FROM subscriptions WHERE ("
            "(expires_at IS NOT NULL AND expires_at > ? AND expires_at <= ?)"
            " OR (status = 'cancelled' AND (expires_at IS NULL OR expires_at > ?))"
            ")" + where_dk,
            [cutoff, now, cutoff] + args_dk,
        ).fetchone()
        churned = int(churn_row["cnt"]) if churn_row else 0
    denom = base + churned
    if denom <= 0:
        return 0.0
    return churned / denom


def get_new_signups(window_days: int = 30, dashboard_key: Optional[str] = None) -> int:
    """Return the number of subscriptions whose ``started_at`` falls in the window."""
    if window_days <= 0:
        return 0
    now = int(time.time())
    cutoff = now - window_days * 86400
    args: list = [cutoff]
    where_dk = ""
    if dashboard_key is not None:
        where_dk = " AND dashboard_key = ?"
        args.append(dashboard_key)
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS cnt FROM subscriptions WHERE started_at >= ?" + where_dk,
            args,
        ).fetchone()
    return int(row["cnt"]) if row else 0


def get_signups_daily_series(
    window_days: int = 90, dashboard_key: Optional[str] = None
) -> list[int]:
    """Return a ``window_days``-long list of daily new-signup counts, oldest first.

    Day buckets are calendar-day UTC sized; today is the last bucket. Output
    length always equals ``window_days`` so the caller can render fixed-width
    sparkline bars without padding logic.
    """
    if window_days <= 0:
        return []
    now = int(time.time())
    # Anchor day buckets to UTC midnight so the series is stable across
    # render times of day.
    day_start = (now // 86400) * 86400
    cutoff = day_start - (window_days - 1) * 86400
    args: list = [cutoff]
    where_dk = ""
    if dashboard_key is not None:
        where_dk = " AND dashboard_key = ?"
        args.append(dashboard_key)
    buckets = [0] * window_days
    with db.conn() as c:
        rows = c.execute(
            "SELECT started_at FROM subscriptions WHERE started_at >= ?" + where_dk,
            args,
        ).fetchall()
    for row in rows:
        try:
            ts = int(row["started_at"])
        except (TypeError, ValueError, KeyError):
            continue
        idx = (ts - cutoff) // 86400
        if 0 <= idx < window_days:
            buckets[int(idx)] += 1
    return buckets


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
        # AUDIT (HIGH): a paused user has no effective tier — they
        # shouldn't see Pro/Trader-only surfaces while paused.
        if _is_user_subscription_paused(c, user_id):
            return "none"
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


def get_user_primary_subscription(user_id: int) -> Optional[dict]:
    """Return the flagship active subproduct subscription for the user, or None.

    Used by the welcome email (and any future "first run" surface) to tailor
    copy to a specific sub-brand. The "primary" pick is:

      - Skip the special ``__plan__`` row (it's the Trader bundle marker, not a
        subproduct).
      - Skip dashboard_keys that don't map to a known subproduct.
      - Among the remaining active rows, prefer the highest-priced subproduct
        (see ``subproduct.SUBPRODUCTS[*].price_usd``) so a multi-subscriber
        sees their most-expensive product, not just the alphabetically first.
      - Pro/enterprise bundles hold a row on every dashboard_key. Callers
        that want bundle-aware copy should branch on ``get_user_subscription_tier``
        first; this helper still returns a sensible flagship for them too.

    Returns a dict with ``slug``, ``dashboard_key``, ``display_name``,
    ``tagline``, ``subdomain``, and ``plan`` — everything the welcome
    template needs without forcing the caller to round-trip the catalog.
    """
    now = int(time.time())
    with db.conn() as c:
        rows = c.execute(
            "SELECT dashboard_key, plan FROM subscriptions "
            "WHERE user_id = ? AND status = 'active' "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (user_id, now),
        ).fetchall()
    if not rows:
        return None

    # Catalog is import-time defensive: a missing subproduct module means
    # we degrade to "no flagship" rather than crashing the welcome enqueue.
    try:
        from subproduct import SUBPRODUCTS, DASHBOARD_KEY_FOR_SLUG
    except Exception:
        return None

    key_to_slug = {dk: slug for slug, dk in DASHBOARD_KEY_FOR_SLUG.items()}
    candidates: list[tuple[float, str, str, str]] = []
    for row in rows:
        dk = row["dashboard_key"]
        plan = row["plan"]
        if dk == "__plan__":
            continue
        slug = key_to_slug.get(dk)
        if not slug:
            continue
        cfg = SUBPRODUCTS.get(slug)
        if not cfg:
            continue
        price = float(cfg.get("price_usd") or 0.0)
        candidates.append((price, slug, dk, plan))

    if not candidates:
        return None

    # Highest price wins; tie-break alphabetically by slug for determinism.
    candidates.sort(key=lambda t: (-t[0], t[1]))
    price, slug, dk, plan = candidates[0]
    cfg = SUBPRODUCTS[slug]
    return {
        "slug": slug,
        "dashboard_key": dk,
        "display_name": cfg["name"],
        "tagline": cfg["tagline"],
        "subdomain": slug,
        "plan": plan,
    }


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
        # AUDIT (HIGH): paused users should not pass the "is a paying
        # customer" check used by embed widgets etc.
        if _is_user_subscription_paused(c, user_id):
            return False
        row = c.execute(
            "SELECT 1 FROM subscriptions "
            "WHERE user_id = ? AND status = 'active' "
            "AND (expires_at IS NULL OR expires_at > ?) "
            "LIMIT 1",
            (user_id, now),
        ).fetchone()
    return row is not None


def get_user_active_subproducts(user_id: int) -> set[str]:
    """Return the set of dashboard_keys the user is actively subscribed to.

    A user is considered "subscribed" to a subproduct when they have a
    matching ``subscriptions`` row with status='active' that hasn't
    expired. The synthetic ``__plan__`` dashboard_key (used by the
    Pro/Trader plan upsert) is excluded — callers handle Pro tier
    detection separately via ``get_user_subscription_tier``.

    Admins return an empty set here on purpose: callers that want
    "show everything" treat Pro / admin specially before calling.
    """
    now = int(time.time())
    with db.conn() as c:
        # AUDIT (HIGH): a paused user shouldn't surface as actively
        # subscribed to any subproduct — they'd otherwise still see
        # their subscriptions listed in cross-product UIs while paused.
        if _is_user_subscription_paused(c, user_id):
            return set()
        rows = c.execute(
            "SELECT dashboard_key FROM subscriptions "
            "WHERE user_id = ? AND status = 'active' "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (user_id, now),
        ).fetchall()
    return {r["dashboard_key"] for r in rows if r["dashboard_key"] != "__plan__"}


__all__ = [
    'list_subscriptions',
    'has_active_subscription',
    'upsert_subscription',
    'cancel_subscription',
    'list_all_subscriptions',
    'get_active_subscription_counts_by_dashboard',
    'count_active_subscribers',
    'get_mrr_by_dashboard',
    'get_churn_rate',
    'get_new_signups',
    'get_signups_daily_series',
    'get_revenue_stats',
    'create_gift',
    'list_active_gifts',
    'get_user_active_gifts',
    'revoke_gift',
    'get_user_intelligence_addon_active',
    'set_user_intelligence_addon',
    'get_user_subscription_tier',
    'get_user_primary_subscription',
    'has_any_active_subscription',
    'get_user_active_subproducts',
]

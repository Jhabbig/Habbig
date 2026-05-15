"""Queries extracted from gateway/db.py — admin domain.

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


def create_enquiry(email: str, job_title: str, message: str) -> int:
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO enquiries (email, job_title, message, created_at) VALUES (?, ?, ?, ?)",
            (email.strip(), job_title.strip(), message.strip(), int(time.time())),
        )
        return cur.lastrowid


def list_enquiries() -> list[sqlite3.Row]:
    with db.conn() as c:
        return c.execute("SELECT * FROM enquiries ORDER BY created_at DESC").fetchall()


def get_enquiry_by_id(enquiry_id: int) -> Optional[sqlite3.Row]:
    with db.conn() as c:
        return c.execute("SELECT * FROM enquiries WHERE id = ?", (enquiry_id,)).fetchone()


def mark_enquiry_read(enquiry_id: int) -> None:
    with db.conn() as c:
        c.execute("UPDATE enquiries SET read = 1 WHERE id = ?", (enquiry_id,))


def count_unread_enquiries() -> int:
    with db.conn() as c:
        row = c.execute("SELECT COUNT(*) FROM enquiries WHERE read = 0").fetchone()
        return row[0] if row else 0


def create_feedback(
    user_id: Optional[int],
    type_: str,
    message: str,
    priority: Optional[str],
    page_url: Optional[str],
    user_tier: Optional[str],
    screenshot_url: Optional[str] = None,
) -> int:
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO feedback_submissions "
            "(user_id, type, message, priority, page_url, user_tier, screenshot_url, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (user_id, type_, message, priority, page_url, user_tier, screenshot_url, int(time.time())),
        )
        return cur.lastrowid


def list_feedback(status_filter: Optional[str] = None, limit: int = 200) -> list[sqlite3.Row]:
    with db.conn() as c:
        if status_filter:
            return c.execute(
                "SELECT f.*, u.email AS user_email, u.username AS user_username "
                "FROM feedback_submissions f LEFT JOIN users u ON f.user_id = u.id "
                "WHERE f.status = ? ORDER BY f.created_at DESC LIMIT ?",
                (status_filter, limit),
            ).fetchall()
        return c.execute(
            "SELECT f.*, u.email AS user_email, u.username AS user_username "
            "FROM feedback_submissions f LEFT JOIN users u ON f.user_id = u.id "
            "ORDER BY f.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


def update_feedback_status(feedback_id: int, status: str, admin_notes: Optional[str] = None) -> None:
    resolved_at = int(time.time()) if status in ("resolved", "closed") else None
    with db.conn() as c:
        if admin_notes is not None:
            c.execute(
                "UPDATE feedback_submissions SET status = ?, admin_notes = ?, resolved_at = ? WHERE id = ?",
                (status, admin_notes, resolved_at, feedback_id),
            )
        else:
            c.execute(
                "UPDATE feedback_submissions SET status = ?, resolved_at = ? WHERE id = ?",
                (status, resolved_at, feedback_id),
            )


def count_feedback_by_status(status: str = "open") -> int:
    with db.conn() as c:
        row = c.execute("SELECT COUNT(*) FROM feedback_submissions WHERE status = ?", (status,)).fetchone()
    return row[0] if row else 0


def record_analytics_event(
    event_type: str,
    user_id: Optional[int],
    session_id: Optional[str],
    page: Optional[str],
    referrer: Optional[str],
    ip_hash: str,
    user_agent_category: Optional[str],
    properties: Optional[dict] = None,
) -> int:
    import json as _json
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO analytics_events "
            "(event_type, user_id, session_id, page, referrer, ip_hash, user_agent_category, properties, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_type,
                user_id,
                session_id,
                page,
                referrer,
                ip_hash,
                user_agent_category,
                _json.dumps(properties or {}),
                int(time.time()),
            ),
        )
        return cur.lastrowid


def get_analytics_prerelease(since: int) -> dict:
    with db.conn() as c:
        rows = c.execute(
            "SELECT event_type, COUNT(*) AS c, COUNT(DISTINCT ip_hash) AS u "
            "FROM analytics_events WHERE created_at >= ? GROUP BY event_type",
            (since,),
        ).fetchall()
    out = {
        "page_views": 0, "unique_visitors": 0,
        "newsletter_signups": 0, "gate_entries": 0, "gate_successes": 0, "gate_failures": 0,
    }
    total_unique = 0
    for r in rows:
        et = r["event_type"]
        if et == "page_view":
            out["page_views"] = r["c"]
            out["unique_visitors"] = r["u"]
            total_unique = r["u"]
        elif et == "newsletter_signup":
            out["newsletter_signups"] = r["c"]
        elif et == "gate_entered":
            out["gate_entries"] = r["c"]
        elif et == "gate_success":
            out["gate_successes"] = r["c"]
        elif et == "gate_failure":
            out["gate_failures"] = r["c"]
    if total_unique == 0:
        with db.conn() as c:
            row = c.execute(
                "SELECT COUNT(DISTINCT ip_hash) AS u FROM analytics_events WHERE created_at >= ?",
                (since,),
            ).fetchone()
            out["unique_visitors"] = row["u"] if row else 0
    return out


def get_analytics_users(since: int) -> dict:
    """Growth series — users per day since `since`. Returns totals + a series."""
    with db.conn() as c:
        total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        week_cut = int(time.time()) - 7 * 86400
        month_cut = int(time.time()) - 30 * 86400
        active_week = c.execute(
            "SELECT COUNT(DISTINCT user_id) FROM sessions WHERE created_at >= ? AND user_id IS NOT NULL",
            (week_cut,),
        ).fetchone()[0]
        active_month = c.execute(
            "SELECT COUNT(DISTINCT user_id) FROM sessions WHERE created_at >= ? AND user_id IS NOT NULL",
            (month_cut,),
        ).fetchone()[0]
        churn_month = c.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE status = 'cancelled' AND started_at >= ?",
            (month_cut,),
        ).fetchone()[0]
        rows = c.execute(
            "SELECT DATE(created_at, 'unixepoch') AS d, COUNT(*) AS c FROM users "
            "WHERE created_at >= ? GROUP BY d ORDER BY d",
            (since,),
        ).fetchall()
    series = []
    running = 0
    for r in rows:
        running += r["c"]
        series.append({"date": r["d"], "count": running})
    return {
        "total_users": total,
        "active_week": active_week,
        "active_month": active_month,
        "churn_month": churn_month,
        "growth_series": series,
    }


def get_analytics_revenue() -> dict:
    """Estimated MRR / ARR / breakdown from active subscriptions."""
    plan_mrr = {
        "trader_monthly": 75,
        "trader_annual": 63,
        "pro_monthly": 180,
        "pro_annual": 153,
        "trading_addon_monthly": 25,
        "trading_addon_annual": 21,
        "intelligence_monthly": 25,
        "enterprise": 500,
    }
    with db.conn() as c:
        subs = c.execute(
            "SELECT plan, COUNT(*) AS c FROM subscriptions WHERE status = 'active' GROUP BY plan"
        ).fetchall()
    breakdown = []
    mrr = 0
    total_active = 0
    for r in subs:
        plan = r["plan"] or "unknown"
        count = r["c"]
        total_active += count
        monthly = plan_mrr.get(plan, 0)
        row_mrr = count * monthly
        mrr += row_mrr
        breakdown.append({"label": plan, "count": count, "mrr_gbp": row_mrr})
    return {
        "mrr": mrr,
        "arr": mrr * 12,
        "subs_active": total_active,
        "breakdown": breakdown,
    }


def get_analytics_features(since: int) -> dict:
    import json as _json
    with db.conn() as c:
        rows = c.execute(
            "SELECT event_type, COUNT(*) AS c FROM analytics_events "
            "WHERE created_at >= ? GROUP BY event_type",
            (since,),
        ).fetchall()
    by_type = {r["event_type"]: r["c"] for r in rows}
    top_markets: dict[str, int] = {}
    top_sources: dict[str, int] = {}
    top_keywords: dict[str, int] = {}
    with db.conn() as c:
        for r in c.execute(
            "SELECT event_type, properties FROM analytics_events WHERE created_at >= ?",
            (since,),
        ):
            try:
                props = _json.loads(r["properties"] or "{}")
            except Exception:
                props = {}
            if r["event_type"] == "market_viewed" and props.get("market"):
                top_markets[props["market"]] = top_markets.get(props["market"], 0) + 1
            elif r["event_type"] == "source_viewed" and props.get("source"):
                top_sources[props["source"]] = top_sources.get(props["source"], 0) + 1
            elif r["event_type"] == "signal_search" and props.get("keyword"):
                top_keywords[props["keyword"]] = top_keywords.get(props["keyword"], 0) + 1

    def top_n(d: dict, n: int = 10) -> list[dict]:
        items = sorted(d.items(), key=lambda x: x[1], reverse=True)[:n]
        return [{"label": k, "count": v} for k, v in items]

    return {
        "feed_views": by_type.get("feed_view", 0),
        "bestbets_views": by_type.get("bestbets_view", 0),
        "source_views": by_type.get("source_viewed", 0),
        "market_views": by_type.get("market_viewed", 0),
        "signal_runs": by_type.get("signal_search", 0),
        "cred_refreshes": by_type.get("credibility_refresh", 0),
        "bets_placed": by_type.get("bet_placed", 0),
        "top_markets": top_n(top_markets),
        "top_sources": top_n(top_sources),
        "top_keywords": top_n(top_keywords),
    }


def insert_audit_log(
    *,
    admin_user_id: Optional[int],
    admin_email: Optional[str],
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    target_description: Optional[str] = None,
    before_state: Optional[str] = None,
    after_state: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
    notes: Optional[str] = None,
) -> int:
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO audit_log ("
            "timestamp, admin_user_id, admin_email, action, target_type, "
            "target_id, target_description, before_state, after_state, "
            "ip_address, user_agent, request_id, notes"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(time.time()),
                admin_user_id,
                admin_email,
                action,
                target_type,
                str(target_id) if target_id is not None else None,
                target_description,
                before_state,
                after_state,
                ip_address,
                user_agent,
                request_id,
                notes,
            ),
        )
        return cur.lastrowid


def query_audit_log(
    *,
    action: Optional[str] = None,
    admin_user_id: Optional[int] = None,
    target_type: Optional[str] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[sqlite3.Row], int]:
    """Paginated query. Returns (rows, total_count) so the caller can render
    pagination controls without a separate count round-trip at the API layer.
    """
    where = []
    params: list = []
    if action:
        where.append("action = ?")
        params.append(action)
    if admin_user_id:
        where.append("admin_user_id = ?")
        params.append(admin_user_id)
    if target_type:
        where.append("target_type = ?")
        params.append(target_type)
    if from_ts:
        where.append("timestamp >= ?")
        params.append(from_ts)
    if to_ts:
        where.append("timestamp <= ?")
        params.append(to_ts)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    offset = max(0, (page - 1) * page_size)
    with db.conn() as c:
        total_row = c.execute(
            f"SELECT COUNT(*) AS n FROM audit_log{where_sql}", tuple(params)
        ).fetchone()
        total = int(total_row["n"] if total_row else 0)
        rows = c.execute(
            f"SELECT * FROM audit_log{where_sql} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            tuple(params) + (page_size, offset),
        ).fetchall()
    return rows, total


def export_audit_log_csv(
    *,
    action: Optional[str] = None,
    admin_user_id: Optional[int] = None,
    target_type: Optional[str] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
) -> str:
    """Return CSV text of every row matching the filters. No pagination."""
    import csv as _csv
    import io as _io
    where = []
    params: list = []
    if action:
        where.append("action = ?")
        params.append(action)
    if admin_user_id:
        where.append("admin_user_id = ?")
        params.append(admin_user_id)
    if target_type:
        where.append("target_type = ?")
        params.append(target_type)
    if from_ts:
        where.append("timestamp >= ?")
        params.append(from_ts)
    if to_ts:
        where.append("timestamp <= ?")
        params.append(to_ts)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    with db.conn() as c:
        rows = c.execute(
            f"SELECT * FROM audit_log{where_sql} ORDER BY timestamp DESC",
            tuple(params),
        ).fetchall()
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow([
        "timestamp_iso", "admin_user_id", "admin_email", "action",
        "target_type", "target_id", "target_description",
        "ip_address", "user_agent", "request_id", "notes",
        "before_state", "after_state",
    ])
    for r in rows:
        w.writerow([
            time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["timestamp"])),
            r["admin_user_id"] or "",
            r["admin_email"] or "",
            r["action"],
            r["target_type"] or "",
            r["target_id"] or "",
            r["target_description"] or "",
            r["ip_address"] or "",
            r["user_agent"] or "",
            r["request_id"] or "",
            r["notes"] or "",
            r["before_state"] or "",
            r["after_state"] or "",
        ])
    return buf.getvalue()


def _hash_impersonation_token(raw: str) -> str:
    """SHA-256 hex of the raw cookie value — matches migration 192's
    ``cookie_token_hash`` column. Stable, no salt: cookie tokens are
    already 48 bytes of CSPRNG entropy, so a rainbow table is pointless.
    """
    return hashlib.sha256(raw.encode()).hexdigest()


def create_impersonation_session(
    *,
    admin_user_id: int,
    target_user_id: int,
    reason: str,
    ip_address=None,
    user_agent=None,
) -> dict:
    """Create an impersonation session, returning {id, cookie_token, started_at}.

    The raw cookie_token is set on the admin's browser; every request
    that presents it is treated as the admin viewing the target user.
    At rest we persist only the SHA-256 hash (``cookie_token_hash``,
    migration 192) so a DB dump cannot replay live impersonation
    cookies. The legacy ``cookie_token`` column carries a per-row
    hashed-sentinel so the existing NOT NULL/UNIQUE constraint can't
    trip; the raw token itself is never persisted.
    """
    token = secrets.token_urlsafe(48)
    token_hash = _hash_impersonation_token(token)
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO impersonation_sessions "
            "(admin_user_id, target_user_id, cookie_token, cookie_token_hash, "
            "reason, ip_address, user_agent, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                admin_user_id, target_user_id,
                f"hashed:{token_hash[:32]}",  # legacy column sentinel — raw never stored
                token_hash,
                reason, ip_address, user_agent, now,
            ),
        )
        return {"id": cur.lastrowid, "cookie_token": token, "started_at": now}


def get_impersonation_session_by_token(token: str):
    """Look up by raw cookie token. Hashes before SELECT so the at-rest
    representation (``cookie_token_hash``, migration 192) is what the
    query actually matches. Does NOT filter on ended_at so callers decide.
    """
    if not token:
        return None
    token_hash = _hash_impersonation_token(token)
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM impersonation_sessions WHERE cookie_token_hash = ?",
            (token_hash,),
        ).fetchone()


def get_impersonation_session(session_id: int):
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM impersonation_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()


def end_impersonation_session(session_id: int, end_reason: str = "admin_ended") -> None:
    with db.conn() as c:
        c.execute(
            "UPDATE impersonation_sessions SET ended_at = ?, end_reason = ? "
            "WHERE id = ? AND ended_at IS NULL",
            (int(time.time()), end_reason, session_id),
        )


def record_impersonation_action(
    *,
    session_id: int,
    method: str,
    path: str,
    status_code,
    was_blocked: bool,
) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT INTO impersonation_actions "
            "(session_id, timestamp, method, path, status_code, was_blocked) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, int(time.time()), method, path, status_code, 1 if was_blocked else 0),
        )
        c.execute(
            "UPDATE impersonation_sessions SET action_count = action_count + 1 WHERE id = ?",
            (session_id,),
        )


def list_impersonation_sessions(limit: int = 100):
    with db.conn() as c:
        return c.execute(
            "SELECT s.*, "
            "  a.email AS admin_email, a.username AS admin_username, "
            "  t.email AS target_email, t.username AS target_username "
            "FROM impersonation_sessions s "
            "LEFT JOIN users a ON a.id = s.admin_user_id "
            "LEFT JOIN users t ON t.id = s.target_user_id "
            "ORDER BY s.started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


def list_impersonation_actions(session_id: int, limit: int = 500):
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM impersonation_actions WHERE session_id = ? "
            "ORDER BY timestamp ASC LIMIT ?",
            (session_id, limit),
        ).fetchall()


def list_feature_flags(subproduct_key="__all__"):
    """List flags.

    Default returns every row across all subproducts (used by the admin
    listing page). Pass ``subproduct_key=None`` for global-only rows, or
    a specific slug for a single subproduct's overrides.
    """
    with db.conn() as c:
        if subproduct_key == "__all__":
            return c.execute(
                "SELECT * FROM feature_flags "
                "ORDER BY key ASC, (subproduct_key IS NOT NULL), subproduct_key ASC"
            ).fetchall()
        if subproduct_key is None:
            return c.execute(
                "SELECT * FROM feature_flags WHERE subproduct_key IS NULL "
                "ORDER BY key ASC"
            ).fetchall()
        return c.execute(
            "SELECT * FROM feature_flags WHERE subproduct_key = ? ORDER BY key ASC",
            (subproduct_key,),
        ).fetchall()


def get_feature_flag(key: str, subproduct_key=None):
    """Fetch a single flag row for a (key, subproduct_key) pair.

    ``subproduct_key=None`` returns the global row (subproduct_key IS NULL).
    """
    with db.conn() as c:
        if subproduct_key is None:
            return c.execute(
                "SELECT * FROM feature_flags WHERE key = ? AND subproduct_key IS NULL",
                (key,),
            ).fetchone()
        return c.execute(
            "SELECT * FROM feature_flags WHERE key = ? AND subproduct_key = ?",
            (key, subproduct_key),
        ).fetchone()


def create_feature_flag(
    *,
    key: str,
    name: str,
    description: str = "",
    enabled_globally: bool = False,
    enabled_for_tiers=None,
    enabled_for_user_ids=None,
    disabled_for_user_ids=None,
    rollout_percentage: int = 0,
    updated_by_admin_id=None,
    subproduct_key=None,
) -> int:
    import json as _json
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO feature_flags "
            "(key, name, description, enabled_globally, enabled_for_tiers, "
            " enabled_for_user_ids, disabled_for_user_ids, rollout_percentage, "
            " created_at, updated_at, updated_by_admin_id, subproduct_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key, name, description,
                1 if enabled_globally else 0,
                _json.dumps(enabled_for_tiers or []),
                _json.dumps(enabled_for_user_ids or []),
                _json.dumps(disabled_for_user_ids or []),
                max(0, min(100, int(rollout_percentage))),
                now, now, updated_by_admin_id,
                subproduct_key,
            ),
        )
        return cur.lastrowid


def update_feature_flag(
    key: str,
    *,
    name=None,
    description=None,
    enabled_globally=None,
    enabled_for_tiers=None,
    enabled_for_user_ids=None,
    disabled_for_user_ids=None,
    rollout_percentage=None,
    updated_by_admin_id=None,
    subproduct_key=None,
) -> bool:
    """Update the (key, subproduct_key) row.

    ``subproduct_key=None`` (the default) updates the global row; pass a
    slug to update that subproduct's override.
    """
    import json as _json
    fields = []
    params = []
    if name is not None:
        fields.append("name = ?"); params.append(name)
    if description is not None:
        fields.append("description = ?"); params.append(description)
    if enabled_globally is not None:
        fields.append("enabled_globally = ?"); params.append(1 if enabled_globally else 0)
    if enabled_for_tiers is not None:
        fields.append("enabled_for_tiers = ?"); params.append(_json.dumps(enabled_for_tiers))
    if enabled_for_user_ids is not None:
        fields.append("enabled_for_user_ids = ?"); params.append(_json.dumps(enabled_for_user_ids))
    if disabled_for_user_ids is not None:
        fields.append("disabled_for_user_ids = ?"); params.append(_json.dumps(disabled_for_user_ids))
    if rollout_percentage is not None:
        fields.append("rollout_percentage = ?")
        params.append(max(0, min(100, int(rollout_percentage))))
    if updated_by_admin_id is not None:
        fields.append("updated_by_admin_id = ?"); params.append(updated_by_admin_id)
    if not fields:
        return False
    fields.append("updated_at = ?"); params.append(int(time.time()))
    if subproduct_key is None:
        where = "WHERE key = ? AND subproduct_key IS NULL"
        params.append(key)
    else:
        where = "WHERE key = ? AND subproduct_key = ?"
        params.append(key)
        params.append(subproduct_key)
    with db.conn() as c:
        cur = c.execute(
            f"UPDATE feature_flags SET {', '.join(fields)} {where}",
            tuple(params),
        )
        return cur.rowcount > 0


def delete_feature_flag(key: str, subproduct_key=None) -> bool:
    with db.conn() as c:
        if subproduct_key is None:
            cur = c.execute(
                "DELETE FROM feature_flags WHERE key = ? AND subproduct_key IS NULL",
                (key,),
            )
        else:
            cur = c.execute(
                "DELETE FROM feature_flags WHERE key = ? AND subproduct_key = ?",
                (key, subproduct_key),
            )
        return cur.rowcount > 0


def record_feature_flag_event(flag_key: str, user_id, result: bool) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT INTO feature_flag_events (flag_key, user_id, result, timestamp) "
            "VALUES (?, ?, ?, ?)",
            (flag_key, user_id, 1 if result else 0, int(time.time())),
        )


def list_email_templates():
    with db.conn() as c:
        return c.execute("SELECT * FROM email_templates ORDER BY key ASC").fetchall()


def get_email_template(key: str):
    with db.conn() as c:
        return c.execute("SELECT * FROM email_templates WHERE key = ?", (key,)).fetchone()


def upsert_email_template(
    *,
    key: str,
    subject: str,
    body_html: str,
    body_text=None,
    variables=None,
    is_active: bool = True,
    updated_by_admin_id=None,
) -> None:
    import json as _json
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "INSERT INTO email_templates "
            "(key, subject, body_html, body_text, variables, is_active, updated_at, updated_by_admin_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "  subject = excluded.subject, "
            "  body_html = excluded.body_html, "
            "  body_text = excluded.body_text, "
            "  variables = excluded.variables, "
            "  is_active = excluded.is_active, "
            "  updated_at = excluded.updated_at, "
            "  updated_by_admin_id = excluded.updated_by_admin_id",
            (
                key, subject, body_html, body_text,
                _json.dumps(variables or []),
                1 if is_active else 0,
                now, updated_by_admin_id,
            ),
        )


def delete_email_template(key: str) -> bool:
    with db.conn() as c:
        cur = c.execute("DELETE FROM email_templates WHERE key = ?", (key,))
        return cur.rowcount > 0


# ── Unified email-address aggregator ─────────────────────────────────────


# Discriminator labels for the source column. Kept here (not in the route
# handler) so tests and the CSV/JSON export use the exact same strings.
EMAIL_SOURCE_LABELS = (
    "newsletter",    # newsletter_subscribers, any source != 'prerelease'
    "user",          # users.email, real account (password_hash != '')
    "enquiry",       # enquiries.email (contact/support form)
    "feedback",      # feedback_submissions.user_id -> users.email
    "prerelease",    # newsletter_subscribers where source == 'prerelease'
    "shell",         # users.email where password_hash == '' (admin-created stub)
    "outbound",      # background_jobs payload (name='send_email')
    "unsubscribe",   # newsletter_subscribers where unsubscribed_at IS NOT NULL
    "invite",        # invite_tokens.target_email (pre-claim invitations)
)


def _aggregate_email_rows(c, status: Optional[str] = None) -> list[dict]:
    """Internal — pull every email-bearing row from each of the 9 sources.

    Each row carries a ``source`` discriminator, a ``ts`` (unix seconds for
    last activity), an ``email``, and optional ``user_id``/``status`` fields.
    The UNION is materialised in-Python (rather than SQL UNION ALL) because
    every source has a different shape and dedupe across sources is easier
    in-Python than as a CTE. Per-source SQL is still pushed down where
    cheap, including the optional ``status`` filter below.

    ``status`` (when given) is matched against each source's own concept of
    status — newsletter rows fork to confirmed/pending/unsubscribed based on
    ``confirmed_at``/``unsubscribed_at``; users use the ``suspended`` flag;
    outbound + invite have their own ``status`` columns. Sources with no
    concept of the requested status (enquiry, feedback when status≠active)
    are skipped entirely so they don't waste a round trip.

    Returns rows roughly newest-first per source. The caller is expected
    to apply final ordering / dedupe.
    """
    rows: list[dict] = []
    st = (status or "").strip().lower() or None

    # 1) Newsletter subscribers — both prerelease and post-launch live here;
    #    the ``source`` column on the table itself tells us which surface
    #    captured them. Unsubscribed rows fork to the 'unsubscribe' bucket.
    #    Status filter pushed into SQL: confirmed/pending/unsubscribed each
    #    map to a distinct WHERE clause; anything else means "don't emit".
    ns_sql = (
        "SELECT email, subscribed_at, confirmed_at, unsubscribed_at, source "
        "FROM newsletter_subscribers"
    )
    ns_skip = False
    if st == "confirmed":
        ns_sql += " WHERE unsubscribed_at IS NULL AND confirmed_at IS NOT NULL"
    elif st == "pending":
        ns_sql += " WHERE unsubscribed_at IS NULL AND confirmed_at IS NULL"
    elif st == "unsubscribed":
        ns_sql += " WHERE unsubscribed_at IS NOT NULL"
    elif st in ("active", "suspended", "queued", "sent", "failed",
                "scheduled", "unclaimed", "claimed", "revoked"):
        # No newsletter row carries any of these statuses.
        ns_skip = True
    try:
        ns = [] if ns_skip else c.execute(ns_sql).fetchall()
    except Exception:
        ns = []
    for r in ns:
        email = (r["email"] or "").strip()
        if not email:
            continue
        sub_src = (r["source"] or "").strip().lower()
        unsub_at = r["unsubscribed_at"]
        if unsub_at:
            rows.append({
                "email": email,
                "source": "unsubscribe",
                "ts": int(unsub_at),
                "first_seen": int(r["subscribed_at"] or 0) or None,
                "user_id": None,
                "status": "unsubscribed",
            })
            continue
        bucket = "prerelease" if sub_src == "prerelease" else "newsletter"
        rows.append({
            "email": email,
            "source": bucket,
            "ts": int(r["subscribed_at"] or 0),
            "first_seen": int(r["subscribed_at"] or 0) or None,
            "user_id": None,
            "status": "confirmed" if r["confirmed_at"] else "pending",
        })

    # 2 + 6) Users — registered accounts vs admin-created shell rows
    #        (password_hash == '' means the row was provisioned but the
    #        user never set a password). Status filter maps to ``suspended``.
    us_sql = "SELECT id, email, created_at, password_hash, suspended FROM users"
    us_skip = False
    if st == "active":
        us_sql += " WHERE suspended = 0"
    elif st == "suspended":
        us_sql += " WHERE suspended = 1"
    elif st in ("confirmed", "pending", "unsubscribed", "queued", "sent",
                "failed", "scheduled", "unclaimed", "claimed", "revoked"):
        us_skip = True
    try:
        us = [] if us_skip else c.execute(us_sql).fetchall()
    except Exception:
        us = []
    for r in us:
        email = (r["email"] or "").strip()
        if not email:
            continue
        is_shell = not (r["password_hash"] or "").strip()
        rows.append({
            "email": email,
            "source": "shell" if is_shell else "user",
            "ts": int(r["created_at"] or 0),
            "first_seen": int(r["created_at"] or 0) or None,
            "user_id": int(r["id"]),
            "status": "suspended" if r["suspended"] else "active",
        })

    # 3) Enquiries (contact/support form). Always status='active' — skip
    #    the round trip if the caller asked for anything else.
    eq_skip = bool(st) and st != "active"
    try:
        eq = [] if eq_skip else c.execute(
            "SELECT email, created_at FROM enquiries"
        ).fetchall()
    except Exception:
        eq = []
    for r in eq:
        email = (r["email"] or "").strip()
        if not email:
            continue
        rows.append({
            "email": email,
            "source": "enquiry",
            "ts": int(r["created_at"] or 0),
            "first_seen": int(r["created_at"] or 0) or None,
            "user_id": None,
            "status": "active",
        })

    # 4) Feedback submitters — linked back to users.email by user_id. Anonymous
    #    feedback (user_id IS NULL) does not surface here since there's no
    #    email captured server-side. Always status='active' — same skip as
    #    enquiry.
    fb_skip = bool(st) and st != "active"
    try:
        fb = [] if fb_skip else c.execute(
            "SELECT f.user_id, f.created_at, u.email "
            "FROM feedback_submissions f "
            "JOIN users u ON f.user_id = u.id "
            "WHERE f.user_id IS NOT NULL"
        ).fetchall()
    except Exception:
        fb = []
    for r in fb:
        email = (r["email"] or "").strip()
        if not email:
            continue
        rows.append({
            "email": email,
            "source": "feedback",
            "ts": int(r["created_at"] or 0),
            "first_seen": int(r["created_at"] or 0) or None,
            "user_id": int(r["user_id"]) if r["user_id"] is not None else None,
            "status": "active",
        })

    # 7) Outbound queue recipients — inside JSON payload on background_jobs
    #    rows where name='send_email'. We cap at 2000 most-recent rows to
    #    bound the result set. SQLite's json_extract pulls the 'to' field
    #    directly so we avoid parsing each payload in Python. Status filter
    #    maps to the background_jobs ``status`` column directly.
    ob_sql = (
        "SELECT json_extract(payload, '$.to') AS recipient, "
        "       enqueued_at, status "
        "FROM background_jobs "
        "WHERE name = 'send_email' "
        "  AND json_valid(payload) = 1 "
        "  AND json_extract(payload, '$.to') IS NOT NULL"
    )
    ob_params: tuple = ()
    ob_skip = False
    if st in ("queued", "sent", "failed", "scheduled"):
        ob_sql += " AND status = ?"
        ob_params = (st,)
    elif st in ("confirmed", "pending", "unsubscribed", "active",
                "suspended", "unclaimed", "claimed", "revoked"):
        ob_skip = True
    ob_sql += " ORDER BY enqueued_at DESC LIMIT 2000"
    try:
        ob = [] if ob_skip else c.execute(ob_sql, ob_params).fetchall()
    except Exception:
        ob = []
    for r in ob:
        to = (r["recipient"] or "").strip()
        if not to:
            continue
        rows.append({
            "email": to,
            "source": "outbound",
            "ts": int(r["enqueued_at"] or 0),
            "first_seen": int(r["enqueued_at"] or 0) or None,
            "user_id": None,
            "status": (r["status"] or "queued"),
        })

    # 9) Invite token targets. Only rows that captured a target_email at
    #    creation surface here; legacy invites had no target email. Status
    #    filter maps to the invite_tokens ``status`` column directly.
    iv_sql = (
        "SELECT target_email, created_at, status, claimed_by_user_id "
        "FROM invite_tokens "
        "WHERE target_email IS NOT NULL AND target_email != ''"
    )
    iv_params: tuple = ()
    iv_skip = False
    if st in ("unclaimed", "claimed", "revoked"):
        iv_sql += " AND status = ?"
        iv_params = (st,)
    elif st in ("confirmed", "pending", "unsubscribed", "active",
                "suspended", "queued", "sent", "failed", "scheduled"):
        iv_skip = True
    try:
        iv = [] if iv_skip else c.execute(iv_sql, iv_params).fetchall()
    except Exception:
        iv = []
    for r in iv:
        email = (r["target_email"] or "").strip()
        if not email:
            continue
        rows.append({
            "email": email,
            "source": "invite",
            "ts": int(r["created_at"] or 0),
            "first_seen": int(r["created_at"] or 0) or None,
            "user_id": (
                int(r["claimed_by_user_id"])
                if r["claimed_by_user_id"] is not None else None
            ),
            "status": (r["status"] or "unclaimed"),
        })

    return rows


_EMAIL_SORT_FIELDS = {
    # admin UI column key → row dict key
    "ts": "ts",                  # default: last activity
    "first_seen": "first_seen",  # oldest sighting
    "email": "email",            # alphabetical
    "source": "source",
    "status": "status",
    "user_id": "user_id",
}


def aggregate_email_addresses(
    c=None,
    *,
    source=None,
    q=None,
    since=None,
    until=None,
    status=None,
    limit=200,
    offset=0,
    sort="ts",
    sort_dir="desc",
):
    """Return a unified view across every email-collection surface.

    Surfaces 9 distinct sources (see ``EMAIL_SOURCE_LABELS``). Each row in
    the return value is a dict shaped:

        {
            "email":        str,        # lower-cased for display, stable for dedupe
            "email_raw":    str,        # the exact email we found (case preserved)
            "source":       str,        # one of EMAIL_SOURCE_LABELS
            "ts":           int,        # last activity (unix seconds)
            "first_seen":   int|None,   # earliest sighting across all sources
            "user_id":      int|None,   # linked users.id if any source carried one
            "status":       str,        # source-specific (e.g. confirmed, queued)
            "all_sources":  list[str],  # every source this email appears in
        }

    Dedup semantics: rows are keyed by ``lower(email)``. When the same
    email appears in multiple sources, we keep the row from the source
    with the *most recent* ``ts`` and stash every other source in
    ``all_sources`` so the UI can render the badge stack. ``first_seen``
    is the min(ts) across every sighting.

    Filters:
      * ``source``  — exact-match against ``EMAIL_SOURCE_LABELS``. None means
                      every source.
      * ``q``       — substring match (case-insensitive) on the email column.
      * ``since``   — only rows with ts >= since (unix seconds).
      * ``until``   — only rows with ts <= until (unix seconds).
      * ``limit`` / ``offset`` — applied after sorting by ts DESC.

    Pass ``c`` to reuse an existing connection (the test harness does this).
    If ``c`` is None, the function opens its own connection.
    """
    own_conn = c is None
    if own_conn:
        with db.conn() as new_c:
            return aggregate_email_addresses(
                new_c, source=source, q=q, since=since, until=until,
                status=status, limit=limit, offset=offset,
                sort=sort, sort_dir=sort_dir,
            )

    # Push the status filter into the per-source SQL when we can — saves
    # SELECTing rows we're about to drop anyway. The Python tail below
    # still re-applies the filter so any source whose status concept we
    # don't model in SQL is still handled correctly.
    raw_rows = _aggregate_email_rows(c, status=status)

    # Group by lower(email). Keep the row from the most recent source; track
    # every other source for the all_sources stack; track first_seen across
    # all sightings; promote user_id if any source had it.
    grouped: dict[str, dict] = {}
    for r in raw_rows:
        key = (r["email"] or "").strip().lower()
        if not key:
            continue
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = {
                "email": key,
                "email_raw": r["email"],
                "source": r["source"],
                "ts": r["ts"],
                "first_seen": r["first_seen"],
                "user_id": r["user_id"],
                "status": r["status"],
                "all_sources": [r["source"]],
            }
            continue
        # Track this source even if we don't promote it.
        if r["source"] not in existing["all_sources"]:
            existing["all_sources"].append(r["source"])
        # Promote user_id if we didn't have one yet.
        if existing["user_id"] is None and r["user_id"] is not None:
            existing["user_id"] = r["user_id"]
        # Track earliest sighting.
        fs = r["first_seen"]
        if fs is not None:
            if existing["first_seen"] is None or fs < existing["first_seen"]:
                existing["first_seen"] = fs
        # Promote the row if this source is newer.
        if r["ts"] > existing["ts"]:
            existing["source"] = r["source"]
            existing["ts"] = r["ts"]
            existing["status"] = r["status"]
            existing["email_raw"] = r["email"]

    out = list(grouped.values())

    # Filter — source (exact), q (substring), since/until (ts).
    if source:
        src = str(source).strip().lower()
        out = [r for r in out if (
            r["source"] == src or src in r["all_sources"]
        )]
    if q:
        needle = str(q).strip().lower()
        if needle:
            out = [r for r in out if needle in r["email"]]
    if since is not None:
        try:
            since_i = int(since)
            out = [r for r in out if (r["ts"] or 0) >= since_i]
        except (TypeError, ValueError):
            pass
    if until is not None:
        try:
            until_i = int(until)
            out = [r for r in out if (r["ts"] or 0) <= until_i]
        except (TypeError, ValueError):
            pass
    if status:
        st = str(status).strip().lower()
        if st:
            out = [r for r in out if (r.get("status") or "").lower() == st]

    # Sort. Whitelist the field so a malicious ?sort= can't crash the page
    # or expose dict internals. Fall back to ts/desc on anything unknown.
    sort_key = _EMAIL_SORT_FIELDS.get(str(sort or "ts").strip().lower(), "ts")
    direction = str(sort_dir or "desc").strip().lower()
    reverse = direction != "asc"

    def _sort_value(row):
        v = row.get(sort_key)
        if sort_key in ("email", "source", "status"):
            return (v or "").lower()
        return v or 0

    out.sort(key=_sort_value, reverse=reverse)

    # Pagination.
    try:
        limit_i = max(1, min(2000, int(limit)))
    except (TypeError, ValueError):
        limit_i = 200
    try:
        offset_i = max(0, int(offset))
    except (TypeError, ValueError):
        offset_i = 0

    return out[offset_i:offset_i + limit_i]


def count_email_addresses_by_source(c=None) -> dict[str, int]:
    """Return a {source: distinct_emails} dict over every surface.

    Used by the /admin/email-addresses page to populate the source-filter
    badge counts in the header. Counts distinct lower(email) per source
    so multi-source emails are accounted for in every bucket they touch.
    """
    own_conn = c is None
    if own_conn:
        with db.conn() as new_c:
            return count_email_addresses_by_source(new_c)
    raw_rows = _aggregate_email_rows(c)
    by_source: dict[str, set[str]] = {label: set() for label in EMAIL_SOURCE_LABELS}
    for r in raw_rows:
        key = (r["email"] or "").strip().lower()
        if not key:
            continue
        by_source.setdefault(r["source"], set()).add(key)
    return {k: len(v) for k, v in by_source.items()}


__all__ = [
    'create_enquiry',
    'list_enquiries',
    'get_enquiry_by_id',
    'mark_enquiry_read',
    'count_unread_enquiries',
    'create_feedback',
    'list_feedback',
    'update_feedback_status',
    'count_feedback_by_status',
    'record_analytics_event',
    'get_analytics_prerelease',
    'get_analytics_users',
    'get_analytics_revenue',
    'get_analytics_features',
    'insert_audit_log',
    'query_audit_log',
    'export_audit_log_csv',
    'create_impersonation_session',
    'get_impersonation_session_by_token',
    'get_impersonation_session',
    'end_impersonation_session',
    'record_impersonation_action',
    'list_impersonation_sessions',
    'list_impersonation_actions',
    'list_feature_flags',
    'get_feature_flag',
    'create_feature_flag',
    'update_feature_flag',
    'delete_feature_flag',
    'record_feature_flag_event',
    'list_email_templates',
    'get_email_template',
    'upsert_email_template',
    'delete_email_template',
    'aggregate_email_addresses',
    'count_email_addresses_by_source',
    'EMAIL_SOURCE_LABELS',
]

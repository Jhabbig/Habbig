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


def create_impersonation_session(
    *,
    admin_user_id: int,
    target_user_id: int,
    reason: str,
    ip_address=None,
    user_agent=None,
) -> dict:
    """Create an impersonation session, returning {id, cookie_token, started_at}.

    The cookie_token is set on the admin's browser; every request that
    presents it is treated as the admin viewing the target user.
    """
    token = secrets.token_urlsafe(48)
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO impersonation_sessions "
            "(admin_user_id, target_user_id, cookie_token, reason, ip_address, user_agent, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (admin_user_id, target_user_id, token, reason, ip_address, user_agent, now),
        )
        return {"id": cur.lastrowid, "cookie_token": token, "started_at": now}


def get_impersonation_session_by_token(token: str):
    """Look up by cookie token. Does NOT filter on ended_at so callers decide."""
    if not token:
        return None
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM impersonation_sessions WHERE cookie_token = ?",
            (token,),
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


def list_feature_flags():
    with db.conn() as c:
        return c.execute("SELECT * FROM feature_flags ORDER BY key ASC").fetchall()


def get_feature_flag(key: str):
    with db.conn() as c:
        return c.execute("SELECT * FROM feature_flags WHERE key = ?", (key,)).fetchone()


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
) -> int:
    import json as _json
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO feature_flags "
            "(key, name, description, enabled_globally, enabled_for_tiers, "
            " enabled_for_user_ids, disabled_for_user_ids, rollout_percentage, "
            " created_at, updated_at, updated_by_admin_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key, name, description,
                1 if enabled_globally else 0,
                _json.dumps(enabled_for_tiers or []),
                _json.dumps(enabled_for_user_ids or []),
                _json.dumps(disabled_for_user_ids or []),
                max(0, min(100, int(rollout_percentage))),
                now, now, updated_by_admin_id,
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
) -> bool:
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
    params.append(key)
    with db.conn() as c:
        cur = c.execute(
            f"UPDATE feature_flags SET {', '.join(fields)} WHERE key = ?",
            tuple(params),
        )
        return cur.rowcount > 0


def delete_feature_flag(key: str) -> bool:
    with db.conn() as c:
        cur = c.execute("DELETE FROM feature_flags WHERE key = ?", (key,))
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
]

"""Audit-log search/stats/export — extracted from queries/admin.py so the
admin audit-log page can express richer filters (admin email, target user
id, multi-action) and a cursor-paginated read pattern without bloating
the original `query_audit_log` signature.

The legacy `insert_audit_log`, `query_audit_log`, and `export_audit_log_csv`
in queries/admin.py are still used by callers that pre-date this polish
pass (security/audit.py writes via `insert_audit_log`, the old paginated
query is kept around for back-compat with any monitoring scripts that
import it from `db`). The functions here are the v2 surface used by
`/admin/audit-log` and `/admin/audit-log/export.csv` after the
`feature/platform-build` polish in May 2026.

All filter helpers accept a single `filters` dict so the same dict can be
threaded from query-params → search → stats → CSV without re-binding
positional arguments at every layer.
"""

from __future__ import annotations

import csv as _csv
import io as _io
import sqlite3
import time
from typing import Iterable, Iterator, Optional

import db


# ── Filter normalisation ────────────────────────────────────────────────────


def _normalise_filters(filters: Optional[dict]) -> dict:
    """Return a sanitised copy. Only known keys survive — anything else is
    dropped before reaching SQL so a stray query param can never inject.

    Keys honoured:
      action:         str or list[str]   (action = … OR action IN (…))
      admin_user_id:  int
      admin_email:    str  (case-insensitive substring match)
      target_type:    str  (exact)
      target_user_id: str  (matches target_id when target_type='user')
      from_ts:        int (unix seconds, inclusive)
      to_ts:          int (unix seconds, inclusive)
    """
    if not filters:
        return {}
    out: dict = {}

    action = filters.get("action")
    if isinstance(action, str) and action.strip():
        out["action"] = action.strip()
    elif isinstance(action, (list, tuple)):
        cleaned = [a.strip() for a in action if isinstance(a, str) and a.strip()]
        if cleaned:
            out["action"] = cleaned

    admin_id = filters.get("admin_user_id")
    if admin_id is not None:
        try:
            out["admin_user_id"] = int(admin_id)
        except (TypeError, ValueError):
            pass

    admin_email = filters.get("admin_email")
    if isinstance(admin_email, str) and admin_email.strip():
        out["admin_email"] = admin_email.strip().lower()

    target_type = filters.get("target_type")
    if isinstance(target_type, str) and target_type.strip():
        out["target_type"] = target_type.strip()

    target_user_id = filters.get("target_user_id")
    if target_user_id is not None and str(target_user_id).strip():
        out["target_user_id"] = str(target_user_id).strip()

    for k in ("from_ts", "to_ts"):
        v = filters.get(k)
        if v is not None:
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                pass

    return out


def _build_where(filters: dict) -> tuple[str, list]:
    """Translate the normalised filter dict into a parameterised WHERE
    fragment. Returns (where_sql, params).
    """
    where: list[str] = []
    params: list = []

    action = filters.get("action")
    if isinstance(action, list):
        # IN clause; placeholders generated dynamically — safe because
        # the count comes from len(action), not user input.
        marks = ",".join("?" * len(action))
        where.append(f"action IN ({marks})")
        params.extend(action)
    elif isinstance(action, str):
        where.append("action = ?")
        params.append(action)

    if "admin_user_id" in filters:
        where.append("admin_user_id = ?")
        params.append(filters["admin_user_id"])

    if "admin_email" in filters:
        # Substring match against lower(admin_email) so an admin
        # autocomplete chip or a free-text "@narve.ai" query both work.
        where.append("LOWER(COALESCE(admin_email, '')) LIKE ?")
        params.append(f"%{filters['admin_email']}%")

    if "target_type" in filters:
        where.append("target_type = ?")
        params.append(filters["target_type"])

    if "target_user_id" in filters:
        # User-id targets are stored as TEXT in audit_log.target_id.
        # Match exact id — the admin pastes a number, so accept the
        # numeric string match across any target_type ('user', 'session', …).
        where.append("target_id = ?")
        params.append(filters["target_user_id"])

    if "from_ts" in filters:
        where.append("timestamp >= ?")
        params.append(filters["from_ts"])

    if "to_ts" in filters:
        where.append("timestamp <= ?")
        params.append(filters["to_ts"])

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    return where_sql, params


# ── Cursor-paginated search ─────────────────────────────────────────────────


def search_audit_log(
    filters: Optional[dict] = None,
    *,
    limit: int = 50,
    before_id: Optional[int] = None,
) -> tuple[list[sqlite3.Row], Optional[int], int]:
    """Search the audit log with the supplied filters.

    Returns (rows, next_cursor, total_count).
      - rows: up to `limit` audit_log rows ordered by id DESC (newest first).
        Ordering on id matches timestamp DESC because audit_log id is an
        autoincrement int allocated under the insert lock — strictly
        monotonic with insert time.
      - next_cursor: id of the last row, suitable for the next
        `before_id` query, or None if this is the last page.
      - total_count: total matching rows across all pages (used for the
        "N events" stat at the top).
    """
    f = _normalise_filters(filters)
    where_sql, params = _build_where(f)
    capped = max(1, min(int(limit), 200))

    with db.conn() as c:
        total_row = c.execute(
            f"SELECT COUNT(*) AS n FROM audit_log{where_sql}", tuple(params)
        ).fetchone()
        total = int(total_row["n"] if total_row else 0)

        cur_sql = where_sql
        cur_params = list(params)
        if before_id is not None:
            cur_sql = (
                where_sql + (" AND id < ?" if where_sql else " WHERE id < ?")
            )
            cur_params.append(int(before_id))

        rows = c.execute(
            f"SELECT * FROM audit_log{cur_sql} ORDER BY id DESC LIMIT ?",
            tuple(cur_params) + (capped,),
        ).fetchall()

    next_cursor = int(rows[-1]["id"]) if len(rows) == capped else None
    return rows, next_cursor, total


# ── Stats card ──────────────────────────────────────────────────────────────


def get_audit_stats(filters: Optional[dict] = None) -> dict:
    """Aggregate stats for the filtered range.

    Returns:
      {
        "total":         int,
        "top_actions":   [(action, count), ...]   # up to 3
        "top_admins":    [(admin_email, count), ...]  # up to 3
        "suspicious":    list[dict]               # flags worth surfacing
      }
    """
    f = _normalise_filters(filters)
    where_sql, params = _build_where(f)

    with db.conn() as c:
        total = int(c.execute(
            f"SELECT COUNT(*) AS n FROM audit_log{where_sql}",
            tuple(params),
        ).fetchone()["n"])

        top_actions = c.execute(
            f"SELECT action, COUNT(*) AS n FROM audit_log{where_sql} "
            "GROUP BY action ORDER BY n DESC LIMIT 3",
            tuple(params),
        ).fetchall()

        # Build a top_admins query that adds "admin_email IS NOT NULL" to
        # whatever filter the caller supplied so unknown-actor rows don't
        # rank into the leaderboard.
        admin_where = where_sql + (
            " AND admin_email IS NOT NULL AND admin_email <> ''"
            if where_sql else
            " WHERE admin_email IS NOT NULL AND admin_email <> ''"
        )
        top_admins = c.execute(
            f"SELECT admin_email, COUNT(*) AS n FROM audit_log{admin_where} "
            "GROUP BY admin_email ORDER BY n DESC LIMIT 3",
            tuple(params),
        ).fetchall()

    return {
        "total": total,
        "top_actions": [(r["action"], int(r["n"])) for r in top_actions],
        "top_admins": [(r["admin_email"], int(r["n"])) for r in top_admins],
        "suspicious": detect_suspicious_patterns(f),
    }


# ── Suspicious-pattern detection ────────────────────────────────────────────


# Threshold per (action, window_seconds) — tripping any flag surfaces a
# monochrome warning on the stats card.
_SUSPICIOUS_RULES = (
    # Forensic email-watermark traces: more than 5 in an hour by any single
    # admin is a strong signal of someone shotgunning leak attribution.
    ("email.watermark_trace", 3600, 5,
     "Watermark traces per admin in 1h"),
    # Bulk role changes — promoting/demoting more than 3 admins in an hour
    # by any single actor warrants a second look.
    ("user.role_change", 3600, 3,
     "Role changes per admin in 1h"),
    ("user.promote_admin", 3600, 3,
     "Admin promotions per admin in 1h"),
    # User-suspension storms — more than 10 in an hour by one admin.
    ("user.suspend", 3600, 10,
     "User suspensions per admin in 1h"),
    # User deletions — only one per admin per hour should be normal.
    ("user.delete_initiated", 3600, 3,
     "Deletion initiations per admin in 1h"),
)


def detect_suspicious_patterns(filters: Optional[dict] = None) -> list[dict]:
    """Return a list of `{action, admin_email, count, window_seconds, label}`
    dicts for any (action, admin) pairs that breached a hardcoded threshold
    within the filtered range. Empty list if no pattern fires.

    Always honours the supplied date range so an admin filtering "last 30
    days" doesn't get an alert from yesterday's normal usage.
    """
    f = _normalise_filters(filters)
    base_where, base_params = _build_where(f)
    flags: list[dict] = []

    with db.conn() as c:
        for action, window_s, threshold, label in _SUSPICIOUS_RULES:
            # Add `action = ?` on top of any caller-supplied filters so the
            # rule only scans rows for the specific action it cares about.
            rule_where = base_where + (" AND action = ?" if base_where else " WHERE action = ?")
            rule_params = list(base_params) + [action]

            # Bucket rows into floor(timestamp / window_s) per admin, then
            # surface any (admin, bucket) cell that crossed the threshold.
            # The bucket arithmetic is a single SQL expression — SQLite
            # plans this against idx_audit_action.
            rows = c.execute(
                "SELECT admin_email, "
                "       (timestamp / ?) AS bucket, "
                "       COUNT(*) AS n "
                f"FROM audit_log{rule_where} "
                "GROUP BY admin_email, bucket "
                "HAVING n >= ? "
                "ORDER BY n DESC LIMIT 3",
                tuple([int(window_s)] + rule_params + [int(threshold)]),
            ).fetchall()
            for r in rows:
                flags.append({
                    "action": action,
                    "admin_email": r["admin_email"] or "(unknown)",
                    "count": int(r["n"]),
                    "window_seconds": int(window_s),
                    "threshold": int(threshold),
                    "label": label,
                })
    return flags


# ── Admin email list (filter autocomplete) ─────────────────────────────────


def list_audit_admin_emails(limit: int = 50) -> list[str]:
    """Distinct admin_email values seen in audit_log, newest activity first.

    Used to populate the admin-email datalist on the filter form so the
    admin can pick from a list of known actors rather than typing.
    """
    capped = max(1, min(int(limit), 500))
    with db.conn() as c:
        rows = c.execute(
            "SELECT admin_email, MAX(timestamp) AS last_seen "
            "FROM audit_log "
            "WHERE admin_email IS NOT NULL AND admin_email <> '' "
            "GROUP BY admin_email "
            "ORDER BY last_seen DESC LIMIT ?",
            (capped,),
        ).fetchall()
    return [r["admin_email"] for r in rows]


# ── Streaming CSV export ───────────────────────────────────────────────────


_CSV_COLUMNS = (
    "timestamp_iso", "admin_user_id", "admin_email", "action",
    "target_type", "target_id", "target_description",
    "ip_address", "user_agent", "request_id", "notes",
    "before_state", "after_state",
)


def export_audit_csv_stream(filters: Optional[dict] = None) -> Iterator[str]:
    """Yield CSV rows as strings, one row at a time, for a StreamingResponse.

    We pull rows in chunks of 500 from SQLite (well under any reasonable
    memory ceiling) and yield CSV-formatted rows. Header is yielded first.
    Caller wraps in `StreamingResponse(export_audit_csv_stream(...))`.
    """
    f = _normalise_filters(filters)
    where_sql, params = _build_where(f)

    buf = _io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow(_CSV_COLUMNS)
    yield buf.getvalue()

    CHUNK = 500
    offset = 0
    while True:
        with db.conn() as c:
            rows = c.execute(
                f"SELECT * FROM audit_log{where_sql} "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                tuple(params) + (CHUNK, offset),
            ).fetchall()
        if not rows:
            return
        buf.seek(0)
        buf.truncate()
        for r in rows:
            writer.writerow([
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
        yield buf.getvalue()
        if len(rows) < CHUNK:
            return
        offset += CHUNK


__all__ = [
    "search_audit_log",
    "get_audit_stats",
    "detect_suspicious_patterns",
    "list_audit_admin_emails",
    "export_audit_csv_stream",
]

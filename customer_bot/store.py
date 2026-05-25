"""SQLite layer for leads.

Writes to the same `gateway/auth.db` so the admin UI in `gateway/server.py`
can read leads without a second database connection. The `leads` table
itself is declared in `gateway/db.py` SCHEMA.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent / "gateway" / "auth.db"

VALID_OUTCOMES = ("replied", "signed_up", "no_reply", "")


def _connect() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 5000")
    return c


@contextmanager
def _conn():
    c = _connect()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def upsert_lead(
    *,
    source: str,
    source_id: str,
    url: str,
    author: str,
    title: str,
    snippet: str,
    dashboard_key: str,
    score: int,
    draft: str,
    posted_at: int,
    ref_code: str = "",
) -> bool:
    """Insert a lead if (source, source_id) is new. Returns True if inserted."""
    now = int(time.time())
    with _conn() as c:
        try:
            c.execute(
                """
                INSERT INTO leads
                    (source, source_id, url, author, title, snippet,
                     dashboard_key, score, draft, status, posted_at,
                     discovered_at, updated_at, ref_code)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?)
                """,
                (source, source_id, url, author, title, snippet,
                 dashboard_key, score, draft, posted_at, now, now, ref_code),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def list_leads(
    status: Optional[str] = "new",
    *,
    source: Optional[str] = None,
    dashboard_key: Optional[str] = None,
    limit: int = 300,
) -> list[sqlite3.Row]:
    """List leads, optionally filtered by status / source / dashboard.

    Pass `status=None` to ignore status filtering (still excludes archived).
    """
    where: list[str] = []
    params: list[object] = []
    if status is not None:
        where.append("status = ?")
        params.append(status)
    else:
        where.append("status != 'archived'")
    if source:
        where.append("source = ?")
        params.append(source)
    if dashboard_key:
        where.append("dashboard_key = ?")
        params.append(dashboard_key)
    sql = (
        "SELECT * FROM leads WHERE " + " AND ".join(where) +
        " ORDER BY score DESC, posted_at DESC LIMIT ?"
    )
    params.append(limit)
    with _conn() as c:
        return list(c.execute(sql, params).fetchall())


def counts_by_status() -> dict[str, int]:
    with _conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM leads GROUP BY status"
        ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def conversion_stats() -> dict[str, int]:
    """Counts of each outcome among contacted leads — for the dashboard
    header so we can see if outreach is actually working."""
    with _conn() as c:
        rows = c.execute(
            "SELECT outcome, COUNT(*) AS n FROM leads WHERE outcome != '' GROUP BY outcome"
        ).fetchall()
    return {r["outcome"]: r["n"] for r in rows}


def signed_up_lift() -> dict[tuple[str, str], int]:
    """Per-(source, dashboard) historical lift to apply to fresh leads.

    Returns a -10..+10 nudge for each combination that has enough history
    to mean anything (n >= 5 leads with recorded outcomes). Patterns that
    convert above 20% get +10, above 10% get +5; patterns where >50% of
    outcomes were no_reply get -10. Sparse history → 0 (no change).

    This is the self-tuning bit: once you've marked a few outcomes, the
    bot quietly upranks leads from the corner of the world that's actually
    paid off, and downranks the corner that hasn't.
    """
    out: dict[tuple[str, str], int] = {}
    with _conn() as c:
        rows = c.execute(
            """
            SELECT source, dashboard_key,
                   SUM(CASE WHEN outcome = 'signed_up' THEN 1 ELSE 0 END) AS signed,
                   SUM(CASE WHEN outcome = 'no_reply'  THEN 1 ELSE 0 END) AS noreply,
                   SUM(CASE WHEN outcome != '' THEN 1 ELSE 0 END)         AS total
            FROM leads
            GROUP BY source, dashboard_key
            HAVING total >= 5
            """
        ).fetchall()
    for r in rows:
        n = r["total"] or 0
        if n < 5:
            continue
        signed_rate = (r["signed"] or 0) / n
        noreply_rate = (r["noreply"] or 0) / n
        if signed_rate >= 0.20:
            lift = 10
        elif signed_rate >= 0.10:
            lift = 5
        elif noreply_rate >= 0.50:
            lift = -10
        else:
            lift = 0
        out[(r["source"], r["dashboard_key"])] = lift
    return out


def bulk_set_status(lead_ids: list[int], status: str, note: str = "") -> int:
    """Apply set_status to many leads in one transaction. Returns rowcount."""
    if not lead_ids:
        return 0
    now = int(time.time())
    placeholders = ",".join("?" * len(lead_ids))
    with _conn() as c:
        cur = c.execute(
            f"UPDATE leads SET status = ?, status_note = ?, updated_at = ? WHERE id IN ({placeholders})",
            [status, note, now, *lead_ids],
        )
        return cur.rowcount


def set_status(lead_id: int, status: str, note: str = "") -> bool:
    now = int(time.time())
    with _conn() as c:
        cur = c.execute(
            "UPDATE leads SET status = ?, status_note = ?, updated_at = ? WHERE id = ?",
            (status, note, now, lead_id),
        )
        return cur.rowcount > 0


def set_outcome(lead_id: int, outcome: str) -> bool:
    if outcome not in VALID_OUTCOMES:
        return False
    now = int(time.time())
    with _conn() as c:
        cur = c.execute(
            "UPDATE leads SET outcome = ?, outcome_at = ?, updated_at = ? WHERE id = ?",
            (outcome, now if outcome else None, now, lead_id),
        )
        return cur.rowcount > 0


def snooze(lead_id: int, until_ts: int) -> bool:
    now = int(time.time())
    with _conn() as c:
        cur = c.execute(
            "UPDATE leads SET status = 'snoozed', snoozed_until = ?, updated_at = ? WHERE id = ?",
            (until_ts, now, lead_id),
        )
        return cur.rowcount > 0


def unsnooze_expired() -> int:
    """Move snoozed leads whose timer expired back to 'new'."""
    now = int(time.time())
    with _conn() as c:
        cur = c.execute(
            """
            UPDATE leads
            SET status = 'new', snoozed_until = NULL, updated_at = ?
            WHERE status = 'snoozed' AND snoozed_until IS NOT NULL AND snoozed_until <= ?
            """,
            (now, now),
        )
        return cur.rowcount


def archive_stale_new(days: int = 21) -> int:
    """Auto-archive 'new' leads that have sat untouched past `days`.

    Keeps the panel scannable. We never archive contacted/snoozed leads,
    only stale 'new' ones the user clearly isn't going to action.
    """
    now = int(time.time())
    cutoff = now - days * 86400
    with _conn() as c:
        cur = c.execute(
            """
            UPDATE leads
            SET status = 'archived', archived_at = ?, updated_at = ?
            WHERE status = 'new' AND discovered_at < ?
            """,
            (now, now, cutoff),
        )
        return cur.rowcount


def get_lead(lead_id: int) -> Optional[sqlite3.Row]:
    with _conn() as c:
        return c.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()

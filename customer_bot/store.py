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
from typing import Iterable, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "gateway" / "auth.db"


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
                     discovered_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)
                """,
                (source, source_id, url, author, title, snippet,
                 dashboard_key, score, draft, posted_at, now, now),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def list_leads(status: str = "new", limit: int = 200) -> list[sqlite3.Row]:
    with _conn() as c:
        rows = c.execute(
            """
            SELECT * FROM leads
            WHERE status = ?
            ORDER BY score DESC, posted_at DESC
            LIMIT ?
            """,
            (status, limit),
        ).fetchall()
    return list(rows)


def counts_by_status() -> dict[str, int]:
    with _conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM leads GROUP BY status"
        ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def set_status(lead_id: int, status: str, note: str = "") -> bool:
    now = int(time.time())
    with _conn() as c:
        cur = c.execute(
            "UPDATE leads SET status = ?, status_note = ?, updated_at = ? WHERE id = ?",
            (status, note, now, lead_id),
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
    """Move snoozed leads whose timer expired back to 'new'. Returns rowcount."""
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


def get_lead(lead_id: int) -> Optional[sqlite3.Row]:
    with _conn() as c:
        return c.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()

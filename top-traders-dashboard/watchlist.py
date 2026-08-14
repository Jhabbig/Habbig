#!/usr/bin/env python3
"""
Watchlist + alert inbox.

Two tables, both keyed on (user_id, …):

  watchlist(user_id, actor_id, label, created_at, UNIQUE(user_id, actor_id))
    - User says "tell me when actor X (Pelosi, a wallet, a CIK) does anything"
    - actor_id matches the same string we put in insider_events.actor_id
      (e.g. 'house:pelosi-nancy', 'cik:0001213900', '0xabc…').

  alert_inbox(id, user_id, event_id, watched_actor_id, created_at, read_at)
    - One row per (user, matching event). Created by `process_new_events()`,
      consumed by the dashboard via `list_inbox(user_id)`.

The poller in server.py calls `process_new_events()` every minute. It
walks insider_events rows newer than the last processed timestamp,
joins against watchlist, and inserts inbox rows. Idempotent via
UNIQUE(user_id, event_id).

Identity model: the gateway already injects an `x-user-id` header (see
`_kalshi_user_id` in server.py). DEV_MODE collapses to user 'default'.
We deliberately don't store any PII here — just the opaque user_id
the gateway hands us.

Why no email/SMS push? Email setup varies per user; the dashboard polls
the inbox endpoint and badges the UI. If/when the gateway gains email
support, hook it into `_dispatch_external` below.
"""

from __future__ import annotations

import logging
import os
import smtplib
import sqlite3
import time
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "watchlist.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    actor_id    TEXT NOT NULL,
    label       TEXT,                       -- friendly name for UI
    created_at  INTEGER NOT NULL,
    UNIQUE(user_id, actor_id)
);
CREATE INDEX IF NOT EXISTS idx_watch_user  ON watchlist(user_id);
CREATE INDEX IF NOT EXISTS idx_watch_actor ON watchlist(actor_id);

CREATE TABLE IF NOT EXISTS alert_inbox (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           TEXT NOT NULL,
    event_id          INTEGER NOT NULL,
    watched_actor_id  TEXT NOT NULL,
    created_at        INTEGER NOT NULL,
    read_at           INTEGER,
    UNIQUE(user_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_inbox_user_unread
    ON alert_inbox(user_id, read_at);

CREATE TABLE IF NOT EXISTS alert_processor_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    last_event_id_seen  INTEGER NOT NULL DEFAULT 0,
    updated_at          INTEGER NOT NULL
);
INSERT OR IGNORE INTO alert_processor_state (id, last_event_id_seen, updated_at)
    VALUES (1, 0, strftime('%s','now'));
"""


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)


# ─── Watchlist CRUD ───────────────────────────────────────────────────

def add_watch(user_id: str, actor_id: str, label: str | None = None) -> bool:
    """Insert a watch. Returns True if newly added, False if it already existed."""
    if not user_id or not actor_id:
        raise ValueError("user_id and actor_id required")
    init_db()
    now = int(time.time())
    with _conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO watchlist (user_id, actor_id, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, actor_id, (label or "").strip() or None, now),
        )
        return cur.rowcount > 0


def remove_watch(user_id: str, actor_id: str) -> bool:
    init_db()
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM watchlist WHERE user_id = ? AND actor_id = ?",
            (user_id, actor_id),
        )
        return cur.rowcount > 0


def list_watches(user_id: str) -> list[dict]:
    init_db()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM watchlist WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def watched_actor_ids(user_id: str) -> set[str]:
    return {w["actor_id"] for w in list_watches(user_id)}


# ─── Inbox reads ──────────────────────────────────────────────────────

def list_inbox(
    user_id: str,
    *,
    unread_only: bool = False,
    limit: int = 100,
) -> list[dict]:
    """
    Returns inbox entries joined with the parent insider_events row.
    Cross-DB: inbox lives in watchlist.db, events in insider_events.db,
    so we ATTACH and join in one shot.
    """
    init_db()
    events_db = str(Path(__file__).parent / "insider_events.db")
    sql = """
        SELECT
            i.id           AS inbox_id,
            i.event_id     AS event_id,
            i.watched_actor_id,
            i.created_at   AS alerted_at,
            i.read_at,
            e.venue, e.actor_id, e.actor_label, e.actor_role,
            e.symbol, e.symbol_name, e.side,
            e.shares, e.price, e.size_usd_low, e.size_usd_high,
            e.ts_filed, e.ts_executed, e.raw_url
        FROM alert_inbox i
        JOIN ev.insider_events e ON e.id = i.event_id
        WHERE i.user_id = ?
    """
    params: list = [user_id]
    if unread_only:
        sql += " AND i.read_at IS NULL"
    sql += " ORDER BY i.created_at DESC LIMIT ?"
    params.append(limit)

    with _conn() as c:
        c.execute(f"ATTACH DATABASE ? AS ev", (events_db,))
        try:
            rows = c.execute(sql, params).fetchall()
        finally:
            c.execute("DETACH DATABASE ev")
    return [dict(r) for r in rows]


def mark_read(user_id: str, inbox_ids: Iterable[int]) -> int:
    ids = [int(i) for i in inbox_ids if i is not None]
    if not ids:
        return 0
    init_db()
    placeholders = ",".join("?" for _ in ids)
    now = int(time.time())
    with _conn() as c:
        cur = c.execute(
            f"UPDATE alert_inbox SET read_at = ? "
            f"WHERE user_id = ? AND id IN ({placeholders}) AND read_at IS NULL",
            [now, user_id, *ids],
        )
        return cur.rowcount


def mark_all_read(user_id: str) -> int:
    init_db()
    now = int(time.time())
    with _conn() as c:
        cur = c.execute(
            "UPDATE alert_inbox SET read_at = ? "
            "WHERE user_id = ? AND read_at IS NULL",
            (now, user_id),
        )
        return cur.rowcount


def unread_count(user_id: str) -> int:
    init_db()
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM alert_inbox "
            "WHERE user_id = ? AND read_at IS NULL",
            (user_id,),
        ).fetchone()
    return int(row["n"]) if row else 0


# ─── Background alert processor ───────────────────────────────────────

def _get_cursor() -> int:
    with _conn() as c:
        row = c.execute(
            "SELECT last_event_id_seen FROM alert_processor_state WHERE id = 1"
        ).fetchone()
    return int(row["last_event_id_seen"]) if row else 0


def _set_cursor(event_id: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE alert_processor_state SET last_event_id_seen = ?, updated_at = ? "
            "WHERE id = 1",
            (event_id, int(time.time())),
        )


def _all_watched_actors() -> dict[str, list[str]]:
    """{actor_id: [user_id, ...]} for fast fan-out."""
    init_db()
    out: dict[str, list[str]] = {}
    with _conn() as c:
        rows = c.execute("SELECT user_id, actor_id FROM watchlist").fetchall()
    for r in rows:
        out.setdefault(r["actor_id"], []).append(r["user_id"])
    return out


def _new_events_since(last_id: int, limit: int = 1000) -> list[dict]:
    """Pull new insider_events with id > last_id. Cross-DB read."""
    events_db = str(Path(__file__).parent / "insider_events.db")
    init_db()
    with _conn() as c:
        c.execute("ATTACH DATABASE ? AS ev", (events_db,))
        try:
            rows = c.execute(
                "SELECT id, actor_id, symbol, symbol_name, ts_filed, side "
                "FROM ev.insider_events WHERE id > ? AND actor_id IS NOT NULL "
                "ORDER BY id ASC LIMIT ?",
                (last_id, limit),
            ).fetchall()
        finally:
            c.execute("DETACH DATABASE ev")
    return [dict(r) for r in rows]


def process_new_events(*, dispatch: bool = True) -> dict:
    """
    Walk new insider_events rows, fan out to watchers' inboxes.

    Optionally dispatches an external notification (SMTP) for each new
    inbox row, controlled by env vars (see _dispatch_external). The
    dispatch is best-effort and never blocks inbox writes.
    """
    init_db()
    cursor = _get_cursor()
    events = _new_events_since(cursor, limit=2000)
    if not events:
        return {"events_scanned": 0, "alerts_created": 0, "dispatched": 0, "cursor": cursor}

    watched = _all_watched_actors()
    if not watched:
        # Nobody is watching anything — advance cursor and bail.
        _set_cursor(events[-1]["id"])
        return {
            "events_scanned": len(events),
            "alerts_created": 0,
            "dispatched": 0,
            "cursor": events[-1]["id"],
        }

    new_inbox_rows: list[tuple[int, str, int, str, int]] = []
    now = int(time.time())
    for ev in events:
        actor = ev.get("actor_id")
        if not actor or actor not in watched:
            continue
        for user_id in watched[actor]:
            new_inbox_rows.append((0, user_id, int(ev["id"]), actor, now))

    inserted = 0
    inserted_for_dispatch: list[tuple[str, dict]] = []
    if new_inbox_rows:
        with _conn() as c:
            for _, user_id, event_id, actor, ts in new_inbox_rows:
                cur = c.execute(
                    "INSERT OR IGNORE INTO alert_inbox "
                    "(user_id, event_id, watched_actor_id, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (user_id, event_id, actor, ts),
                )
                if cur.rowcount > 0:
                    inserted += 1
                    # Look up the event for richer dispatch payload
                    ev_match = next((e for e in events if e["id"] == event_id), None)
                    if ev_match:
                        inserted_for_dispatch.append((user_id, ev_match))

    last_id = events[-1]["id"]
    _set_cursor(last_id)

    dispatched = 0
    if dispatch and inserted_for_dispatch:
        for user_id, ev in inserted_for_dispatch:
            try:
                if _dispatch_external(user_id, ev):
                    dispatched += 1
            except Exception as e:
                logger.warning("alert dispatch failed for %s/%s: %s",
                               user_id, ev.get("id"), e)

    return {
        "events_scanned": len(events),
        "alerts_created": inserted,
        "dispatched": dispatched,
        "cursor": last_id,
    }


# ─── External dispatch (SMTP) ─────────────────────────────────────────
#
# Env vars (all optional — if any are missing, dispatch silently no-ops):
#   ALERTS_SMTP_HOST, ALERTS_SMTP_PORT (default 587),
#   ALERTS_SMTP_USER, ALERTS_SMTP_PASS,
#   ALERTS_FROM_ADDR
#   ALERTS_USER_<USERID>=email@example.com   (per-user destination map)

def _dispatch_available() -> bool:
    return all(os.environ.get(k) for k in (
        "ALERTS_SMTP_HOST", "ALERTS_SMTP_USER", "ALERTS_SMTP_PASS", "ALERTS_FROM_ADDR",
    ))


def _user_email(user_id: str) -> str | None:
    # Per-user mapping via env var, e.g. ALERTS_USER_default=me@example.com
    safe = (user_id or "").replace("-", "_").replace(".", "_")
    return os.environ.get(f"ALERTS_USER_{safe}")


def _dispatch_external(user_id: str, event: dict) -> bool:
    """Best-effort SMTP send. Returns True if a message left the box."""
    # Respect the ALERT_MODE switch — when set to 'digest' (the default),
    # per-event SMTP is suppressed and users only receive the daily roll-up.
    # Set ALERT_MODE=per_event or ALERT_MODE=both to re-enable instant pings.
    mode = (os.environ.get("ALERT_MODE", "digest") or "digest").strip().lower()
    if mode == "digest":
        return False
    if not _dispatch_available():
        return False
    to_addr = _user_email(user_id)
    if not to_addr:
        return False

    actor = event.get("actor_id") or "actor"
    sym = event.get("symbol") or "—"
    side = event.get("side") or "?"
    name = event.get("symbol_name") or ""
    subj = f"[Insider alert] {actor} → {side.upper()} {sym}"
    body = (
        f"Watched actor {actor} just appeared on a new insider event:\n\n"
        f"  Symbol : {sym}  ({name})\n"
        f"  Side   : {side}\n"
        f"  Filed  : {event.get('ts_filed')}\n"
        f"  Event#: {event.get('id')}\n\n"
        f"Open the dashboard inbox to review."
    )

    msg = EmailMessage()
    msg["From"] = os.environ["ALERTS_FROM_ADDR"]
    msg["To"] = to_addr
    msg["Subject"] = subj
    msg.set_content(body)

    host = os.environ["ALERTS_SMTP_HOST"]
    port = int(os.environ.get("ALERTS_SMTP_PORT", "587"))
    user = os.environ["ALERTS_SMTP_USER"]
    pwd = os.environ["ALERTS_SMTP_PASS"]
    try:
        with smtplib.SMTP(host, port, timeout=10) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
        return True
    except Exception as e:
        logger.warning("SMTP send to %s failed: %s", to_addr, e)
        return False


# ─── Status ───────────────────────────────────────────────────────────

def status_summary(user_id: str | None = None) -> dict:
    init_db()
    with _conn() as c:
        total_watches = c.execute("SELECT COUNT(*) AS n FROM watchlist").fetchone()["n"]
        total_alerts = c.execute("SELECT COUNT(*) AS n FROM alert_inbox").fetchone()["n"]
        cursor = c.execute(
            "SELECT last_event_id_seen FROM alert_processor_state WHERE id = 1"
        ).fetchone()["last_event_id_seen"]
        my = None
        if user_id:
            my = {
                "watches": c.execute(
                    "SELECT COUNT(*) AS n FROM watchlist WHERE user_id = ?",
                    (user_id,),
                ).fetchone()["n"],
                "unread": c.execute(
                    "SELECT COUNT(*) AS n FROM alert_inbox "
                    "WHERE user_id = ? AND read_at IS NULL", (user_id,),
                ).fetchone()["n"],
            }
    return {
        "total_watches": total_watches,
        "total_alerts": total_alerts,
        "cursor_event_id": cursor,
        "smtp_dispatch_available": _dispatch_available(),
        "you": my,
    }


if __name__ == "__main__":
    import json
    init_db()
    print(json.dumps(status_summary(), indent=2))

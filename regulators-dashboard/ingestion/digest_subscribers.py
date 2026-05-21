"""SQLite subscriber store for v1.6 — managed email digest.

Schema:

    subscribers(
        id                INTEGER PRIMARY KEY,
        email             TEXT NOT NULL,
        filter_json       TEXT NOT NULL,
        status            TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'confirmed' | 'unsubscribed'
        confirm_token     TEXT NOT NULL UNIQUE,
        unsubscribe_token TEXT NOT NULL UNIQUE,
        created_at        TEXT NOT NULL,
        confirmed_at      TEXT,
        unsubscribed_at   TEXT,
        last_sent_at      TEXT
    )

Double opt-in: a fresh row lands in `status='pending'`. The signup
endpoint sends a confirmation email; clicking the link flips status
to `'confirmed'`. Only confirmed rows receive digests. Unsubscribe
keeps the row (audit trail) and flips status to `'unsubscribed'`.

Path is configurable via DIGEST_DB_PATH. Defaults to a tempfile path
that loses across container restart (honest dev default). Production
Docker should mount a persistent volume there.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get(
    "DIGEST_DB_PATH",
    os.path.join(tempfile.gettempdir(), "regulators-digest.sqlite"),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_schema() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                email             TEXT NOT NULL,
                filter_json       TEXT NOT NULL,
                status            TEXT NOT NULL DEFAULT 'pending',
                confirm_token     TEXT NOT NULL UNIQUE,
                unsubscribe_token TEXT NOT NULL UNIQUE,
                created_at        TEXT NOT NULL,
                confirmed_at      TEXT,
                unsubscribed_at   TEXT,
                last_sent_at      TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_email ON subscribers(email)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_status ON subscribers(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_confirm_token ON subscribers(confirm_token)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_unsubscribe_token ON subscribers(unsubscribe_token)")


def add_pending(email: str, filter_dict: dict) -> dict:
    """Insert a new pending row. Returns
    {id, email, confirm_token, unsubscribe_token}."""
    init_schema()
    confirm_token = secrets.token_urlsafe(32)
    unsubscribe_token = secrets.token_urlsafe(32)
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO subscribers (email, filter_json, status, confirm_token, "
            "unsubscribe_token, created_at) VALUES (?, ?, 'pending', ?, ?, ?)",
            (email.strip().lower(), json.dumps(filter_dict, sort_keys=True),
             confirm_token, unsubscribe_token, _now_iso()),
        )
        sub_id = cur.lastrowid
    return {
        "id": sub_id,
        "email": email.strip().lower(),
        "confirm_token": confirm_token,
        "unsubscribe_token": unsubscribe_token,
    }


def confirm(token: str) -> dict | None:
    """Flip pending→confirmed by confirm_token. Returns the updated row dict
    or None if no match."""
    init_schema()
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM subscribers WHERE confirm_token = ? AND status = 'pending'",
            (token,),
        ).fetchone()
        if not row:
            return None
        c.execute(
            "UPDATE subscribers SET status='confirmed', confirmed_at=? WHERE id=?",
            (_now_iso(), row["id"]),
        )
        return _row_to_dict(row, status="confirmed")


def unsubscribe(token: str) -> dict | None:
    """Flip → unsubscribed by unsubscribe_token. Idempotent (already-unsub
    returns the row again rather than raising)."""
    init_schema()
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM subscribers WHERE unsubscribe_token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        if row["status"] != "unsubscribed":
            c.execute(
                "UPDATE subscribers SET status='unsubscribed', unsubscribed_at=? WHERE id=?",
                (_now_iso(), row["id"]),
            )
        return _row_to_dict(row, status="unsubscribed")


def list_due(now: datetime | None = None) -> list[dict]:
    """Return confirmed subscribers whose last_sent_at is before today UTC.
    Used by the manual dispatcher (POST /api/digest/send_now)."""
    init_schema()
    now = now or datetime.now(timezone.utc)
    today_iso = now.date().isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM subscribers WHERE status='confirmed' "
            "AND (last_sent_at IS NULL OR substr(last_sent_at, 1, 10) < ?)",
            (today_iso,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_sent(subscriber_id: int) -> None:
    init_schema()
    with _conn() as c:
        c.execute(
            "UPDATE subscribers SET last_sent_at=? WHERE id=?",
            (_now_iso(), subscriber_id),
        )


def stats() -> dict:
    """For the admin endpoint."""
    init_schema()
    with _conn() as c:
        out: dict = {"pending": 0, "confirmed": 0, "unsubscribed": 0, "total": 0}
        for status, count in c.execute(
            "SELECT status, COUNT(*) FROM subscribers GROUP BY status"
        ):
            out[status] = count
            out["total"] += count
    return out


def _row_to_dict(row, status: str | None = None) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "filter": json.loads(row["filter_json"]),
        "status": status or row["status"],
        "confirm_token": row["confirm_token"],
        "unsubscribe_token": row["unsubscribe_token"],
        "created_at": row["created_at"],
        "confirmed_at": row["confirmed_at"],
        "unsubscribed_at": row["unsubscribed_at"],
        "last_sent_at": row["last_sent_at"],
    }


# --- Self-test --------------------------------------------------------------

if __name__ == "__main__":
    import shutil
    test_db = os.path.join(tempfile.gettempdir(), "regulators-digest-smoke.sqlite")
    if os.path.exists(test_db):
        os.remove(test_db)
    globals()["DB_PATH"] = test_db
    init_schema()

    # Subscribe
    r = add_pending("alice@example.com", {"tag": "enforcement", "jurisdiction": "US"})
    print(f"add_pending → id={r['id']} email={r['email']} confirm={r['confirm_token'][:12]}…")
    assert r["id"] > 0

    # Cannot list due (still pending)
    assert list_due() == []

    # Confirm
    confirmed = confirm(r["confirm_token"])
    assert confirmed is not None and confirmed["status"] == "confirmed"

    # Now due
    due = list_due()
    assert len(due) == 1 and due[0]["email"] == "alice@example.com"

    # Mark sent
    mark_sent(r["id"])
    assert list_due() == []  # already sent today

    # Stats
    s = stats()
    assert s["confirmed"] == 1

    # Unsubscribe
    unsub = unsubscribe(r["unsubscribe_token"])
    assert unsub is not None and unsub["status"] == "unsubscribed"

    # Unsubscribe is idempotent
    unsub2 = unsubscribe(r["unsubscribe_token"])
    assert unsub2 is not None and unsub2["status"] == "unsubscribed"

    # Bad tokens return None
    assert confirm("not-a-real-token") is None
    assert unsubscribe("not-a-real-token") is None

    # Confirm already-confirmed row → None (status no longer 'pending')
    assert confirm(r["confirm_token"]) is None

    os.remove(test_db)
    print("smoke OK")

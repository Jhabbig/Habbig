"""SQLite-backed cache for scraper output.

One row per (source, key) — `key` is whatever uniquely identifies an item
within a source (URL, post id, hashtag, …). Writes are upserts. Reads are
filtered by `section` or `source` and sorted by score.

Keeping this trivial — the goal is "don't hammer external APIs on every page
load", not building a real datastore.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from models import Item

_LOCK = threading.Lock()
_DB_PATH = Path(__file__).parent / "culture.db"


def set_db_path(path: str | Path) -> None:
    global _DB_PATH
    _DB_PATH = Path(path)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with _connect() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS items (
                section     TEXT NOT NULL,
                source      TEXT NOT NULL,
                key         TEXT NOT NULL,
                title       TEXT NOT NULL,
                url         TEXT,
                image       TEXT,
                summary     TEXT,
                score       REAL NOT NULL DEFAULT 0,
                velocity    REAL NOT NULL DEFAULT 0,
                fetched_at  REAL NOT NULL,
                extra_json  TEXT,
                phash       TEXT,
                PRIMARY KEY (source, key)
            );
            CREATE INDEX IF NOT EXISTS idx_items_section
                ON items(section, score DESC);
            CREATE INDEX IF NOT EXISTS idx_items_source
                ON items(source, fetched_at DESC);
            CREATE INDEX IF NOT EXISTS idx_items_phash
                ON items(phash) WHERE phash IS NOT NULL;

            CREATE TABLE IF NOT EXISTS source_runs (
                source      TEXT PRIMARY KEY,
                last_run    REAL NOT NULL,
                last_ok     INTEGER NOT NULL DEFAULT 0,
                last_error  TEXT
            );

            CREATE TABLE IF NOT EXISTS index_history (
                ts            REAL PRIMARY KEY,
                overall       REAL,
                sections_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_index_history_ts
                ON index_history(ts DESC);
        """)
        # Add phash column to existing DBs that predate the schema bump.
        try:
            c.execute("ALTER TABLE items ADD COLUMN phash TEXT")
        except sqlite3.OperationalError:
            pass


@contextmanager
def _txn() -> Iterator[sqlite3.Connection]:
    with _LOCK:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def replace_source(source: str, items: Iterable[Item]) -> None:
    """Atomically replace all rows for `source` with `items`.

    Replace (not merge) is the right semantic: trending/charts data goes stale
    quickly and we never want yesterday's #1 to linger if today's call returned
    a fresh top-50.
    """
    now = time.time()
    rows = []
    for it in items:
        rows.append((
            it.section,
            it.source,
            (it.url or it.title)[:512],   # key fallback
            it.title,
            it.url,
            it.image,
            it.summary,
            float(it.score),
            float(it.velocity),
            now,
            json.dumps(it.extra) if it.extra else None,
        ))
    with _txn() as c:
        c.execute("DELETE FROM items WHERE source = ?", (source,))
        if rows:
            c.executemany(
                "INSERT OR REPLACE INTO items "
                "(section, source, key, title, url, image, summary, "
                " score, velocity, fetched_at, extra_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        c.execute(
            "INSERT INTO source_runs (source, last_run, last_ok) VALUES (?, ?, 1) "
            "ON CONFLICT(source) DO UPDATE SET last_run=excluded.last_run, "
            "last_ok=1, last_error=NULL",
            (source, now),
        )


def record_failure(source: str, error: str) -> None:
    with _txn() as c:
        c.execute(
            "INSERT INTO source_runs (source, last_run, last_ok, last_error) "
            "VALUES (?, ?, 0, ?) "
            "ON CONFLICT(source) DO UPDATE SET last_run=excluded.last_run, "
            "last_ok=0, last_error=excluded.last_error",
            (source, time.time(), error[:500]),
        )


def get_section(section: str, limit: int = 50) -> list[dict]:
    with _connect() as c:
        cur = c.execute(
            "SELECT * FROM items WHERE section = ? ORDER BY score DESC LIMIT ?",
            (section, limit),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


def get_source(source: str, limit: int = 50) -> list[dict]:
    with _connect() as c:
        cur = c.execute(
            "SELECT * FROM items WHERE source = ? ORDER BY score DESC LIMIT ?",
            (source, limit),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


def list_runs() -> list[dict]:
    with _connect() as c:
        cur = c.execute(
            "SELECT source, last_run, last_ok, last_error FROM source_runs "
            "ORDER BY source"
        )
        return [dict(r) for r in cur.fetchall()]


def items_missing_phash(limit: int = 50) -> list[dict]:
    """Items that have an image URL but no perceptual hash yet."""
    with _connect() as c:
        cur = c.execute(
            "SELECT source, key, image FROM items "
            "WHERE image IS NOT NULL AND phash IS NULL "
            "ORDER BY fetched_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


def set_phash(source: str, key: str, phash: str) -> None:
    with _txn() as c:
        c.execute(
            "UPDATE items SET phash = ? WHERE source = ? AND key = ?",
            (phash, source, key),
        )


def record_index_snapshot(overall: float | None, sections_json: str) -> None:
    with _txn() as c:
        c.execute(
            "INSERT OR REPLACE INTO index_history (ts, overall, sections_json) "
            "VALUES (?, ?, ?)",
            (time.time(), overall, sections_json),
        )


def index_history(hours: int = 72) -> list[dict]:
    cutoff = time.time() - hours * 3600
    with _connect() as c:
        cur = c.execute(
            "SELECT ts, overall, sections_json FROM index_history "
            "WHERE ts >= ? ORDER BY ts ASC",
            (cutoff,),
        )
        out = []
        for r in cur.fetchall():
            d = dict(r)
            try:
                d["sections"] = json.loads(d.pop("sections_json"))
            except json.JSONDecodeError:
                d["sections"] = {}
            out.append(d)
        return out


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    extra = d.pop("extra_json", None)
    if extra:
        try:
            d["extra"] = json.loads(extra)
        except json.JSONDecodeError:
            pass
    return {k: v for k, v in d.items() if v is not None}

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

            CREATE TABLE IF NOT EXISTS item_history (
                source   TEXT NOT NULL,
                key      TEXT NOT NULL,
                ts       REAL NOT NULL,
                score    REAL NOT NULL,
                velocity REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (source, key, ts)
            );
            CREATE INDEX IF NOT EXISTS idx_item_history_lookup
                ON item_history(source, key, ts DESC);

            CREATE TABLE IF NOT EXISTS surge_alerts (
                source       TEXT NOT NULL,
                key          TEXT NOT NULL,
                alerted_at   REAL NOT NULL,
                z_score      REAL NOT NULL,
                payload_json TEXT,
                PRIMARY KEY (source, key, alerted_at)
            );
            CREATE INDEX IF NOT EXISTS idx_surge_alerts_recent
                ON surge_alerts(source, key, alerted_at DESC);
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
            # Append a history snapshot so surge detection has a time series.
            c.executemany(
                "INSERT OR IGNORE INTO item_history (source, key, ts, score, velocity) "
                "VALUES (?,?,?,?,?)",
                [(r[1], r[2], now, r[7], r[8]) for r in rows],
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


def items_with_history(min_points: int = 3, hours: int = 168) -> list[dict]:
    """Return current rows joined with their score history (last `hours`).

    Only includes items that have at least `min_points` history rows — surge
    detection needs a baseline to compute against.
    """
    cutoff = time.time() - hours * 3600
    with _connect() as c:
        cur = c.execute(
            "SELECT source, key, COUNT(*) AS n FROM item_history "
            "WHERE ts >= ? GROUP BY source, key HAVING n >= ?",
            (cutoff, min_points),
        )
        eligible = [(r["source"], r["key"]) for r in cur.fetchall()]
        if not eligible:
            return []

        # Pull the items and their history. SQLite has no array binding, so
        # we use a temp table for the join — much faster than per-item queries.
        c.execute("CREATE TEMP TABLE IF NOT EXISTS _surge_keys (source TEXT, key TEXT)")
        c.execute("DELETE FROM _surge_keys")
        c.executemany("INSERT INTO _surge_keys VALUES (?, ?)", eligible)

        items = {(r["source"], r["key"]): _row_to_dict(r) for r in c.execute(
            "SELECT i.* FROM items i JOIN _surge_keys k "
            "ON i.source = k.source AND i.key = k.key"
        ).fetchall()}

        history: dict[tuple[str, str], list[dict]] = {}
        for r in c.execute(
            "SELECT h.source, h.key, h.ts, h.score FROM item_history h "
            "JOIN _surge_keys k ON h.source = k.source AND h.key = k.key "
            "WHERE h.ts >= ? ORDER BY h.ts ASC",
            (cutoff,),
        ).fetchall():
            history.setdefault((r["source"], r["key"]), []).append(
                {"ts": r["ts"], "score": r["score"]}
            )

    out = []
    for skey, item in items.items():
        item["history"] = history.get(skey, [])
        out.append(item)
    return out


def recent_alert(source: str, key: str, within_seconds: float) -> bool:
    cutoff = time.time() - within_seconds
    with _connect() as c:
        cur = c.execute(
            "SELECT 1 FROM surge_alerts WHERE source = ? AND key = ? "
            "AND alerted_at >= ? LIMIT 1",
            (source, key, cutoff),
        )
        return cur.fetchone() is not None


def record_alert(source: str, key: str, z_score: float, payload_json: str) -> None:
    with _txn() as c:
        c.execute(
            "INSERT INTO surge_alerts (source, key, alerted_at, z_score, payload_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (source, key, time.time(), z_score, payload_json),
        )


def prune_history(days: int = 7) -> int:
    """Delete item_history rows older than `days`. Returns rows removed."""
    cutoff = time.time() - days * 86400
    with _txn() as c:
        cur = c.execute("DELETE FROM item_history WHERE ts < ?", (cutoff,))
        return cur.rowcount


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    extra = d.pop("extra_json", None)
    if extra:
        try:
            d["extra"] = json.loads(extra)
        except json.JSONDecodeError:
            pass
    return {k: v for k, v in d.items() if v is not None}

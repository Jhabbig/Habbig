"""
Local SQLite database for the scraper service.

Stores raw posts, run history, session metadata, and keywords.
Uses WAL mode for concurrent read access (matching main server pattern).
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from scraper.config import SCRAPER_DB_PATH, DEFAULT_TWITTER_KEYWORDS, DEFAULT_TRUTHSOCIAL_KEYWORDS
from scraper.storage.models import RawPost, ScraperRun, ScraperSession

_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(SCRAPER_DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return _local.conn


def init_db() -> None:
    """Create tables if they don't exist. Safe to call multiple times."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS raw_posts (
            id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            author_handle TEXT NOT NULL,
            author_display_name TEXT NOT NULL DEFAULT '',
            author_followers INTEGER NOT NULL DEFAULT 0,
            author_verified INTEGER NOT NULL DEFAULT 0,
            content TEXT NOT NULL,
            posted_at TEXT NOT NULL,
            scraped_at TEXT NOT NULL,
            likes INTEGER NOT NULL DEFAULT 0,
            retweets_or_boosts INTEGER NOT NULL DEFAULT 0,
            replies INTEGER NOT NULL DEFAULT 0,
            keyword_matched TEXT NOT NULL DEFAULT '',
            transmitted INTEGER NOT NULL DEFAULT 0,
            transmission_attempts INTEGER NOT NULL DEFAULT 0,
            last_transmission_attempt TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_posts_transmitted ON raw_posts(transmitted);
        CREATE INDEX IF NOT EXISTS idx_posts_platform ON raw_posts(platform);
        CREATE INDEX IF NOT EXISTS idx_posts_scraped_at ON raw_posts(scraped_at);

        CREATE TABLE IF NOT EXISTS scraper_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            keyword TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            completed_at TEXT,
            posts_found INTEGER NOT NULL DEFAULT 0,
            posts_new INTEGER NOT NULL DEFAULT 0,
            posts_transmitted INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            duration_seconds REAL
        );

        CREATE INDEX IF NOT EXISTS idx_runs_platform ON scraper_runs(platform);
        CREATE INDEX IF NOT EXISTS idx_runs_started ON scraper_runs(started_at);

        CREATE TABLE IF NOT EXISTS scraper_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL UNIQUE,
            session_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            valid INTEGER NOT NULL DEFAULT 1,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            keyword TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(platform, keyword)
        );
    """)
    conn.commit()

    # Seed default keywords if table is empty
    cur = conn.execute("SELECT COUNT(*) FROM keywords")
    if cur.fetchone()[0] == 0:
        _seed_keywords(conn)


def _seed_keywords(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for kw in DEFAULT_TWITTER_KEYWORDS:
        rows.append(("twitter", kw, now))
    for kw in DEFAULT_TRUTHSOCIAL_KEYWORDS:
        rows.append(("truthsocial", kw, now))
    conn.executemany(
        "INSERT OR IGNORE INTO keywords (platform, keyword, created_at) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()


# ── Posts ────────────────────────────────────────────────────────────────────

def insert_post(post: RawPost) -> bool:
    """Insert a post. Returns True if new (not a duplicate)."""
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO raw_posts
               (id, platform, author_handle, author_display_name, author_followers,
                author_verified, content, posted_at, scraped_at, likes,
                retweets_or_boosts, replies, keyword_matched, transmitted,
                transmission_attempts, last_transmission_attempt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                post.id, post.platform, post.author_handle,
                post.author_display_name, post.author_followers,
                int(post.author_verified), post.content,
                post.posted_at.isoformat(), post.scraped_at.isoformat(),
                post.likes, post.retweets_or_boosts, post.replies,
                post.keyword_matched, int(post.transmitted),
                post.transmission_attempts,
                post.last_transmission_attempt.isoformat() if post.last_transmission_attempt else None,
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False  # duplicate


def get_untransmitted(platform: Optional[str] = None, limit: int = 100) -> list[RawPost]:
    conn = _get_conn()
    if platform:
        rows = conn.execute(
            "SELECT * FROM raw_posts WHERE transmitted = 0 AND platform = ? ORDER BY scraped_at LIMIT ?",
            (platform, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM raw_posts WHERE transmitted = 0 ORDER BY scraped_at LIMIT ?",
            (limit,),
        ).fetchall()
    return [RawPost.from_row(dict(r)) for r in rows]


def mark_transmitted(post_ids: list[str]) -> int:
    """Mark posts as transmitted. Returns count updated."""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    for pid in post_ids:
        cur = conn.execute(
            "UPDATE raw_posts SET transmitted = 1, last_transmission_attempt = ? WHERE id = ?",
            (now, pid),
        )
        updated += cur.rowcount
    conn.commit()
    return updated


def increment_transmission_attempts(post_ids: list[str]) -> None:
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    for pid in post_ids:
        conn.execute(
            "UPDATE raw_posts SET transmission_attempts = transmission_attempts + 1, last_transmission_attempt = ? WHERE id = ?",
            (now, pid),
        )
    conn.commit()


def get_untransmitted_count() -> int:
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(*) FROM raw_posts WHERE transmitted = 0").fetchone()
    return row[0]


def get_posts_today_count(platform: str) -> int:
    conn = _get_conn()
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM raw_posts WHERE platform = ? AND scraped_at >= ?",
        (platform, today_start),
    ).fetchone()
    return row[0]


def post_exists(post_id: str) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT 1 FROM raw_posts WHERE id = ?", (post_id,)).fetchone()
    return row is not None


# ── Runs ─────────────────────────────────────────────────────────────────────

def create_run(platform: str, keyword: str) -> int:
    """Start a new scraper run. Returns the run ID."""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO scraper_runs (platform, keyword, started_at) VALUES (?, ?, ?)",
        (platform, keyword, now),
    )
    conn.commit()
    return cur.lastrowid


def complete_run(run_id: int, posts_found: int, posts_new: int, posts_transmitted: int,
                 duration_seconds: float, error: Optional[str] = None) -> None:
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE scraper_runs
           SET completed_at = ?, posts_found = ?, posts_new = ?, posts_transmitted = ?,
               duration_seconds = ?, error = ?
           WHERE id = ?""",
        (now, posts_found, posts_new, posts_transmitted, duration_seconds, error, run_id),
    )
    conn.commit()


def list_runs(platform: Optional[str] = None, limit: int = 50) -> list[ScraperRun]:
    conn = _get_conn()
    if platform:
        rows = conn.execute(
            "SELECT * FROM scraper_runs WHERE platform = ? ORDER BY started_at DESC LIMIT ?",
            (platform, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM scraper_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [ScraperRun.from_row(dict(r)) for r in rows]


def get_run(run_id: int) -> Optional[ScraperRun]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM scraper_runs WHERE id = ?", (run_id,)).fetchone()
    return ScraperRun.from_row(dict(row)) if row else None


def get_last_run(platform: str) -> Optional[ScraperRun]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM scraper_runs WHERE platform = ? ORDER BY started_at DESC LIMIT 1",
        (platform,),
    ).fetchone()
    return ScraperRun.from_row(dict(row)) if row else None


# ── Sessions ─────────────────────────────────────────────────────────────────

def upsert_session(platform: str, session_path: str, notes: Optional[str] = None) -> None:
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO scraper_sessions (platform, session_path, created_at, valid, notes)
           VALUES (?, ?, ?, 1, ?)
           ON CONFLICT(platform) DO UPDATE SET
               session_path = excluded.session_path,
               created_at = excluded.created_at,
               valid = 1,
               notes = excluded.notes""",
        (platform, session_path, now, notes),
    )
    conn.commit()


def get_session(platform: str) -> Optional[ScraperSession]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM scraper_sessions WHERE platform = ?", (platform,),
    ).fetchone()
    return ScraperSession.from_row(dict(row)) if row else None


def update_session_used(platform: str) -> None:
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE scraper_sessions SET last_used_at = ? WHERE platform = ?",
        (now, platform),
    )
    conn.commit()


def invalidate_session(platform: str) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE scraper_sessions SET valid = 0 WHERE platform = ?",
        (platform,),
    )
    conn.commit()


def delete_session(platform: str) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM scraper_sessions WHERE platform = ?", (platform,))
    conn.commit()


def list_sessions() -> list[ScraperSession]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM scraper_sessions ORDER BY platform").fetchall()
    return [ScraperSession.from_row(dict(r)) for r in rows]


# ── Keywords ─────────────────────────────────────────────────────────────────

def get_keywords(platform: Optional[str] = None) -> dict[str, list[str]]:
    """Return keywords grouped by platform."""
    conn = _get_conn()
    if platform:
        rows = conn.execute(
            "SELECT platform, keyword FROM keywords WHERE platform = ? ORDER BY keyword",
            (platform,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT platform, keyword FROM keywords ORDER BY platform, keyword"
        ).fetchall()
    result: dict[str, list[str]] = {"twitter": [], "truthsocial": []}
    for r in rows:
        p = r["platform"]
        if p not in result:
            result[p] = []
        result[p].append(r["keyword"])
    return result


def add_keyword(platform: str, keyword: str) -> bool:
    """Add a keyword. Returns True if added (not duplicate)."""
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO keywords (platform, keyword) VALUES (?, ?)",
            (platform, keyword),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def remove_keyword(platform: str, keyword: str) -> bool:
    """Remove a keyword. Returns True if it existed."""
    conn = _get_conn()
    cur = conn.execute(
        "DELETE FROM keywords WHERE platform = ? AND keyword = ?",
        (platform, keyword),
    )
    conn.commit()
    return cur.rowcount > 0

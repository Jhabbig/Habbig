"""
Local SQLite store for the annoyance dashboard.

Raw sqlite3 (sync) — matches crypto-dashboard/database.py pattern. WAL mode for
concurrent reads. Thread-local connections so the async background loops and
FastAPI handlers each get their own.

Schema is initialized on first call to init_db(). Safe to call multiple times.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional

import config

_local = threading.local()


# ── Connection management ────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return _local.conn


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    conn = _get_conn()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Schema ───────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_channel TEXT,
    author TEXT,
    content TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    url TEXT,
    engagement INTEGER NOT NULL DEFAULT 0,
    keyword TEXT,
    classified INTEGER NOT NULL DEFAULT 0,
    content_dropped_at TEXT           -- NULL until 30d TTL job scrubs content
);
CREATE INDEX IF NOT EXISTS idx_posts_classified ON posts(classified);
CREATE INDEX IF NOT EXISTS idx_posts_posted_at ON posts(posted_at);
CREATE INDEX IF NOT EXISTS idx_posts_source ON posts(source);

CREATE TABLE IF NOT EXISTS classifications (
    post_id TEXT PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
    annoyance_score REAL NOT NULL,
    sentiment TEXT NOT NULL,
    primary_topic TEXT,
    entities_json TEXT NOT NULL,
    classified_at TEXT NOT NULL,
    model TEXT NOT NULL,
    is_sensitive INTEGER NOT NULL DEFAULT 0,
    sensitive_reason TEXT,
    triage_score REAL                 -- Haiku's short-circuit score for audit
);
CREATE INDEX IF NOT EXISTS idx_classifications_classified_at ON classifications(classified_at);

CREATE TABLE IF NOT EXISTS annoyance_index (
    hour TEXT PRIMARY KEY,
    score REAL NOT NULL,
    post_count INTEGER NOT NULL,
    sources_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_counts (
    entity TEXT NOT NULL,
    entity_type TEXT,
    hour TEXT NOT NULL,
    count INTEGER NOT NULL,
    avg_annoyance REAL NOT NULL,
    PRIMARY KEY (entity, hour)
);
CREATE INDEX IF NOT EXISTS idx_entity_counts_hour ON entity_counts(hour);

CREATE TABLE IF NOT EXISTS spikes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity TEXT NOT NULL,
    detected_hour TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    z_score REAL NOT NULL,
    multiple_of_baseline REAL NOT NULL,
    avg_annoyance REAL NOT NULL,
    count INTEGER NOT NULL,
    sample_post_ids_json TEXT NOT NULL,
    sample_excerpts_json TEXT,         -- cached at insertion; survives 30d raw TTL
    summary TEXT,
    confidence_score REAL,             -- 0-100 blended (z + backtest)
    sources_json TEXT,                 -- [{"source":"reddit","count":8},...]
    UNIQUE(entity, detected_hour)
);
CREATE INDEX IF NOT EXISTS idx_spikes_detected ON spikes(detected_at);

CREATE TABLE IF NOT EXISTS sources (
    name TEXT PRIMARY KEY,
    last_fetch_at TEXT,
    last_ok INTEGER NOT NULL DEFAULT 0,
    posts_today INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);

-- Claude usage tracking for cost ceiling enforcement and reporting.
CREATE TABLE IF NOT EXISTS claude_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    operation TEXT NOT NULL,            -- 'triage'|'classify'|'summarize'
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    estimated_cost_cents REAL NOT NULL,
    post_count INTEGER NOT NULL DEFAULT 0,
    batch_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_claude_usage_ts ON claude_usage(timestamp);

-- User-submitted false-positive flags for spikes; review queue in admin panel.
CREATE TABLE IF NOT EXISTS fp_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spike_id INTEGER NOT NULL REFERENCES spikes(id) ON DELETE CASCADE,
    user_id TEXT,
    user_email TEXT,
    reason TEXT,
    flagged_at TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    resolution_note TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_fp_flags_resolved ON fp_flags(resolved, flagged_at);

-- Email-notification ledger: dedup sends, track delivery state.
CREATE TABLE IF NOT EXISTS email_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spike_id INTEGER NOT NULL REFERENCES spikes(id) ON DELETE CASCADE,
    user_email TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    status TEXT NOT NULL,                -- 'sent'|'failed'|'skipped'
    error TEXT,
    UNIQUE(spike_id, user_email)
);
CREATE INDEX IF NOT EXISTS idx_email_notifications_sent ON email_notifications(sent_at);
"""


# Idempotent per-column ALTER TABLE migrations. SQLite doesn't support
# ADD COLUMN IF NOT EXISTS, so we introspect table_info first.
# Format: (table, column, ddl)
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("posts", "content_dropped_at", "ALTER TABLE posts ADD COLUMN content_dropped_at TEXT"),
    ("classifications", "is_sensitive", "ALTER TABLE classifications ADD COLUMN is_sensitive INTEGER NOT NULL DEFAULT 0"),
    ("classifications", "sensitive_reason", "ALTER TABLE classifications ADD COLUMN sensitive_reason TEXT"),
    ("classifications", "triage_score", "ALTER TABLE classifications ADD COLUMN triage_score REAL"),
    ("spikes", "sample_excerpts_json", "ALTER TABLE spikes ADD COLUMN sample_excerpts_json TEXT"),
    ("spikes", "confidence_score", "ALTER TABLE spikes ADD COLUMN confidence_score REAL"),
    ("spikes", "sources_json", "ALTER TABLE spikes ADD COLUMN sources_json TEXT"),
]

# Indexes that reference columns added by _COLUMN_MIGRATIONS. Run AFTER
# those migrations so the columns exist.
_POST_MIGRATION_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_classifications_sensitive ON classifications(is_sensitive)",
]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def init_db() -> None:
    conn = _get_conn()
    # Create missing tables + indexes (excluding indexes on migrated columns)
    conn.executescript(_SCHEMA)
    # Backfill columns added after initial ship
    for table, column, ddl in _COLUMN_MIGRATIONS:
        existing = _table_columns(conn, table)
        if column not in existing:
            conn.execute(ddl)
    # Indexes that depend on migrated columns must run after the columns exist
    for ddl in _POST_MIGRATION_INDEXES:
        conn.execute(ddl)
    conn.commit()


# ── Posts ────────────────────────────────────────────────────────────────────

def insert_post(
    id: str,
    source: str,
    content: str,
    posted_at: str,
    *,
    source_channel: Optional[str] = None,
    author: Optional[str] = None,
    url: Optional[str] = None,
    engagement: int = 0,
    keyword: Optional[str] = None,
) -> bool:
    """Insert a post. Returns True if newly inserted (not a duplicate)."""
    with cursor() as cur:
        try:
            cur.execute(
                """INSERT INTO posts
                   (id, source, source_channel, author, content, posted_at,
                    fetched_at, url, engagement, keyword, classified)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (id, source, source_channel, author, content, posted_at,
                 now_iso(), url, engagement, keyword),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def get_unclassified_posts(limit: int) -> list[dict]:
    with cursor() as cur:
        rows = cur.execute(
            """SELECT id, source, content, posted_at
               FROM posts
               WHERE classified = 0
               ORDER BY posted_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_classified(post_id: str, status: int = 1) -> None:
    with cursor() as cur:
        cur.execute("UPDATE posts SET classified = ? WHERE id = ?", (status, post_id))


def mark_many_classified(post_ids: Iterable[str], status: int = 1) -> None:
    with cursor() as cur:
        cur.executemany(
            "UPDATE posts SET classified = ? WHERE id = ?",
            [(status, pid) for pid in post_ids],
        )


def count_posts(*, since_hour: Optional[str] = None) -> int:
    with cursor() as cur:
        if since_hour:
            row = cur.execute(
                "SELECT COUNT(*) FROM posts WHERE posted_at >= ?", (since_hour,)
            ).fetchone()
        else:
            row = cur.execute("SELECT COUNT(*) FROM posts").fetchone()
    return row[0] if row else 0


def count_classified_since(since_iso: str) -> int:
    with cursor() as cur:
        row = cur.execute(
            "SELECT COUNT(*) FROM classifications WHERE classified_at >= ?",
            (since_iso,),
        ).fetchone()
    return row[0] if row else 0


# ── Classifications ──────────────────────────────────────────────────────────

def insert_classification(
    post_id: str,
    annoyance_score: float,
    sentiment: str,
    primary_topic: Optional[str],
    entities: list[dict],
    model: str,
    is_sensitive: bool = False,
    sensitive_reason: Optional[str] = None,
    triage_score: Optional[float] = None,
) -> None:
    with cursor() as cur:
        cur.execute(
            """INSERT OR REPLACE INTO classifications
               (post_id, annoyance_score, sentiment, primary_topic,
                entities_json, classified_at, model,
                is_sensitive, sensitive_reason, triage_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (post_id, annoyance_score, sentiment, primary_topic,
             json.dumps(entities), now_iso(), model,
             1 if is_sensitive else 0, sensitive_reason, triage_score),
        )


def get_classifications_in_hour(hour_iso: str) -> list[dict]:
    """Return all classifications whose posts fall in [hour, hour+1h)."""
    # We join on posts to get posted_at bucketing. Hour format: 'YYYY-MM-DDTHH:00:00+00:00'.
    next_hour = _hour_bump(hour_iso, 1)
    with cursor() as cur:
        rows = cur.execute(
            """SELECT c.post_id, c.annoyance_score, c.sentiment, c.primary_topic,
                      c.entities_json, p.posted_at, p.source, p.source_channel, p.content
               FROM classifications c
               JOIN posts p ON p.id = c.post_id
               WHERE p.posted_at >= ? AND p.posted_at < ?""",
            (hour_iso, next_hour),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Annoyance index ──────────────────────────────────────────────────────────

def upsert_annoyance_index(hour: str, score: float, post_count: int, sources: dict) -> None:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO annoyance_index (hour, score, post_count, sources_json)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(hour) DO UPDATE SET
                 score = excluded.score,
                 post_count = excluded.post_count,
                 sources_json = excluded.sources_json""",
            (hour, score, post_count, json.dumps(sources)),
        )


def get_annoyance_index(hours: int = 24) -> list[dict]:
    with cursor() as cur:
        rows = cur.execute(
            """SELECT hour, score, post_count, sources_json
               FROM annoyance_index
               ORDER BY hour DESC
               LIMIT ?""",
            (hours,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["sources"] = json.loads(d.pop("sources_json") or "{}")
        except Exception:
            d["sources"] = {}
        out.append(d)
    out.reverse()  # chronological
    return out


# ── Entity counts ────────────────────────────────────────────────────────────

def upsert_entity_count(
    entity: str, entity_type: Optional[str], hour: str,
    count: int, avg_annoyance: float,
) -> None:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO entity_counts (entity, entity_type, hour, count, avg_annoyance)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(entity, hour) DO UPDATE SET
                 entity_type = excluded.entity_type,
                 count = excluded.count,
                 avg_annoyance = excluded.avg_annoyance""",
            (entity, entity_type, hour, count, avg_annoyance),
        )


def get_entity_history(entity: str, hours: int = 168) -> list[dict]:
    """Last N hours of counts for one entity."""
    with cursor() as cur:
        rows = cur.execute(
            """SELECT hour, count, avg_annoyance
               FROM entity_counts
               WHERE entity = ?
               ORDER BY hour DESC
               LIMIT ?""",
            (entity, hours),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_top_entities_for_hour(hour: str, limit: int = 20) -> list[dict]:
    with cursor() as cur:
        rows = cur.execute(
            """SELECT entity, entity_type, count, avg_annoyance
               FROM entity_counts
               WHERE hour = ?
               ORDER BY (count * (avg_annoyance / 50.0)) DESC
               LIMIT ?""",
            (hour, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_hour_with_entity_data() -> Optional[str]:
    """Return the most recent hour bucket that has at least one entity_counts row,
    or None if the table is empty. Lets /api/entities/top degrade gracefully in
    the first minutes of a fresh hour before the aggregator has new data."""
    with cursor() as cur:
        row = cur.execute(
            "SELECT hour FROM entity_counts ORDER BY hour DESC LIMIT 1"
        ).fetchone()
    return row["hour"] if row else None


def get_distinct_entities_with_min_count(min_count: int) -> list[str]:
    """Entities worth running the spike detector on."""
    with cursor() as cur:
        rows = cur.execute(
            """SELECT entity, SUM(count) AS total
               FROM entity_counts
               GROUP BY entity
               HAVING total >= ?
               ORDER BY total DESC""",
            (min_count,),
        ).fetchall()
    return [r["entity"] for r in rows]


# ── Spikes ───────────────────────────────────────────────────────────────────

def insert_spike(
    entity: str, detected_hour: str, z_score: float, multiple_of_baseline: float,
    avg_annoyance: float, count: int, sample_post_ids: list[str],
    summary: Optional[str] = None,
    sample_excerpts: Optional[list[str]] = None,
    confidence_score: Optional[float] = None,
    sources_breakdown: Optional[list[dict]] = None,
) -> Optional[int]:
    """Insert a spike, dedup by (entity, detected_hour). Returns the new spike id
    if newly inserted, or None on dedup hit. The `sample_excerpts` list is cached
    at insertion so spike cards stay readable after the 30-day raw-content TTL
    scrubs the underlying posts.
    """
    with cursor() as cur:
        try:
            cur.execute(
                """INSERT INTO spikes
                   (entity, detected_hour, detected_at, z_score, multiple_of_baseline,
                    avg_annoyance, count, sample_post_ids_json, summary,
                    sample_excerpts_json, confidence_score, sources_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (entity, detected_hour, now_iso(), z_score, multiple_of_baseline,
                 avg_annoyance, count, json.dumps(sample_post_ids), summary,
                 json.dumps(sample_excerpts or []),
                 confidence_score,
                 json.dumps(sources_breakdown or [])),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def get_recent_spikes(limit: int = 20) -> list[dict]:
    with cursor() as cur:
        rows = cur.execute(
            """SELECT id, entity, detected_hour, detected_at, z_score,
                      multiple_of_baseline, avg_annoyance, count,
                      sample_post_ids_json, sample_excerpts_json,
                      summary, confidence_score, sources_json
               FROM spikes
               ORDER BY detected_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for src_col, dst_col, default in (
            ("sample_post_ids_json", "sample_post_ids", []),
            ("sample_excerpts_json", "sample_excerpts", []),
            ("sources_json", "sources_breakdown", []),
        ):
            try:
                d[dst_col] = json.loads(d.pop(src_col) or "null") or default
            except Exception:
                d[dst_col] = default
        out.append(d)
    return out


def get_posts_by_ids(post_ids: list[str]) -> list[dict]:
    if not post_ids:
        return []
    placeholders = ",".join("?" * len(post_ids))
    with cursor() as cur:
        rows = cur.execute(
            f"""SELECT id, source, source_channel, author, content, posted_at, url, engagement
                FROM posts WHERE id IN ({placeholders})""",
            post_ids,
        ).fetchall()
    return [dict(r) for r in rows]


def get_posts_with_sensitivity(post_ids: list[str]) -> list[dict]:
    """Variant of ``get_posts_by_ids`` that LEFT JOINs classifications so the
    caller can see ``is_sensitive`` + ``sensitive_reason`` + ``annoyance_score``
    per post. Used by ``/api/spikes`` so the client can blur sensitive sample
    excerpts by default.

    LEFT JOIN (not INNER) so unclassified posts still come back — they simply
    have ``is_sensitive=0`` and the score fields as ``None``.
    """
    if not post_ids:
        return []
    placeholders = ",".join("?" * len(post_ids))
    with cursor() as cur:
        rows = cur.execute(
            f"""SELECT p.id, p.source, p.source_channel, p.author, p.content,
                       p.posted_at, p.url, p.engagement,
                       COALESCE(c.is_sensitive, 0) AS is_sensitive,
                       c.sensitive_reason, c.annoyance_score
                FROM posts p
                LEFT JOIN classifications c ON c.post_id = p.id
                WHERE p.id IN ({placeholders})""",
            post_ids,
        ).fetchall()
    return [dict(r) for r in rows]


def get_entity_spikes(entity: str, limit: int = 20) -> list[dict]:
    """Recent spikes for a single entity, newest first.

    Same JSON-deserialization semantics as ``get_recent_spikes`` — the three
    JSON-encoded columns are parsed into native lists/dicts for the caller.
    Used by the entity drill-in page (/entity/{name}).
    """
    with cursor() as cur:
        rows = cur.execute(
            """SELECT id, entity, detected_hour, detected_at, z_score,
                      multiple_of_baseline, avg_annoyance, count,
                      sample_post_ids_json, sample_excerpts_json,
                      summary, confidence_score, sources_json
               FROM spikes
               WHERE entity = ?
               ORDER BY detected_at DESC
               LIMIT ?""",
            (entity, limit),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for src_col, dst_col, default in (
            ("sample_post_ids_json", "sample_post_ids", []),
            ("sample_excerpts_json", "sample_excerpts", []),
            ("sources_json", "sources_breakdown", []),
        ):
            try:
                d[dst_col] = json.loads(d.pop(src_col) or "null") or default
            except Exception:
                d[dst_col] = default
        out.append(d)
    return out


def get_entity_recent_classified_posts(entity: str, limit: int = 30) -> list[dict]:
    """Recent classified posts mentioning ``entity``.

    SQLite doesn't have first-class JSON containment operators so we use
    ``LIKE %entity%`` against ``entities_json``. That's loose (will match
    substrings inside other entity names) but good enough for the drill-in
    view — and cheap on an indexed table. For entity 'Apple' it will match
    the literal substring anywhere in the serialized list, which in practice
    catches every prediction that includes an entity whose ``name`` contains
    'Apple'. False-positive substring matches are acceptable here because
    the spike detector uses canonicalized names that don't share prefixes.

    Security (P8.1): sanitize LIKE wildcards in `entity` before interpolating.
    Without this, an entity name containing '%' or '_' (crafted via the FP
    flag reason or a malicious post-classification entity) could turn the
    drill-in query into a wildcard scan or a side-channel. Escape backslash
    first (it's the escape char), then '%' and '_', and declare ESCAPE '\'
    on the LIKE clause so SQLite honours the escaping.
    """
    safe_entity = (
        entity.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    with cursor() as cur:
        rows = cur.execute(
            r"""SELECT p.id AS post_id,
                       p.source, p.source_channel, p.content, p.posted_at, p.url,
                       c.annoyance_score, c.sentiment, c.primary_topic,
                       c.is_sensitive, c.sensitive_reason, c.classified_at
                FROM classifications c
                JOIN posts p ON p.id = c.post_id
                WHERE c.entities_json LIKE ? ESCAPE '\'
                ORDER BY c.classified_at DESC
                LIMIT ?""",
            (f"%{safe_entity}%", limit),
        ).fetchall()
    return [dict(r) for r in rows]


def count_user_emails_today(user_email: str) -> int:
    """Per-user daily email count. Used by the notifications module to
    enforce the 5-emails-per-user-per-day cap from the polish-layer spec.

    Counts rows with status='sent' (skipped/failed don't consume the quota).
    Window is rolling 24h from `now`, which is slightly stricter than a
    calendar day but stops "send 5 at 23:50 + 5 at 00:10 → 10 in 20min".
    """
    with cursor() as cur:
        row = cur.execute(
            """SELECT COUNT(*)
               FROM email_notifications
               WHERE user_email = ?
                 AND status = 'sent'
                 AND sent_at >= datetime('now', '-1 day')""",
            (user_email,),
        ).fetchone()
    return int(row[0] if row else 0)


# ── Sources ──────────────────────────────────────────────────────────────────

def upsert_source_status(
    name: str, ok: bool, *, posts_today: int = 0, error: Optional[str] = None,
) -> None:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO sources (name, last_fetch_at, last_ok, posts_today, error_count, last_error)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 last_fetch_at = excluded.last_fetch_at,
                 last_ok = excluded.last_ok,
                 posts_today = excluded.posts_today,
                 error_count = CASE WHEN excluded.last_ok = 1 THEN 0
                                    ELSE sources.error_count + 1 END,
                 last_error = excluded.last_error""",
            (name, now_iso(), 1 if ok else 0, posts_today,
             0 if ok else 1, error),
        )


def get_all_sources() -> list[dict]:
    with cursor() as cur:
        rows = cur.execute(
            "SELECT name, last_fetch_at, last_ok, posts_today, error_count, last_error FROM sources"
        ).fetchall()
    return [dict(r) for r in rows]


# ── Claude usage / cost ──────────────────────────────────────────────────────

def log_claude_usage(
    operation: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    estimated_cost_cents: float,
    *,
    post_count: int = 0,
    batch_id: Optional[str] = None,
) -> None:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO claude_usage
               (timestamp, operation, model, input_tokens, output_tokens,
                estimated_cost_cents, post_count, batch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (now_iso(), operation, model, input_tokens, output_tokens,
             estimated_cost_cents, post_count, batch_id),
        )


def cost_cents_since(since_iso: str) -> float:
    with cursor() as cur:
        row = cur.execute(
            "SELECT COALESCE(SUM(estimated_cost_cents), 0) FROM claude_usage WHERE timestamp >= ?",
            (since_iso,),
        ).fetchone()
    return float(row[0] if row else 0.0)


def cost_summary(days: int = 7) -> list[dict]:
    """Return per-day cost breakdown grouped by model + operation."""
    with cursor() as cur:
        rows = cur.execute(
            """SELECT substr(timestamp, 1, 10) AS day,
                      model, operation,
                      COUNT(*) AS call_count,
                      SUM(post_count) AS posts,
                      SUM(input_tokens) AS input_tokens,
                      SUM(output_tokens) AS output_tokens,
                      SUM(estimated_cost_cents) AS cost_cents
               FROM claude_usage
               WHERE timestamp >= date('now', ?)
               GROUP BY day, model, operation
               ORDER BY day DESC, model, operation""",
            (f"-{max(1, days)} days",),
        ).fetchall()
    return [dict(r) for r in rows]


# ── FP flags ─────────────────────────────────────────────────────────────────

def insert_fp_flag(
    spike_id: int,
    user_id: Optional[str],
    user_email: Optional[str],
    reason: Optional[str],
) -> int:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO fp_flags (spike_id, user_id, user_email, reason, flagged_at)
               VALUES (?, ?, ?, ?, ?)""",
            (spike_id, user_id, user_email, reason, now_iso()),
        )
        return cur.lastrowid


def list_fp_queue(*, resolved: bool = False, limit: int = 50) -> list[dict]:
    with cursor() as cur:
        rows = cur.execute(
            """SELECT f.id, f.spike_id, f.user_id, f.user_email, f.reason,
                      f.flagged_at, f.resolved, f.resolution_note, f.resolved_at,
                      s.entity, s.detected_at, s.summary, s.z_score, s.count,
                      s.confidence_score, s.sources_json
               FROM fp_flags f
               JOIN spikes s ON s.id = f.spike_id
               WHERE f.resolved = ?
               ORDER BY f.flagged_at DESC
               LIMIT ?""",
            (1 if resolved else 0, limit),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["sources_breakdown"] = json.loads(d.pop("sources_json") or "[]")
        except Exception:
            d["sources_breakdown"] = []
            d.pop("sources_json", None)
        out.append(d)
    return out


def resolve_fp_flag(flag_id: int, note: Optional[str] = None) -> bool:
    with cursor() as cur:
        cur.execute(
            """UPDATE fp_flags
               SET resolved = 1, resolution_note = ?, resolved_at = ?
               WHERE id = ? AND resolved = 0""",
            (note, now_iso(), flag_id),
        )
        return cur.rowcount > 0


# ── Email notifications ──────────────────────────────────────────────────────

def record_email_notification(
    spike_id: int, user_email: str, status: str, error: Optional[str] = None,
) -> bool:
    """Insert with dedup on (spike_id, user_email). Returns True if new row."""
    with cursor() as cur:
        try:
            cur.execute(
                """INSERT INTO email_notifications (spike_id, user_email, sent_at, status, error)
                   VALUES (?, ?, ?, ?, ?)""",
                (spike_id, user_email, now_iso(), status, error),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def spike_already_emailed(spike_id: int, user_email: str) -> bool:
    with cursor() as cur:
        row = cur.execute(
            "SELECT 1 FROM email_notifications WHERE spike_id = ? AND user_email = ?",
            (spike_id, user_email),
        ).fetchone()
    return row is not None


# ── Per-source entity breakdowns (for multi-source corroboration) ────────────

def get_entity_hourly_counts_by_source(entity: str, hour_iso: str) -> dict[str, int]:
    """Count classified posts mentioning `entity` in the given hour, grouped by source.
    Used by the multi-source corroboration gate in spike_detector.

    Security (P8.1): same LIKE-wildcard escaping as
    get_entity_recent_classified_posts — a '%' or '_' in an entity name
    must not turn into a query-manipulation primitive.
    """
    # Hour bucket semantics match db.get_classifications_in_hour.
    next_hour = _hour_bump(hour_iso, 1)
    safe_entity = (
        entity.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    with cursor() as cur:
        rows = cur.execute(
            r"""SELECT p.source, COUNT(*) AS cnt
                FROM classifications c
                JOIN posts p ON p.id = c.post_id
                WHERE p.posted_at >= ? AND p.posted_at < ?
                  AND c.entities_json LIKE ? ESCAPE '\'
                GROUP BY p.source""",
            (hour_iso, next_hour, f"%{safe_entity}%"),
        ).fetchall()
    return {r["source"]: int(r["cnt"]) for r in rows}


def get_entity_hourly_source_stats(entity: str, hour_iso: str) -> dict[str, dict[str, int]]:
    """Like `get_entity_hourly_counts_by_source` but also counts distinct authors
    per source. Used by the multi-source corroboration gate to surface the
    posts/authors ratio — a single account spraying N posts should not count
    as N corroborators.

    Returns `{source: {"posts": int, "unique_authors": int}}`. A source with
    unique_authors < posts signals one account driving most of the volume,
    which the admin FP queue surfaces for manual review (P4.1).

    Security: LIKE-wildcard escaping identical to the sibling helper — '%'/'_'
    in an entity name must not become a query-manipulation primitive.
    """
    next_hour = _hour_bump(hour_iso, 1)
    safe_entity = (
        entity.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    with cursor() as cur:
        rows = cur.execute(
            r"""SELECT p.source,
                       COUNT(*) AS cnt,
                       COUNT(DISTINCT COALESCE(NULLIF(p.author, ''), 'anon:' || p.id)) AS authors
                  FROM classifications c
                  JOIN posts p ON p.id = c.post_id
                 WHERE p.posted_at >= ? AND p.posted_at < ?
                   AND c.entities_json LIKE ? ESCAPE '\'
                 GROUP BY p.source""",
            (hour_iso, next_hour, f"%{safe_entity}%"),
        ).fetchall()
    return {
        r["source"]: {"posts": int(r["cnt"]), "unique_authors": int(r["authors"])}
        for r in rows
    }


# ── Raw-content TTL job ──────────────────────────────────────────────────────

def scrub_raw_content_older_than(days: int = 30) -> int:
    """Zero out `content` and `author` on posts older than N days. Keep the
    row and classification (retention policy: classifications forever).
    Returns rows scrubbed."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with cursor() as cur:
        cur.execute(
            """UPDATE posts
               SET content = '', author = NULL, content_dropped_at = ?
               WHERE posted_at < ?
                 AND content_dropped_at IS NULL""",
            (now_iso(), cutoff),
        )
        return cur.rowcount


# ── Helpers ──────────────────────────────────────────────────────────────────

def bucket_hour(iso: str) -> str:
    """Round an ISO timestamp down to the hour. Returns ISO with '+00:00' tz."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return dt.isoformat()


def _hour_bump(hour_iso: str, hours: int) -> str:
    from datetime import timedelta
    dt = datetime.fromisoformat(hour_iso)
    dt = dt + timedelta(hours=hours)
    return dt.isoformat()


def current_hour_iso() -> str:
    return bucket_hour(now_iso())

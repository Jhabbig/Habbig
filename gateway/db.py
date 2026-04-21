"""SQLite layer for the gateway — users, sessions, subscriptions."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

# The SQLite database path. Defaults to ./auth.db beside this file, but can
# be overridden via GATEWAY_DB_PATH so staging can use auth-staging.db on the
# same host without ever touching production data.
import os as _os
_db_override = _os.environ.get("GATEWAY_DB_PATH", "").strip()
if _db_override:
    _p = Path(_db_override)
    DB_PATH = _p if _p.is_absolute() else (Path(__file__).parent / _p)
else:
    DB_PATH = Path(__file__).parent / "auth.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    username          TEXT UNIQUE NOT NULL,
    email             TEXT UNIQUE NOT NULL,
    password_hash     TEXT NOT NULL,
    password_salt     TEXT NOT NULL,
    created_at        INTEGER NOT NULL,
    is_admin          INTEGER NOT NULL DEFAULT 0,
    suspended         INTEGER NOT NULL DEFAULT 0,
    default_dashboard TEXT,
    invite_token_id   INTEGER REFERENCES invite_tokens(id)
);

CREATE TABLE IF NOT EXISTS sessions (
    token           TEXT PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    csrf_token      TEXT,
    csrf_created_at INTEGER,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    dashboard_key   TEXT NOT NULL,
    plan            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    started_at      INTEGER NOT NULL,
    expires_at      INTEGER,
    stripe_sub_id   TEXT,
    source          TEXT NOT NULL DEFAULT 'placeholder',
    UNIQUE(user_id, dashboard_key),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS invite_tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT UNIQUE NOT NULL,
    status          TEXT NOT NULL DEFAULT 'unclaimed',
    claimed_by_user_id INTEGER REFERENCES users(id),
    claimed_by_email TEXT,
    note            TEXT DEFAULT '',
    created_at      INTEGER NOT NULL,
    claimed_at      INTEGER
);

CREATE TABLE IF NOT EXISTS enquiries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL,
    job_title       TEXT NOT NULL,
    message         TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    read            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS password_resets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    token       TEXT UNIQUE NOT NULL,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_invite_token ON invite_tokens(token);
CREATE INDEX IF NOT EXISTS idx_invite_status ON invite_tokens(status);
CREATE INDEX IF NOT EXISTS idx_password_resets_token ON password_resets(token);

CREATE TABLE IF NOT EXISTS newsletter_subscribers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT UNIQUE NOT NULL,
    subscribed_at   INTEGER NOT NULL,
    source          TEXT NOT NULL DEFAULT 'prerelease'
);

CREATE TABLE IF NOT EXISTS user_market_credentials (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                     INTEGER NOT NULL,
    source                      TEXT NOT NULL,
    kalshi_token                TEXT,
    kalshi_member_id            TEXT,
    kalshi_token_expires_at     INTEGER,
    polymarket_wallet_address   TEXT,
    connected_at                INTEGER NOT NULL,
    last_used_at                INTEGER,
    is_active                   INTEGER NOT NULL DEFAULT 1,
    UNIQUE(user_id, source),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_positions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL,
    platform            TEXT NOT NULL,
    market_id           TEXT NOT NULL,
    market_title        TEXT NOT NULL,
    side                TEXT NOT NULL,
    shares              REAL NOT NULL DEFAULT 0,
    avg_entry_price     REAL NOT NULL DEFAULT 0,
    current_price       REAL NOT NULL DEFAULT 0,
    unrealised_pnl      REAL NOT NULL DEFAULT 0,
    position_value_usd  REAL NOT NULL DEFAULT 0,
    last_synced_at      INTEGER NOT NULL,
    UNIQUE(user_id, platform, market_id, side),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_positions_user ON user_positions(user_id);
CREATE INDEX IF NOT EXISTS idx_positions_user_platform ON user_positions(user_id, platform);

CREATE TABLE IF NOT EXISTS user_bet_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL,
    source              TEXT NOT NULL,
    external_order_id   TEXT,
    market_id           TEXT NOT NULL,
    market_title        TEXT NOT NULL,
    side                TEXT NOT NULL,
    amount_usd          REAL NOT NULL,
    price_at_bet        REAL NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    placed_at           INTEGER NOT NULL,
    resolved_correct    INTEGER,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_market_creds_user ON user_market_credentials(user_id);
CREATE INDEX IF NOT EXISTS idx_bet_history_user ON user_bet_history(user_id);

-- Credibility engine tables
CREATE TABLE IF NOT EXISTS source_credibility (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    source_handle           TEXT NOT NULL,
    global_credibility      REAL NOT NULL DEFAULT 0.5,
    accuracy_unlocked       INTEGER NOT NULL DEFAULT 0,
    decay_weighted_accuracy REAL,
    total_predictions       INTEGER NOT NULL DEFAULT 0,
    correct_predictions     INTEGER NOT NULL DEFAULT 0,
    categories_active       INTEGER NOT NULL DEFAULT 0,
    last_computed_at        INTEGER NOT NULL,
    UNIQUE(source_handle)
);

CREATE TABLE IF NOT EXISTS source_category_credibility (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_handle       TEXT NOT NULL,
    category            TEXT NOT NULL,
    category_credibility REAL NOT NULL DEFAULT 0.5,
    prediction_count    INTEGER NOT NULL DEFAULT 0,
    correct_count       INTEGER NOT NULL DEFAULT 0,
    last_computed_at    INTEGER NOT NULL,
    UNIQUE(source_handle, category)
);

CREATE TABLE IF NOT EXISTS credibility_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_handle   TEXT NOT NULL,
    global_credibility REAL NOT NULL,
    snapshot_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cred_source ON source_credibility(source_handle);
CREATE INDEX IF NOT EXISTS idx_cred_cat ON source_category_credibility(source_handle, category);
CREATE INDEX IF NOT EXISTS idx_cred_snap ON credibility_snapshots(source_handle, snapshot_at);

-- Predictions table
CREATE TABLE IF NOT EXISTS predictions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_handle       TEXT NOT NULL,
    market_id           TEXT,
    category            TEXT NOT NULL DEFAULT 'other',
    direction           TEXT,
    predicted_probability REAL,
    content             TEXT NOT NULL,
    source_url          TEXT,
    extracted_at        INTEGER NOT NULL,
    resolved            INTEGER NOT NULL DEFAULT 0,
    resolved_correct    INTEGER,
    resolved_at         INTEGER
);

CREATE INDEX IF NOT EXISTS idx_predictions_source ON predictions(source_handle);
CREATE INDEX IF NOT EXISTS idx_predictions_market ON predictions(market_id);

-- Signal Search tables (Pro feature)
CREATE TABLE IF NOT EXISTS user_topics (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                     INTEGER NOT NULL,
    name                        TEXT NOT NULL,
    keywords                    TEXT NOT NULL DEFAULT '[]',
    schedule_minutes            INTEGER NOT NULL DEFAULT 60,
    last_pulled_at              INTEGER,
    next_pull_at                INTEGER,
    posts_found_total           INTEGER NOT NULL DEFAULT 0,
    predictions_extracted_total INTEGER NOT NULL DEFAULT 0,
    is_active                   INTEGER NOT NULL DEFAULT 1,
    created_at                  INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_topic_predictions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_topic_id   INTEGER NOT NULL,
    prediction_id   INTEGER NOT NULL,
    pulled_at       INTEGER NOT NULL,
    FOREIGN KEY (user_topic_id) REFERENCES user_topics(id) ON DELETE CASCADE,
    FOREIGN KEY (prediction_id) REFERENCES predictions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_topic_analyses (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_topic_id       INTEGER NOT NULL,
    signal_direction    TEXT NOT NULL DEFAULT 'unclear',
    summary             TEXT NOT NULL DEFAULT '',
    top_signals         TEXT NOT NULL DEFAULT '[]',
    contradictions      TEXT NOT NULL DEFAULT '[]',
    relevant_markets    TEXT NOT NULL DEFAULT '[]',
    confidence          TEXT NOT NULL DEFAULT 'low',
    confidence_reason   TEXT NOT NULL DEFAULT '',
    generated_at        INTEGER NOT NULL,
    FOREIGN KEY (user_topic_id) REFERENCES user_topics(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_topics_user ON user_topics(user_id);
CREATE INDEX IF NOT EXISTS idx_topic_preds ON user_topic_predictions(user_topic_id);
CREATE INDEX IF NOT EXISTS idx_topic_analyses ON user_topic_analyses(user_topic_id);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        # Lightweight migrations: add columns that were introduced after the
        # original schema shipped. SQLite doesn't support IF NOT EXISTS on
        # ALTER TABLE, so we probe PRAGMA table_info and only add when missing.
        existing_cols = {row["name"] for row in c.execute("PRAGMA table_info(users)")}
        if "default_dashboard" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN default_dashboard TEXT")
        if "suspended" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN suspended INTEGER NOT NULL DEFAULT 0")
        if "invite_token_id" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN invite_token_id INTEGER REFERENCES invite_tokens(id)")
        # invite_tokens migrations
        invite_cols = {row["name"] for row in c.execute("PRAGMA table_info(invite_tokens)")}
        if "target_email" not in invite_cols:
            c.execute("ALTER TABLE invite_tokens ADD COLUMN target_email TEXT")
        if "username" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN username TEXT")
            # Backfill: set username to email local part for existing users
            for row in c.execute("SELECT id, email FROM users WHERE username IS NULL").fetchall():
                uname = row[1].split("@")[0] if row[1] else f"user{row[0]}"
                c.execute("UPDATE users SET username = ? WHERE id = ?", (uname, row[0]))
        # Trading add-on fields
        if "trading_addon_active" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN trading_addon_active INTEGER NOT NULL DEFAULT 0")
        if "trading_addon_period_end" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN trading_addon_period_end INTEGER")
        # CSRF token columns on sessions
        session_cols = {row["name"] for row in c.execute("PRAGMA table_info(sessions)")}
        if "csrf_token" not in session_cols:
            c.execute("ALTER TABLE sessions ADD COLUMN csrf_token TEXT")
        if "csrf_created_at" not in session_cols:
            c.execute("ALTER TABLE sessions ADD COLUMN csrf_created_at INTEGER")
        # ── Newsletter waitlist columns (pre-release referral mechanic) ──
        # Each subscriber has a `referral_code` (random 8-char) used as a
        # share link identifier, and an optional `referred_by` column that
        # stores the *inviter's* referral_code. Position on the waitlist is
        # computed from the insertion order minus 5× successful referrals
        # so inviters get bumped up when new signups arrive via their link.
        newsletter_cols = {row["name"] for row in c.execute("PRAGMA table_info(newsletter_subscribers)")}
        if "referral_code" not in newsletter_cols:
            c.execute("ALTER TABLE newsletter_subscribers ADD COLUMN referral_code TEXT")
        if "referred_by" not in newsletter_cols:
            c.execute("ALTER TABLE newsletter_subscribers ADD COLUMN referred_by TEXT")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_newsletter_referral_code ON newsletter_subscribers(referral_code)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_newsletter_referred_by ON newsletter_subscribers(referred_by)")
        # Backfill missing codes for any existing rows so /api/newsletter/position
        # never has to return NULL for a share_url.
        for row in c.execute("SELECT id FROM newsletter_subscribers WHERE referral_code IS NULL OR referral_code = ''").fetchall():
            c.execute(
                "UPDATE newsletter_subscribers SET referral_code = ? WHERE id = ?",
                (secrets.token_urlsafe(6)[:8], row[0]),
            )
        # ── Onboarding fields (Feature 4) ─────────────────────────
        if "onboarding_completed" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN onboarding_completed INTEGER NOT NULL DEFAULT 0")
        if "onboarding_completed_at" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN onboarding_completed_at INTEGER")
        if "onboarding_categories" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN onboarding_categories TEXT")
        if "notify_push" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN notify_push INTEGER NOT NULL DEFAULT 0")
        if "notify_email" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN notify_email INTEGER NOT NULL DEFAULT 0")
        if "notify_ev_threshold" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN notify_ev_threshold REAL")
        if "notify_cred_threshold" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN notify_cred_threshold REAL")
        # ── Intelligence add-on fields (Feature 8) ────────────────
        if "intelligence_addon_active" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN intelligence_addon_active INTEGER NOT NULL DEFAULT 0")
        if "intelligence_addon_period_end" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN intelligence_addon_period_end INTEGER")
        # ── Feedback submissions (Feature 5) ──────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS feedback_submissions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
                type            TEXT NOT NULL,
                message         TEXT NOT NULL,
                priority        TEXT,
                page_url        TEXT,
                user_tier       TEXT,
                screenshot_url  TEXT,
                status          TEXT NOT NULL DEFAULT 'open',
                admin_notes     TEXT,
                created_at      INTEGER NOT NULL,
                resolved_at     INTEGER
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback_submissions(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback_submissions(created_at)")
        # ── Analytics events (Feature 6) ──────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS analytics_events (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type           TEXT NOT NULL,
                user_id              INTEGER REFERENCES users(id) ON DELETE SET NULL,
                session_id           TEXT,
                page                 TEXT,
                referrer             TEXT,
                ip_hash              TEXT NOT NULL,
                user_agent_category  TEXT,
                properties           TEXT,
                created_at           INTEGER NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_analytics_type ON analytics_events(event_type)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_analytics_created ON analytics_events(created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_analytics_user ON analytics_events(user_id)")
        # ── Gifted subscriptions (Feature 7) ──────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS gifted_subscriptions (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id               INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                gifted_by_admin_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
                subscription_type     TEXT NOT NULL,
                is_enterprise         INTEGER NOT NULL DEFAULT 0,
                starts_at             INTEGER NOT NULL,
                ends_at               INTEGER,
                is_permanent          INTEGER NOT NULL DEFAULT 0,
                enterprise_config     TEXT,
                internal_notes        TEXT,
                revoked               INTEGER NOT NULL DEFAULT 0,
                revoked_at            INTEGER,
                revoked_by_admin_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at            INTEGER NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_gifts_user ON gifted_subscriptions(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_gifts_active ON gifted_subscriptions(revoked, ends_at)")
        # ── Intelligence conversations (Feature 8) ────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS intelligence_conversations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title           TEXT,
                message_count   INTEGER NOT NULL DEFAULT 0,
                created_at      INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_intel_conv_user ON intelligence_conversations(user_id)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS intelligence_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES intelligence_conversations(id) ON DELETE CASCADE,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                context_used    TEXT,
                tokens_used     INTEGER,
                created_at      INTEGER NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_intel_msg_conv ON intelligence_messages(conversation_id)")
        # ── Rate limiting (persistent across restarts) ────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS rate_limits (
                key             TEXT NOT NULL,
                ts              REAL NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_rate_limits_key_ts ON rate_limits(key, ts)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS login_failures (
                identifier      TEXT NOT NULL,
                ip              TEXT NOT NULL,
                ts              REAL NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_login_failures_id_ip_ts ON login_failures(identifier, ip, ts)")
        # ── User-facing features (saved / follow / snapshots / FTS) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS saved_predictions (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id                  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                prediction_id            INTEGER NOT NULL REFERENCES predictions(id) ON DELETE CASCADE,
                saved_at                 INTEGER NOT NULL,
                notes                    TEXT,
                notified_on_resolution   INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user_id, prediction_id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_saved_user ON saved_predictions(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_saved_prediction ON saved_predictions(prediction_id)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS followed_sources (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id                  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                source_handle            TEXT NOT NULL,
                platform                 TEXT NOT NULL DEFAULT '',
                followed_at              INTEGER NOT NULL,
                notify_on_prediction     INTEGER NOT NULL DEFAULT 0,
                notify_min_credibility   REAL NOT NULL DEFAULT 0.5,
                UNIQUE(user_id, source_handle)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_follow_user ON followed_sources(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_follow_handle ON followed_sources(source_handle)")

        # market_snapshots: dashboard backends push yes_price over time here so
        # the gateway can serve the historical odds chart endpoint. The gateway
        # itself never populates this table outside the snapshot ingestion API.
        c.execute("""
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                market_slug         TEXT NOT NULL,
                market_question     TEXT,
                category            TEXT,
                yes_price           REAL NOT NULL,
                no_price            REAL,
                volume              REAL,
                snapshotted_at      INTEGER NOT NULL,
                source_platform     TEXT NOT NULL DEFAULT 'polymarket'
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_snap_slug_ts ON market_snapshots(market_slug, snapshotted_at)")

        # ── FTS5 virtual tables (rebuilt at startup if missing) ──────
        # Predictions FTS
        c.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS predictions_fts USING fts5(
                content,
                source_handle,
                category,
                content='predictions',
                content_rowid='id',
                tokenize='porter unicode61'
            )
        """)
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS predictions_ai AFTER INSERT ON predictions BEGIN
                INSERT INTO predictions_fts(rowid, content, source_handle, category)
                VALUES (new.id, new.content, new.source_handle, new.category);
            END
        """)
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS predictions_ad AFTER DELETE ON predictions BEGIN
                INSERT INTO predictions_fts(predictions_fts, rowid, content, source_handle, category)
                VALUES ('delete', old.id, old.content, old.source_handle, old.category);
            END
        """)
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS predictions_au AFTER UPDATE ON predictions BEGIN
                INSERT INTO predictions_fts(predictions_fts, rowid, content, source_handle, category)
                VALUES ('delete', old.id, old.content, old.source_handle, old.category);
                INSERT INTO predictions_fts(rowid, content, source_handle, category)
                VALUES (new.id, new.content, new.source_handle, new.category);
            END
        """)
        # Sources FTS
        c.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS sources_fts USING fts5(
                source_handle,
                content='source_credibility',
                content_rowid='id',
                tokenize='porter unicode61'
            )
        """)
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS sources_ai AFTER INSERT ON source_credibility BEGIN
                INSERT INTO sources_fts(rowid, source_handle) VALUES (new.id, new.source_handle);
            END
        """)
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS sources_ad AFTER DELETE ON source_credibility BEGIN
                INSERT INTO sources_fts(sources_fts, rowid, source_handle) VALUES ('delete', old.id, old.source_handle);
            END
        """)
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS sources_au AFTER UPDATE ON source_credibility BEGIN
                INSERT INTO sources_fts(sources_fts, rowid, source_handle) VALUES ('delete', old.id, old.source_handle);
                INSERT INTO sources_fts(rowid, source_handle) VALUES (new.id, new.source_handle);
            END
        """)
        # Markets FTS — backed by market_snapshots
        c.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS markets_fts USING fts5(
                market_slug,
                market_question,
                category,
                content='market_snapshots',
                content_rowid='id',
                tokenize='porter unicode61'
            )
        """)
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS markets_ai AFTER INSERT ON market_snapshots BEGIN
                INSERT INTO markets_fts(rowid, market_slug, market_question, category)
                VALUES (new.id, new.market_slug, new.market_question, new.category);
            END
        """)
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS markets_ad AFTER DELETE ON market_snapshots BEGIN
                INSERT INTO markets_fts(markets_fts, rowid, market_slug, market_question, category)
                VALUES ('delete', old.id, old.market_slug, old.market_question, old.category);
            END
        """)
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS markets_au AFTER UPDATE ON market_snapshots BEGIN
                INSERT INTO markets_fts(markets_fts, rowid, market_slug, market_question, category)
                VALUES ('delete', old.id, old.market_slug, old.market_question, old.category);
                INSERT INTO markets_fts(rowid, market_slug, market_question, category)
                VALUES (new.id, new.market_slug, new.market_question, new.category);
            END
        """)
        # Backfill FTS tables if they were just created and the base tables
        # already have rows (first run after this migration ships).
        existing_pred = c.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] or 0
        fts_pred = c.execute("SELECT COUNT(*) FROM predictions_fts").fetchone()[0] or 0
        if existing_pred > 0 and fts_pred == 0:
            c.execute("INSERT INTO predictions_fts(predictions_fts) VALUES('rebuild')")
        existing_src = c.execute("SELECT COUNT(*) FROM source_credibility").fetchone()[0] or 0
        fts_src = c.execute("SELECT COUNT(*) FROM sources_fts").fetchone()[0] or 0
        if existing_src > 0 and fts_src == 0:
            c.execute("INSERT INTO sources_fts(sources_fts) VALUES('rebuild')")
        existing_mkt = c.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0] or 0
        fts_mkt = c.execute("SELECT COUNT(*) FROM markets_fts").fetchone()[0] or 0
        if existing_mkt > 0 and fts_mkt == 0:
            c.execute("INSERT INTO markets_fts(markets_fts) VALUES('rebuild')")


# ── Rate limiting (persistent) ────────────────────────────────────────────────


def rate_limit_hit(key: str, limit: int, window: int) -> bool:
    """Record a hit and return True if this hit exceeds *limit* within *window* seconds.

    Atomic: GC of old rows, count, insert, all in a single transaction.
    Callers should treat True as "deny this request".
    """
    now = time.time()
    cutoff = now - window
    with conn() as c:
        c.execute("DELETE FROM rate_limits WHERE key = ? AND ts < ?", (key, cutoff))
        row = c.execute("SELECT COUNT(*) AS n FROM rate_limits WHERE key = ? AND ts >= ?", (key, cutoff)).fetchone()
        count = int(row["n"] if row else 0)
        if count >= limit:
            return True
        c.execute("INSERT INTO rate_limits (key, ts) VALUES (?, ?)", (key, now))
    return False


def rate_limit_check(key: str, limit: int, window: int) -> bool:
    """Non-destructive check: return True if *key* has hit *limit* within *window* seconds.
    Does NOT record a new hit. Use for dry-run checks (e.g. middleware early-out).
    """
    now = time.time()
    cutoff = now - window
    with conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM rate_limits WHERE key = ? AND ts >= ?", (key, cutoff)).fetchone()
        return int(row["n"] if row else 0) >= limit


def rate_limit_gc(max_age_seconds: int = 86400) -> int:
    """Garbage-collect rate_limits rows older than max_age_seconds. Returns rows deleted."""
    cutoff = time.time() - max_age_seconds
    with conn() as c:
        cur = c.execute("DELETE FROM rate_limits WHERE ts < ?", (cutoff,))
        return cur.rowcount or 0


def record_login_failure(identifier: str, ip: str) -> None:
    """Log a failed login attempt keyed on (identifier, ip)."""
    with conn() as c:
        c.execute("INSERT INTO login_failures (identifier, ip, ts) VALUES (?, ?, ?)",
                  (identifier.lower(), ip or "unknown", time.time()))


def is_login_locked(identifier: str, ip: str, threshold: int = 5, window: int = 900) -> bool:
    """True if (identifier, ip) pair has >= threshold failures within window seconds.

    Keying on the pair (rather than identifier alone) prevents a remote attacker
    from locking out the victim by spamming failed attempts from another IP.
    """
    cutoff = time.time() - window
    with conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM login_failures WHERE identifier = ? AND ip = ? AND ts >= ?",
            (identifier.lower(), ip or "unknown", cutoff),
        ).fetchone()
        return int(row["n"] if row else 0) >= threshold


def clear_login_failures(identifier: str, ip: str = "") -> None:
    """Clear login failures for identifier. If ip provided, only clear for that ip."""
    with conn() as c:
        if ip:
            c.execute("DELETE FROM login_failures WHERE identifier = ? AND ip = ?", (identifier.lower(), ip))
        else:
            c.execute("DELETE FROM login_failures WHERE identifier = ?", (identifier.lower(),))


def login_failures_gc(max_age_seconds: int = 86400) -> int:
    """Garbage-collect login_failures rows older than max_age_seconds."""
    cutoff = time.time() - max_age_seconds
    with conn() as c:
        cur = c.execute("DELETE FROM login_failures WHERE ts < ?", (cutoff,))
        return cur.rowcount or 0


def delete_sessions_for_user(user_id: int, except_token: str = "") -> int:
    """Delete all sessions for user_id, optionally preserving one by token. Returns rows deleted."""
    with conn() as c:
        if except_token:
            cur = c.execute("DELETE FROM sessions WHERE user_id = ? AND token != ?", (user_id, except_token))
        else:
            cur = c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        return cur.rowcount or 0


# ── Full-text search (SQLite FTS5) ────────────────────────────────────────────


def _fts_sanitize_query(q: str) -> str:
    """Turn an arbitrary user query into a safe FTS5 MATCH string.

    FTS5 has its own query syntax where ``"`` / ``:`` / ``(`` / ``)`` / ``*``
    are metachars. We defend by quoting each whitespace-separated term and
    joining with spaces (implicit AND). A bare ``"`` inside a term becomes
    ``""`` per FTS5 escaping rules.
    """
    q = (q or "").strip()
    if not q:
        return ""
    terms = []
    for raw in q.split():
        # Strip anything FTS5 would treat as a grouping/operator.
        clean = raw.replace('"', '""')
        # Wrap each term in quotes and append the prefix wildcard so
        # "demo" matches "democrats". Wildcards are only valid on bare terms,
        # so we put * outside the quotes.
        terms.append(f'"{clean}" *')
    return " ".join(terms)


def search_predictions(q: str, limit: int = 20) -> list[sqlite3.Row]:
    """FTS5 search against predictions. Joins source_credibility for scoring.

    Results come back with a ``highlight`` column containing the matched
    snippet wrapped in <mark>…</mark> tags (safe to render as HTML because
    FTS5 snippet() escapes non-tag characters itself, but the CALLER MUST
    still html-escape the caller-provided base text).
    """
    match = _fts_sanitize_query(q)
    if not match:
        return []
    with conn() as c:
        try:
            return c.execute(
                """
                SELECT p.id, p.content, p.source_handle, p.category,
                       p.market_id, p.direction, p.predicted_probability,
                       p.extracted_at, p.resolved, p.resolved_correct,
                       sc.global_credibility, sc.accuracy_unlocked,
                       snippet(predictions_fts, 0, '<mark>', '</mark>', '…', 16) AS highlight,
                       bm25(predictions_fts) AS rank
                FROM predictions_fts
                JOIN predictions p ON p.id = predictions_fts.rowid
                LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle
                WHERE predictions_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # Malformed MATCH — return empty rather than 500.
            return []


def search_sources(q: str, limit: int = 20) -> list[sqlite3.Row]:
    """FTS5 search against source handles, enriched with credibility data."""
    match = _fts_sanitize_query(q)
    if not match:
        return []
    with conn() as c:
        try:
            return c.execute(
                """
                SELECT sc.id, sc.source_handle, sc.global_credibility,
                       sc.accuracy_unlocked, sc.total_predictions,
                       sc.correct_predictions, sc.decay_weighted_accuracy,
                       bm25(sources_fts) AS rank
                FROM sources_fts
                JOIN source_credibility sc ON sc.id = sources_fts.rowid
                WHERE sources_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []


def search_markets(q: str, limit: int = 20) -> list[sqlite3.Row]:
    """FTS5 search against market_snapshots (latest per slug)."""
    match = _fts_sanitize_query(q)
    if not match:
        return []
    with conn() as c:
        try:
            return c.execute(
                """
                SELECT ms.market_slug, ms.market_question, ms.category,
                       ms.yes_price, ms.snapshotted_at,
                       snippet(markets_fts, 1, '<mark>', '</mark>', '…', 16) AS highlight,
                       bm25(markets_fts) AS rank
                FROM markets_fts
                JOIN market_snapshots ms ON ms.id = markets_fts.rowid
                WHERE markets_fts MATCH ?
                  AND ms.id = (
                      SELECT MAX(id) FROM market_snapshots
                      WHERE market_slug = ms.market_slug
                  )
                ORDER BY rank
                LIMIT ?
                """,
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []


# ── Saved predictions (watchlist) ─────────────────────────────────────────────


def save_prediction(user_id: int, prediction_id: int, notes: Optional[str] = None) -> int:
    """Insert-or-return-existing saved_predictions row. Returns the row id."""
    with conn() as c:
        # Ensure the prediction actually exists (FK would catch it, but we
        # prefer a clean 404 at the API layer).
        exists = c.execute("SELECT 1 FROM predictions WHERE id = ?", (prediction_id,)).fetchone()
        if not exists:
            return 0
        try:
            cur = c.execute(
                "INSERT INTO saved_predictions (user_id, prediction_id, saved_at, notes) "
                "VALUES (?, ?, ?, ?)",
                (user_id, prediction_id, int(time.time()), notes),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            # Already saved — return the existing id
            row = c.execute(
                "SELECT id FROM saved_predictions WHERE user_id = ? AND prediction_id = ?",
                (user_id, prediction_id),
            ).fetchone()
            return row["id"] if row else 0


def unsave_prediction(user_id: int, prediction_id: int) -> bool:
    with conn() as c:
        cur = c.execute(
            "DELETE FROM saved_predictions WHERE user_id = ? AND prediction_id = ?",
            (user_id, prediction_id),
        )
        return bool(cur.rowcount)


def is_prediction_saved(user_id: int, prediction_id: int) -> bool:
    with conn() as c:
        row = c.execute(
            "SELECT 1 FROM saved_predictions WHERE user_id = ? AND prediction_id = ?",
            (user_id, prediction_id),
        ).fetchone()
        return row is not None


def list_saved_predictions(
    user_id: int,
    resolved_filter: str = "all",  # all | active | correct | incorrect
    sort: str = "saved_at",         # saved_at | credibility | resolution_date
) -> list[sqlite3.Row]:
    where = ["sp.user_id = ?"]
    params: list = [user_id]
    if resolved_filter == "active":
        where.append("p.resolved = 0")
    elif resolved_filter == "correct":
        where.append("p.resolved = 1 AND p.resolved_correct = 1")
    elif resolved_filter == "incorrect":
        where.append("p.resolved = 1 AND p.resolved_correct = 0")
    order = {
        "saved_at": "sp.saved_at DESC",
        "credibility": "sc.global_credibility DESC NULLS LAST, sp.saved_at DESC",
        "resolution_date": "p.resolved_at DESC NULLS LAST, sp.saved_at DESC",
    }.get(sort, "sp.saved_at DESC")
    sql = (
        "SELECT sp.id AS saved_id, sp.saved_at, sp.notes, sp.notified_on_resolution, "
        "p.id AS prediction_id, p.content, p.source_handle, p.category, "
        "p.market_id, p.direction, p.predicted_probability, p.source_url, "
        "p.extracted_at, p.resolved, p.resolved_correct, p.resolved_at, "
        "sc.global_credibility, sc.accuracy_unlocked "
        "FROM saved_predictions sp "
        "JOIN predictions p ON p.id = sp.prediction_id "
        "LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY {order}"
    )
    with conn() as c:
        return c.execute(sql, tuple(params)).fetchall()


def update_saved_prediction_notes(user_id: int, prediction_id: int, notes: Optional[str]) -> bool:
    with conn() as c:
        cur = c.execute(
            "UPDATE saved_predictions SET notes = ? WHERE user_id = ? AND prediction_id = ?",
            (notes, user_id, prediction_id),
        )
        return bool(cur.rowcount)


def saved_prediction_ids_for_user(user_id: int) -> set[int]:
    """Return the set of prediction ids saved by this user — small query used
    by the feed to annotate rows with their saved-state without an N+1."""
    with conn() as c:
        rows = c.execute(
            "SELECT prediction_id FROM saved_predictions WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return {r["prediction_id"] for r in rows}


def saved_predictions_pending_resolution_notification(user_id: int) -> list[sqlite3.Row]:
    """Return saved predictions whose underlying prediction just resolved and
    haven't yet been flagged as notified. Notification jobs mark them seen."""
    with conn() as c:
        return c.execute(
            "SELECT sp.id AS saved_id, sp.prediction_id, p.resolved_correct, "
            "p.content, p.source_handle "
            "FROM saved_predictions sp "
            "JOIN predictions p ON p.id = sp.prediction_id "
            "WHERE sp.user_id = ? AND p.resolved = 1 AND sp.notified_on_resolution = 0",
            (user_id,),
        ).fetchall()


def mark_saved_prediction_notified(saved_id: int) -> None:
    with conn() as c:
        c.execute(
            "UPDATE saved_predictions SET notified_on_resolution = 1 WHERE id = ?",
            (saved_id,),
        )


# ── Source following ──────────────────────────────────────────────────────────


def follow_source(
    user_id: int,
    source_handle: str,
    platform: str = "",
    notify_on_prediction: bool = False,
    notify_min_credibility: float = 0.5,
) -> int:
    source_handle = source_handle.strip()
    if not source_handle:
        return 0
    with conn() as c:
        try:
            cur = c.execute(
                "INSERT INTO followed_sources (user_id, source_handle, platform, followed_at, "
                "notify_on_prediction, notify_min_credibility) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, source_handle, platform, int(time.time()),
                 1 if notify_on_prediction else 0, float(notify_min_credibility)),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            row = c.execute(
                "SELECT id FROM followed_sources WHERE user_id = ? AND source_handle = ?",
                (user_id, source_handle),
            ).fetchone()
            return row["id"] if row else 0


def unfollow_source(user_id: int, source_handle: str) -> bool:
    with conn() as c:
        cur = c.execute(
            "DELETE FROM followed_sources WHERE user_id = ? AND source_handle = ?",
            (user_id, source_handle),
        )
        return bool(cur.rowcount)


def is_following_source(user_id: int, source_handle: str) -> bool:
    with conn() as c:
        row = c.execute(
            "SELECT 1 FROM followed_sources WHERE user_id = ? AND source_handle = ?",
            (user_id, source_handle),
        ).fetchone()
        return row is not None


def update_follow_preferences(
    user_id: int,
    source_handle: str,
    notify_on_prediction: bool,
    notify_min_credibility: float,
) -> bool:
    with conn() as c:
        cur = c.execute(
            "UPDATE followed_sources SET notify_on_prediction = ?, notify_min_credibility = ? "
            "WHERE user_id = ? AND source_handle = ?",
            (1 if notify_on_prediction else 0, float(notify_min_credibility),
             user_id, source_handle),
        )
        return bool(cur.rowcount)


def list_followed_sources(user_id: int) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT fs.id, fs.source_handle, fs.platform, fs.followed_at, "
            "fs.notify_on_prediction, fs.notify_min_credibility, "
            "sc.global_credibility, sc.accuracy_unlocked, sc.total_predictions "
            "FROM followed_sources fs "
            "LEFT JOIN source_credibility sc ON sc.source_handle = fs.source_handle "
            "WHERE fs.user_id = ? ORDER BY fs.followed_at DESC",
            (user_id,),
        ).fetchall()


def followed_source_handles(user_id: int) -> set[str]:
    """Small query used by feed-ranking code in dashboard backends."""
    with conn() as c:
        rows = c.execute(
            "SELECT source_handle FROM followed_sources WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return {r["source_handle"] for r in rows}


# ── Market snapshots (historical odds) ────────────────────────────────────────


def insert_market_snapshot(
    market_slug: str,
    yes_price: float,
    snapshotted_at: Optional[int] = None,
    market_question: Optional[str] = None,
    category: Optional[str] = None,
    no_price: Optional[float] = None,
    volume: Optional[float] = None,
    source_platform: str = "polymarket",
) -> int:
    """Insert a new market snapshot.

    If market_question or category is omitted, we backfill from the most
    recent snapshot for this slug — dashboard backends typically only send
    the question on the first ingest and push price-only updates after that.
    Without this backfill the FTS index would only contain the first row,
    and the "latest snapshot per slug" filter in search_markets() would
    yield zero hits once a price update arrives.
    """
    slug = market_slug.strip()
    ts = snapshotted_at if snapshotted_at is not None else int(time.time())
    with conn() as c:
        if market_question is None or category is None:
            prev = c.execute(
                "SELECT market_question, category FROM market_snapshots "
                "WHERE market_slug = ? ORDER BY snapshotted_at DESC LIMIT 1",
                (slug,),
            ).fetchone()
            if prev:
                if market_question is None:
                    market_question = prev["market_question"]
                if category is None:
                    category = prev["category"]
        cur = c.execute(
            "INSERT INTO market_snapshots (market_slug, market_question, category, "
            "yes_price, no_price, volume, snapshotted_at, source_platform) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (slug, market_question, category,
             float(yes_price), no_price, volume, ts, source_platform),
        )
        return cur.lastrowid


def get_market_history(market_slug: str, limit: int = 500) -> list[sqlite3.Row]:
    """Snapshots for a market ordered ascending by time — suitable for charting."""
    with conn() as c:
        return c.execute(
            "SELECT yes_price, snapshotted_at, volume FROM market_snapshots "
            "WHERE market_slug = ? ORDER BY snapshotted_at ASC LIMIT ?",
            (market_slug.strip(), limit),
        ).fetchall()


def get_latest_market_snapshot(market_slug: str) -> Optional[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM market_snapshots WHERE market_slug = ? "
            "ORDER BY snapshotted_at DESC LIMIT 1",
            (market_slug.strip(),),
        ).fetchone()


def get_market_snapshot_at(market_slug: str, at_time: int) -> Optional[sqlite3.Row]:
    """Return the snapshot closest to at_time (<=) for market_slug, or None.

    Used to annotate prediction markers with "market odds at the time this
    prediction was made".
    """
    with conn() as c:
        return c.execute(
            "SELECT yes_price, snapshotted_at FROM market_snapshots "
            "WHERE market_slug = ? AND snapshotted_at <= ? "
            "ORDER BY snapshotted_at DESC LIMIT 1",
            (market_slug.strip(), int(at_time)),
        ).fetchone()


def get_prediction_markers_for_market(market_slug: str) -> list[sqlite3.Row]:
    """Predictions tied to this market, joined with credibility + nearest snapshot.

    Used by the historical odds chart as the marker layer.
    """
    with conn() as c:
        return c.execute(
            """
            SELECT p.id, p.source_handle, p.content, p.direction,
                   p.predicted_probability, p.extracted_at,
                   sc.global_credibility,
                   (
                     SELECT yes_price FROM market_snapshots
                     WHERE market_slug = ? AND snapshotted_at <= p.extracted_at
                     ORDER BY snapshotted_at DESC LIMIT 1
                   ) AS market_yes_price_at_time
            FROM predictions p
            LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle
            WHERE p.market_id = ?
            ORDER BY p.extracted_at ASC
            """,
            (market_slug.strip(), market_slug.strip()),
        ).fetchall()


# ── Password hashing ──────────────────────────────────────────────────────────
# Using PBKDF2-HMAC-SHA256 (stdlib, no external deps). 200k iterations.


def _hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return dk.hex(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    candidate, _ = _hash_password(password, salt)
    return hmac.compare_digest(candidate, stored_hash)


# ── User operations ───────────────────────────────────────────────────────────


def create_user(email: str, password: str, username: str = "", is_admin: bool = False, admin_level: int = 0) -> int:
    email = email.lower().strip()
    username = username.strip()
    if not username:
        username = email.split("@")[0]
    level = admin_level if admin_level else (1 if is_admin else 0)
    pwd_hash, salt = _hash_password(password)
    with conn() as c:
        cur = c.execute(
            "INSERT INTO users (username, email, password_hash, password_salt, created_at, is_admin) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (username, email, pwd_hash, salt, int(time.time()), level),
        )
        return cur.lastrowid


def get_user_by_email(email: str) -> Optional[sqlite3.Row]:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
        ).fetchone()
    return row


def get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM users WHERE username = ?", (username.strip(),)
        ).fetchone()


def get_user_by_email_or_username(identifier: str) -> Optional[sqlite3.Row]:
    """Look up a user by email or username."""
    identifier = identifier.strip()
    if "@" in identifier:
        return get_user_by_email(identifier)
    return get_user_by_username(identifier)


def get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def set_default_dashboard(user_id: int, dashboard_key: Optional[str]) -> None:
    """Store the user's preferred landing dashboard (or clear it with None)."""
    with conn() as c:
        c.execute(
            "UPDATE users SET default_dashboard = ? WHERE id = ?",
            (dashboard_key, user_id),
        )


def get_default_dashboard(user_id: int) -> Optional[str]:
    with conn() as c:
        row = c.execute(
            "SELECT default_dashboard FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return row["default_dashboard"] if row else None


# ── Session operations ────────────────────────────────────────────────────────

SESSION_TTL = 90 * 24 * 60 * 60  # 90 days (3 months)


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(48)
    now = int(time.time())
    with conn() as c:
        c.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now, now + SESSION_TTL),
        )
    return token


def get_session(token: str) -> Optional[sqlite3.Row]:
    if not token:
        return None
    with conn() as c:
        row = c.execute(
            "SELECT s.*, u.username, u.email, u.is_admin FROM sessions s "
            "JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ? AND s.expires_at > ?",
            (token, int(time.time())),
        ).fetchone()
    return row


def delete_session(token: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))


def purge_expired_sessions() -> int:
    with conn() as c:
        cur = c.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))
        return cur.rowcount


def set_session_csrf(session_token: str, csrf_token: str) -> None:
    """Store a CSRF token in the session row."""
    with conn() as c:
        c.execute(
            "UPDATE sessions SET csrf_token = ?, csrf_created_at = ? WHERE token = ?",
            (csrf_token, int(time.time()), session_token),
        )


def get_session_csrf(session_token: str) -> Optional[dict]:
    """Get the CSRF token and creation time for a session."""
    if not session_token:
        return None
    with conn() as c:
        row = c.execute(
            "SELECT csrf_token, csrf_created_at FROM sessions WHERE token = ? AND expires_at > ?",
            (session_token, int(time.time())),
        ).fetchone()
    if not row or not row["csrf_token"]:
        return None
    return {"csrf_token": row["csrf_token"], "csrf_created_at": row["csrf_created_at"]}


def clear_session_csrf(session_token: str) -> None:
    """Clear the CSRF token from a session (e.g. on logout)."""
    with conn() as c:
        c.execute(
            "UPDATE sessions SET csrf_token = NULL, csrf_created_at = NULL WHERE token = ?",
            (session_token,),
        )


# ── Subscription operations ───────────────────────────────────────────────────


def list_subscriptions(user_id: int) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)
        ).fetchall()


def has_active_subscription(user_id: int, dashboard_key: str) -> bool:
    now = int(time.time())
    with conn() as c:
        # Admins bypass subscription checks for all dashboards.
        admin_row = c.execute(
            "SELECT is_admin FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if admin_row and admin_row[0]:
            return True
        row = c.execute(
            "SELECT id FROM subscriptions "
            "WHERE user_id = ? AND dashboard_key = ? AND status = 'active' "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (user_id, dashboard_key, now),
        ).fetchone()
    return row is not None


def upsert_subscription(
    user_id: int,
    dashboard_key: str,
    plan: str,
    duration_days: Optional[int] = None,
    source: str = "placeholder",
    stripe_sub_id: Optional[str] = None,
) -> None:
    now = int(time.time())
    expires_at = now + duration_days * 86400 if duration_days else None
    with conn() as c:
        c.execute(
            """
            INSERT INTO subscriptions
                (user_id, dashboard_key, plan, status, started_at, expires_at, stripe_sub_id, source)
            VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
            ON CONFLICT(user_id, dashboard_key) DO UPDATE SET
                plan        = excluded.plan,
                status      = 'active',
                started_at  = excluded.started_at,
                expires_at  = excluded.expires_at,
                stripe_sub_id = excluded.stripe_sub_id,
                source      = excluded.source
            """,
            (user_id, dashboard_key, plan, now, expires_at, stripe_sub_id, source),
        )


def cancel_subscription(user_id: int, dashboard_key: str) -> None:
    with conn() as c:
        c.execute(
            "UPDATE subscriptions SET status = 'cancelled' "
            "WHERE user_id = ? AND dashboard_key = ?",
            (user_id, dashboard_key),
        )


# ── Invite token operations ──────────────────────────────────────────────────


def generate_invite_token() -> str:
    """Generate a 32-character URL-safe random invite token."""
    return secrets.token_urlsafe(24)


def create_invite_token(note: str = "", target_email: str = "") -> str:
    """Create a new unclaimed invite token. Returns the token string."""
    token = generate_invite_token()
    with conn() as c:
        c.execute(
            "INSERT INTO invite_tokens (token, status, note, target_email, created_at) VALUES (?, 'unclaimed', ?, ?, ?)",
            (token, note, target_email.strip() or None, int(time.time())),
        )
    return token


def get_invite_token(token: str) -> Optional[sqlite3.Row]:
    token = token.strip()
    with conn() as c:
        return c.execute("SELECT * FROM invite_tokens WHERE token = ?", (token,)).fetchone()


def claim_invite_token(token_str: str, user_id: int, email: str) -> bool:
    """Atomically claim a token. Returns True if claimed, False if already claimed (race condition)."""
    token_str = token_str.strip()
    with conn() as c:
        # Atomic: only update if still unclaimed (prevents race condition)
        cur = c.execute(
            "UPDATE invite_tokens SET status = 'claimed', claimed_by_user_id = ?, "
            "claimed_by_email = ?, claimed_at = ? WHERE token = ? AND status = 'unclaimed'",
            (user_id, email, int(time.time()), token_str),
        )
        if cur.rowcount == 0:
            return False  # Token was already claimed by another request
        c.execute("UPDATE users SET invite_token_id = (SELECT id FROM invite_tokens WHERE token = ?) WHERE id = ?",
                   (token_str, user_id))
        return True


def revoke_invite_token(token_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE invite_tokens SET status = 'revoked' WHERE id = ? AND status = 'unclaimed'", (token_id,))


def list_invite_tokens() -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT * FROM invite_tokens ORDER BY created_at DESC").fetchall()


# ── User management (admin) ─────────────────────────────────────────────────


def list_all_users() -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()


def set_user_role(user_id: int, level: int) -> None:
    """Set user role: 0=user, 1=admin, 2=super_admin."""
    with conn() as c:
        c.execute("UPDATE users SET is_admin = ? WHERE id = ?", (level, user_id))


def set_user_admin(user_id: int, is_admin: bool) -> None:
    """Legacy helper — promotes to admin (1) or demotes to user (0)."""
    set_user_role(user_id, 1 if is_admin else 0)


def set_user_suspended(user_id: int, suspended: bool) -> None:
    with conn() as c:
        c.execute("UPDATE users SET suspended = ? WHERE id = ?", (1 if suspended else 0, user_id))
        if suspended:
            # Kill all sessions for this user
            c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def list_all_subscriptions() -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT s.*, u.email, u.username FROM subscriptions s "
            "JOIN users u ON u.id = s.user_id "
            "ORDER BY s.started_at DESC"
        ).fetchall()


def get_revenue_stats() -> dict:
    """Return subscription counts and breakdown by dashboard and plan."""
    now = int(time.time())
    with conn() as c:
        total = c.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        active = c.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE status = 'active' "
            "AND (expires_at IS NULL OR expires_at > ?)", (now,)
        ).fetchone()[0]
        cancelled = c.execute("SELECT COUNT(*) FROM subscriptions WHERE status = 'cancelled'").fetchone()[0]
        expired = c.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE status = 'active' "
            "AND expires_at IS NOT NULL AND expires_at <= ?", (now,)
        ).fetchone()[0]
        # Per-dashboard active counts
        per_dashboard = c.execute(
            "SELECT dashboard_key, plan, COUNT(*) as cnt FROM subscriptions "
            "WHERE status = 'active' AND (expires_at IS NULL OR expires_at > ?) "
            "GROUP BY dashboard_key, plan ORDER BY dashboard_key", (now,)
        ).fetchall()
        return {
            "total": total,
            "active": active,
            "cancelled": cancelled,
            "expired": expired,
            "per_dashboard": per_dashboard,
        }


def create_enquiry(email: str, job_title: str, message: str) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO enquiries (email, job_title, message, created_at) VALUES (?, ?, ?, ?)",
            (email.strip(), job_title.strip(), message.strip(), int(time.time())),
        )
        return cur.lastrowid


def list_enquiries() -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT * FROM enquiries ORDER BY created_at DESC").fetchall()


def get_enquiry_by_id(enquiry_id: int) -> Optional[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT * FROM enquiries WHERE id = ?", (enquiry_id,)).fetchone()


def mark_enquiry_read(enquiry_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE enquiries SET read = 1 WHERE id = ?", (enquiry_id,))


def count_unread_enquiries() -> int:
    with conn() as c:
        row = c.execute("SELECT COUNT(*) FROM enquiries WHERE read = 0").fetchone()
        return row[0] if row else 0


def mask_email(email: str) -> str:
    """Mask email like sh***@gmail.com."""
    if not email or "@" not in email:
        return "***"
    local, domain = email.rsplit("@", 1)
    if len(local) <= 2:
        return f"{local[0]}***@{domain}"
    return f"{local[:2]}***@{domain}"


# ── Password reset operations ────────────────────────────────────────────────

RESET_TTL = 60 * 60  # 1 hour


def create_password_reset(user_id: int) -> str:
    """Create a password reset token (expires in 1 hour). Returns the raw token.

    Stores BOTH the raw `token` (for backwards compatibility with any legacy
    reset link that's still in the wild) AND `token_hash` (Feature 2: at-rest
    hardening — lookups prefer the hash column). When the legacy column is
    eventually removed the migration just drops it.
    """
    token = secrets.token_urlsafe(36)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = int(time.time())
    with conn() as c:
        c.execute(
            "INSERT INTO password_resets (user_id, token, token_hash, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, token, token_hash, now, now + RESET_TTL),
        )
    return token


def get_password_reset(token: str) -> Optional[sqlite3.Row]:
    """Get a valid (not expired, not used) password reset record."""
    if not token:
        return None
    with conn() as c:
        return c.execute(
            "SELECT * FROM password_resets "
            "WHERE token = ? AND used = 0 AND expires_at > ?",
            (token, int(time.time())),
        ).fetchone()


def use_password_reset(token: str) -> bool:
    """Atomically mark a reset token as used. Returns True if successful."""
    with conn() as c:
        cur = c.execute(
            "UPDATE password_resets SET used = 1 WHERE token = ? AND used = 0",
            (token,),
        )
        return cur.rowcount > 0


def purge_expired_resets() -> int:
    """Delete expired or used reset tokens."""
    with conn() as c:
        cur = c.execute(
            "DELETE FROM password_resets WHERE expires_at <= ? OR used = 1",
            (int(time.time()),),
        )
        return cur.rowcount


# ── Newsletter ────────────────────────────────────────────────────────────────


def _new_referral_code() -> str:
    """Generate a short, URL-safe referral code. Collision odds are ~1/10^14
    per code; the caller handles the rare IntegrityError retry.
    """
    return secrets.token_urlsafe(6)[:8]


def subscribe_newsletter(
    email: str,
    source: str = "prerelease",
    referred_by: Optional[str] = None,
) -> dict:
    """Insert or fetch a newsletter row and return waitlist metadata.

    Return shape:
        {
            "is_new": bool,              # False if email already existed
            "referral_code": str,        # always present (backfilled if old row)
            "referred_by": str | None,   # inviter's referral_code, if any
            "position": int,             # 1-indexed waitlist position
        }

    Position is computed as:
        subscriber_rank - 5 * num_successful_referrals
    floored at 1. Rank is the 1-indexed row order by subscribed_at, so
    new signups always start at the back and climb as their link gets used.

    The referred_by argument must match an existing subscriber's
    referral_code — invalid values are silently ignored so a malformed
    ?ref= never 500s the signup form.
    """
    email = (email or "").strip().lower()
    now = int(time.time())

    # Normalise the inviter code: only accept exact matches on an existing row.
    inviter_code: Optional[str] = None
    if referred_by:
        referred_by = referred_by.strip()
        if referred_by:
            with conn() as c:
                row = c.execute(
                    "SELECT 1 FROM newsletter_subscribers WHERE referral_code = ? LIMIT 1",
                    (referred_by,),
                ).fetchone()
                if row:
                    inviter_code = referred_by

    with conn() as c:
        existing = c.execute(
            "SELECT id, referral_code FROM newsletter_subscribers WHERE email = ?",
            (email,),
        ).fetchone()

        if existing:
            # Idempotent re-signup — don't touch source/referred_by, just
            # return the current position so the UI shows the same number.
            ref_code = existing["referral_code"]
            if not ref_code:
                # Defensive: backfill if init_db's migration missed a row.
                ref_code = _new_referral_code()
                c.execute(
                    "UPDATE newsletter_subscribers SET referral_code = ? WHERE id = ?",
                    (ref_code, existing["id"]),
                )
            return {
                "is_new": False,
                "referral_code": ref_code,
                "referred_by": None,
                "position": _waitlist_position(c, ref_code),
            }

        # New signup — retry on the rare referral_code collision.
        ref_code = _new_referral_code()
        for _ in range(5):
            try:
                c.execute(
                    "INSERT INTO newsletter_subscribers "
                    "(email, subscribed_at, source, referral_code, referred_by) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (email, now, source, ref_code, inviter_code),
                )
                break
            except sqlite3.IntegrityError as exc:
                if "referral_code" in str(exc):
                    ref_code = _new_referral_code()
                    continue
                # Email unique conflict under concurrent signup — treat as
                # re-signup on the next SELECT below.
                existing = c.execute(
                    "SELECT id, referral_code FROM newsletter_subscribers WHERE email = ?",
                    (email,),
                ).fetchone()
                if existing:
                    return {
                        "is_new": False,
                        "referral_code": existing["referral_code"] or ref_code,
                        "referred_by": None,
                        "position": _waitlist_position(c, existing["referral_code"] or ref_code),
                    }
                raise

        return {
            "is_new": True,
            "referral_code": ref_code,
            "referred_by": inviter_code,
            "position": _waitlist_position(c, ref_code),
        }


def _waitlist_position(c, referral_code: str) -> int:
    """Return this subscriber's 1-indexed position on the waitlist.

    Rank = number of subscribers who signed up at-or-before this one
    (ordered by subscribed_at, tie-broken by id). Each successful referral
    the subscriber has made bumps them forward by 5 slots. Floor at 1 so
    nobody gets a zero or negative number.
    """
    row = c.execute(
        "SELECT id, subscribed_at FROM newsletter_subscribers WHERE referral_code = ?",
        (referral_code,),
    ).fetchone()
    if not row:
        # Total count as a safe fallback — caller shouldn't see this path.
        total = c.execute("SELECT COUNT(*) FROM newsletter_subscribers").fetchone()[0]
        return max(1, total)

    rank = c.execute(
        "SELECT COUNT(*) FROM newsletter_subscribers "
        "WHERE subscribed_at < ? OR (subscribed_at = ? AND id <= ?)",
        (row["subscribed_at"], row["subscribed_at"], row["id"]),
    ).fetchone()[0]

    referrals = c.execute(
        "SELECT COUNT(*) FROM newsletter_subscribers WHERE referred_by = ?",
        (referral_code,),
    ).fetchone()[0]

    return max(1, rank - 5 * referrals)


def get_newsletter_position(email: str) -> Optional[dict]:
    """Look up an existing subscriber's current waitlist position.

    Returns None if the email isn't on the waitlist. Used by
    /api/newsletter/position so returning visitors can see their current
    rank after their link has been used.
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    with conn() as c:
        row = c.execute(
            "SELECT referral_code, referred_by FROM newsletter_subscribers WHERE email = ?",
            (email,),
        ).fetchone()
        if not row:
            return None
        ref_code = row["referral_code"]
        if not ref_code:
            # Backfill lazily so subsequent position calls are stable.
            ref_code = _new_referral_code()
            c.execute(
                "UPDATE newsletter_subscribers SET referral_code = ? WHERE email = ?",
                (ref_code, email),
            )
        return {
            "is_new": False,
            "referral_code": ref_code,
            "referred_by": row["referred_by"],
            "position": _waitlist_position(c, ref_code),
        }


# ── Market credential operations ──────────────────────────────────────────


def upsert_market_credential(
    user_id: int,
    source: str,
    *,
    kalshi_token: Optional[str] = None,
    kalshi_member_id: Optional[str] = None,
    kalshi_token_expires_at: Optional[int] = None,
    polymarket_wallet_address: Optional[str] = None,
) -> None:
    """Insert or update market credentials for a user/source pair. Always
    marks the row is_active=1 so reconnecting after an expiry reactivates."""
    now = int(time.time())
    with conn() as c:
        c.execute(
            """
            INSERT INTO user_market_credentials
                (user_id, source, kalshi_token, kalshi_member_id, kalshi_token_expires_at,
                 polymarket_wallet_address, connected_at, last_used_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(user_id, source) DO UPDATE SET
                kalshi_token = excluded.kalshi_token,
                kalshi_member_id = excluded.kalshi_member_id,
                kalshi_token_expires_at = excluded.kalshi_token_expires_at,
                polymarket_wallet_address = excluded.polymarket_wallet_address,
                connected_at = excluded.connected_at,
                last_used_at = excluded.last_used_at,
                is_active = 1
            """,
            (user_id, source, kalshi_token, kalshi_member_id,
             kalshi_token_expires_at, polymarket_wallet_address, now, now),
        )


def get_market_credential(user_id: int, source: str) -> Optional[sqlite3.Row]:
    """Get stored market credentials for a user/source."""
    with conn() as c:
        return c.execute(
            "SELECT * FROM user_market_credentials WHERE user_id = ? AND source = ?",
            (user_id, source),
        ).fetchone()


def get_all_market_credentials(user_id: int) -> list[sqlite3.Row]:
    """Get all market credentials for a user."""
    with conn() as c:
        return c.execute(
            "SELECT * FROM user_market_credentials WHERE user_id = ?",
            (user_id,),
        ).fetchall()


def delete_market_credential(user_id: int, source: str) -> bool:
    """Delete market credentials. Returns True if a row was deleted."""
    with conn() as c:
        cur = c.execute(
            "DELETE FROM user_market_credentials WHERE user_id = ? AND source = ?",
            (user_id, source),
        )
        return cur.rowcount > 0


def update_market_credential_last_used(user_id: int, source: str) -> None:
    """Touch the last_used_at timestamp."""
    with conn() as c:
        c.execute(
            "UPDATE user_market_credentials SET last_used_at = ? WHERE user_id = ? AND source = ?",
            (int(time.time()), user_id, source),
        )


# ── Bet history operations ──────────────────────────────────────────────────


def record_bet(
    user_id: int,
    source: str,
    external_order_id: str,
    market_id: str,
    market_title: str,
    side: str,
    amount_usd: float,
    price_at_bet: float,
    status: str = "pending",
) -> int:
    """Record a bet in history. Returns the row ID."""
    with conn() as c:
        cur = c.execute(
            "INSERT INTO user_bet_history "
            "(user_id, source, external_order_id, market_id, market_title, side, amount_usd, price_at_bet, status, placed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, source, external_order_id, market_id, market_title,
             side, amount_usd, price_at_bet, status, int(time.time())),
        )
        return cur.lastrowid


def list_bet_history(user_id: int, limit: int = 50) -> list[sqlite3.Row]:
    """Get recent bet history for a user."""
    with conn() as c:
        return c.execute(
            "SELECT * FROM user_bet_history WHERE user_id = ? ORDER BY placed_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


# ── Market connection activation ───────────────────────────────────────────


def set_market_credential_active(user_id: int, source: str, active: bool) -> None:
    """Flip is_active on a user's market connection without deleting the row.

    Used when upstream credentials expire (e.g. Kalshi 401) so the UI can
    prompt for a reconnect instead of silently dropping the account."""
    with conn() as c:
        c.execute(
            "UPDATE user_market_credentials SET is_active = ? WHERE user_id = ? AND source = ?",
            (1 if active else 0, user_id, source),
        )


def disconnect_market_credential(user_id: int, source: str) -> bool:
    """User-initiated disconnect. Keep the row so the UI can show
    'Reconnect', but scrub the Kalshi token and mark the row inactive.
    Returns True if a row was updated."""
    with conn() as c:
        cur = c.execute(
            "UPDATE user_market_credentials "
            "SET is_active = 0, kalshi_token = NULL, kalshi_token_expires_at = NULL "
            "WHERE user_id = ? AND source = ?",
            (user_id, source),
        )
        return cur.rowcount > 0


# ── User positions (Polymarket + Kalshi snapshots) ────────────────────────


def upsert_user_position(
    user_id: int,
    platform: str,
    market_id: str,
    market_title: str,
    side: str,
    shares: float,
    avg_entry_price: float,
    current_price: float,
    unrealised_pnl: float,
    position_value_usd: float,
) -> None:
    now = int(time.time())
    with conn() as c:
        c.execute(
            """
            INSERT INTO user_positions
                (user_id, platform, market_id, market_title, side, shares,
                 avg_entry_price, current_price, unrealised_pnl,
                 position_value_usd, last_synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, platform, market_id, side) DO UPDATE SET
                market_title       = excluded.market_title,
                shares             = excluded.shares,
                avg_entry_price    = excluded.avg_entry_price,
                current_price      = excluded.current_price,
                unrealised_pnl     = excluded.unrealised_pnl,
                position_value_usd = excluded.position_value_usd,
                last_synced_at     = excluded.last_synced_at
            """,
            (user_id, platform, market_id, market_title, side, shares,
             avg_entry_price, current_price, unrealised_pnl,
             position_value_usd, now),
        )


def get_user_positions(
    user_id: int, platform: Optional[str] = None,
) -> list[sqlite3.Row]:
    with conn() as c:
        if platform:
            return c.execute(
                "SELECT * FROM user_positions WHERE user_id = ? AND platform = ? "
                "ORDER BY position_value_usd DESC",
                (user_id, platform),
            ).fetchall()
        return c.execute(
            "SELECT * FROM user_positions WHERE user_id = ? "
            "ORDER BY position_value_usd DESC",
            (user_id,),
        ).fetchall()


def delete_user_positions(user_id: int, platform: Optional[str] = None) -> int:
    """Drop cached positions. Platform-scoped if given. Returns rows deleted."""
    with conn() as c:
        if platform:
            cur = c.execute(
                "DELETE FROM user_positions WHERE user_id = ? AND platform = ?",
                (user_id, platform),
            )
        else:
            cur = c.execute(
                "DELETE FROM user_positions WHERE user_id = ?", (user_id,),
            )
        return cur.rowcount


def prune_stale_positions(
    user_id: int, platform: str, keep_keys: set[tuple[str, str]],
) -> int:
    """Delete rows for a platform that are NOT in *keep_keys* (set of
    (market_id, side) tuples). Used after a sync to drop positions the
    exchange no longer reports (closed trades)."""
    with conn() as c:
        rows = c.execute(
            "SELECT market_id, side FROM user_positions "
            "WHERE user_id = ? AND platform = ?",
            (user_id, platform),
        ).fetchall()
        to_delete = [
            (r["market_id"], r["side"]) for r in rows
            if (r["market_id"], r["side"]) not in keep_keys
        ]
        for mid, side in to_delete:
            c.execute(
                "DELETE FROM user_positions "
                "WHERE user_id = ? AND platform = ? AND market_id = ? AND side = ?",
                (user_id, platform, mid, side),
            )
        return len(to_delete)


def get_portfolio_stats(user_id: int) -> dict:
    """Aggregate stats across cached positions.

    Value/P&L/active come from user_positions; resolved-bet win rate comes
    from user_bet_history (bets with resolved_correct set)."""
    with conn() as c:
        agg = c.execute(
            "SELECT "
            " COALESCE(SUM(position_value_usd), 0) AS total_value, "
            " COALESCE(SUM(unrealised_pnl), 0)    AS total_pnl, "
            " COUNT(*) AS active "
            "FROM user_positions WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        resolved = c.execute(
            "SELECT "
            " COUNT(*)  AS total, "
            " SUM(CASE WHEN resolved_correct = 1 THEN 1 ELSE 0 END) AS wins "
            "FROM user_bet_history "
            "WHERE user_id = ? AND resolved_correct IS NOT NULL",
            (user_id,),
        ).fetchone()
    total_bets = int(resolved["total"] or 0)
    wins = int(resolved["wins"] or 0)
    win_rate = (wins / total_bets) if total_bets else None
    return {
        "total_value_usd": round(float(agg["total_value"]), 2),
        "unrealised_pnl_usd": round(float(agg["total_pnl"]), 2),
        "active_positions": int(agg["active"]),
        "resolved_bets": total_bets,
        "winning_bets": wins,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
    }


# ── User bankroll (Kelly preferences) ──────────────────────────────────────


def get_user_bankroll(user_id: int) -> dict:
    """Return the user's stated bankroll and Kelly fraction preference."""
    with conn() as c:
        row = c.execute(
            "SELECT bankroll, kelly_fraction FROM users WHERE id = ?", (user_id,),
        ).fetchone()
    if not row:
        return {"bankroll": None, "kelly_fraction": 0.5}
    return {
        "bankroll": float(row["bankroll"]) if row["bankroll"] is not None else None,
        "kelly_fraction": float(row["kelly_fraction"] or 0.5),
    }


def set_user_bankroll(
    user_id: int,
    bankroll: Optional[float] = None,
    kelly_fraction: Optional[float] = None,
) -> None:
    sets: list[str] = []
    params: list = []
    if bankroll is not None:
        sets.append("bankroll = ?")
        params.append(float(bankroll))
    if kelly_fraction is not None:
        sets.append("kelly_fraction = ?")
        params.append(float(kelly_fraction))
    if not sets:
        return
    params.append(user_id)
    with conn() as c:
        c.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", tuple(params))


# ── Trading add-on operations ────────────────────────────────────────────────


def get_trading_addon_status(user_id: int) -> dict:
    """Return trading add-on status for a user."""
    with conn() as c:
        row = c.execute(
            "SELECT trading_addon_active, trading_addon_period_end FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"active": False, "period_end": None}
    active = bool(row["trading_addon_active"])
    period_end = row["trading_addon_period_end"]
    # Check expiry
    if active and period_end and period_end <= int(time.time()):
        active = False
    return {"active": active, "period_end": period_end}


def set_trading_addon(user_id: int, active: bool, period_end: Optional[int] = None) -> None:
    """Admin toggle for trading add-on."""
    with conn() as c:
        c.execute(
            "UPDATE users SET trading_addon_active = ?, trading_addon_period_end = ? WHERE id = ?",
            (1 if active else 0, period_end, user_id),
        )


def has_trading_addon(user_id: int) -> bool:
    """Check if user has active trading add-on (or is admin/enterprise)."""
    with conn() as c:
        row = c.execute(
            "SELECT is_admin, trading_addon_active, trading_addon_period_end FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return False
    if row["is_admin"]:
        return True
    if not row["trading_addon_active"]:
        return False
    period_end = row["trading_addon_period_end"]
    if period_end and period_end <= int(time.time()):
        return False
    return True


# ── Credibility engine operations ────────────────────────────────────────────


def get_source_credibility(source_handle: str) -> Optional[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM source_credibility WHERE source_handle = ?",
            (source_handle,),
        ).fetchone()


def upsert_source_credibility(
    source_handle: str,
    global_credibility: float,
    accuracy_unlocked: bool = False,
    decay_weighted_accuracy: Optional[float] = None,
    total_predictions: int = 0,
    correct_predictions: int = 0,
    categories_active: int = 0,
) -> None:
    now = int(time.time())
    with conn() as c:
        c.execute(
            """INSERT INTO source_credibility
                (source_handle, global_credibility, accuracy_unlocked, decay_weighted_accuracy,
                 total_predictions, correct_predictions, categories_active, last_computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_handle) DO UPDATE SET
                global_credibility = excluded.global_credibility,
                accuracy_unlocked = excluded.accuracy_unlocked,
                decay_weighted_accuracy = excluded.decay_weighted_accuracy,
                total_predictions = excluded.total_predictions,
                correct_predictions = excluded.correct_predictions,
                categories_active = excluded.categories_active,
                last_computed_at = excluded.last_computed_at
            """,
            (source_handle, global_credibility, 1 if accuracy_unlocked else 0,
             decay_weighted_accuracy, total_predictions, correct_predictions,
             categories_active, now),
        )
        # Store snapshot
        c.execute(
            "INSERT INTO credibility_snapshots (source_handle, global_credibility, snapshot_at) VALUES (?, ?, ?)",
            (source_handle, global_credibility, now),
        )


def get_category_credibility(source_handle: str, category: str) -> Optional[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM source_category_credibility WHERE source_handle = ? AND category = ?",
            (source_handle, category),
        ).fetchone()


def get_all_category_credibilities(source_handle: str) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM source_category_credibility WHERE source_handle = ? ORDER BY category",
            (source_handle,),
        ).fetchall()


def upsert_category_credibility(
    source_handle: str, category: str, credibility: float,
    prediction_count: int = 0, correct_count: int = 0,
) -> None:
    now = int(time.time())
    with conn() as c:
        c.execute(
            """INSERT INTO source_category_credibility
                (source_handle, category, category_credibility, prediction_count, correct_count, last_computed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_handle, category) DO UPDATE SET
                category_credibility = excluded.category_credibility,
                prediction_count = excluded.prediction_count,
                correct_count = excluded.correct_count,
                last_computed_at = excluded.last_computed_at
            """,
            (source_handle, category, credibility, prediction_count, correct_count, now),
        )


def get_credibility_snapshots(source_handle: str, limit: int = 5) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM credibility_snapshots WHERE source_handle = ? ORDER BY snapshot_at DESC LIMIT ?",
            (source_handle, limit),
        ).fetchall()


def list_all_source_credibilities() -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT * FROM source_credibility ORDER BY global_credibility DESC").fetchall()


def compute_calibration(source_handle: str) -> Optional[dict]:
    """Compute calibration score for a source (F9).

    Buckets all resolved predictions with a stated probability into 10 bins
    (0-10%, 10-20%, ..., 90-100%). For each bucket, compares the average
    predicted probability to the actual resolution rate.

    Calibration score = 1 - mean(|actual_rate - predicted_avg|) per bucket.
    A perfectly calibrated source scores 1.0.

    Returns None if < 5 calibratable predictions.
    """
    import json as _json

    BUCKETS = [(i / 10, (i + 1) / 10) for i in range(10)]

    with conn() as c:
        preds = c.execute(
            "SELECT predicted_probability, resolved_correct FROM predictions "
            "WHERE source_handle = ? AND resolved = 1 "
            "AND predicted_probability IS NOT NULL AND resolved_correct IS NOT NULL",
            (source_handle,),
        ).fetchall()

    if len(preds) < 5:
        return None

    bucket_data = []
    deviations = []
    for low, high in BUCKETS:
        in_bucket = [p for p in preds if low <= (p["predicted_probability"] or 0) < high]
        if not in_bucket:
            continue
        predicted_avg = sum(p["predicted_probability"] for p in in_bucket) / len(in_bucket)
        actual_rate = sum(1 for p in in_bucket if p["resolved_correct"]) / len(in_bucket)
        deviation = abs(actual_rate - predicted_avg)
        deviations.append(deviation)
        bucket_data.append({
            "range": f"{int(low * 100)}-{int(high * 100)}%",
            "predicted": round(predicted_avg, 3),
            "actual": round(actual_rate, 3),
            "count": len(in_bucket),
        })

    if not deviations:
        return None

    score = round(1 - sum(deviations) / len(deviations), 4)
    now = int(time.time())

    with conn() as c:
        c.execute(
            """INSERT INTO source_calibration
                (source_handle, calibration_score, calibration_data, total_calibrated, last_computed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_handle) DO UPDATE SET
                calibration_score = excluded.calibration_score,
                calibration_data = excluded.calibration_data,
                total_calibrated = excluded.total_calibrated,
                last_computed_at = excluded.last_computed_at
            """,
            (source_handle, score, _json.dumps({"buckets": bucket_data}), len(preds), now),
        )

    return {"calibration_score": score, "buckets": bucket_data, "total_calibrated": len(preds)}


def get_source_calibration(source_handle: str) -> Optional[dict]:
    """Fetch cached calibration data for a source."""
    import json as _json
    with conn() as c:
        row = c.execute(
            "SELECT * FROM source_calibration WHERE source_handle = ?",
            (source_handle,),
        ).fetchone()
    if not row:
        return None
    return {
        "calibration_score": row["calibration_score"],
        "buckets": _json.loads(row["calibration_data"] or "{}").get("buckets", []),
        "total_calibrated": row["total_calibrated"],
        "last_computed_at": row["last_computed_at"],
    }


def recompute_all_credibilities() -> int:
    """Recompute all source credibility scores using Bayesian time-decay.

    For each source with at least one resolved prediction:
      1. Exponential time-decay weighting: recent predictions count more.
         weight = exp(-LAMBDA * age_days), half-life ~69 days.
      2. Decay-weighted accuracy: sum(correct_i * weight_i) / sum(weight_i)
      3. Bayesian smoothing toward a 0.5 prior so new sources with few
         predictions don't swing to 0.0 or 1.0.
      4. Per-category breakdown with the same algorithm.
      5. accuracy_unlocked set True when total resolved >= 10.

    Returns the number of sources recomputed.
    """
    import math

    LAMBDA = 0.01       # decay rate: half-life = ln(2)/0.01 ~ 69 days
    PRIOR = 0.5         # Bayesian prior (uninformed)
    STRENGTH = 10       # prior pseudo-count (strength of regression to PRIOR)
    MIN_FOR_UNLOCK = 10 # minimum resolved predictions to unlock accuracy badge
    now = int(time.time())

    # Get all sources that have at least one resolved prediction.
    with conn() as c:
        source_rows = c.execute(
            "SELECT DISTINCT source_handle FROM predictions "
            "WHERE resolved = 1 AND resolved_correct IS NOT NULL"
        ).fetchall()

    count = 0
    for src_row in source_rows:
        handle = src_row["source_handle"]

        with conn() as c:
            preds = c.execute(
                "SELECT resolved_correct, resolved_at, category "
                "FROM predictions "
                "WHERE source_handle = ? AND resolved = 1 AND resolved_correct IS NOT NULL",
                (handle,),
            ).fetchall()

        if not preds:
            continue

        # ── Global decay-weighted accuracy ──────────────────────────────
        weighted_correct = 0.0
        weight_total = 0.0
        total = len(preds)
        correct = 0

        # Per-category accumulators: cat -> {wc, wt, total, correct}
        cat_data: dict = {}

        for p in preds:
            age_days = max(0, (now - (p["resolved_at"] or now)) / 86400)
            decay = math.exp(-LAMBDA * age_days)
            is_correct = 1 if p["resolved_correct"] else 0
            correct += is_correct

            weighted_correct += is_correct * decay
            weight_total += decay

            cat = p["category"] or "other"
            if cat not in cat_data:
                cat_data[cat] = {"wc": 0.0, "wt": 0.0, "total": 0, "correct": 0}
            cd = cat_data[cat]
            cd["wc"] += is_correct * decay
            cd["wt"] += decay
            cd["total"] += 1
            cd["correct"] += is_correct

        dwa = weighted_correct / weight_total if weight_total > 0 else PRIOR
        # Bayesian smoothing: (n * observation + strength * prior) / (n + strength)
        global_cred = (total * dwa + STRENGTH * PRIOR) / (total + STRENGTH)
        unlocked = total >= MIN_FOR_UNLOCK

        upsert_source_credibility(
            source_handle=handle,
            global_credibility=round(global_cred, 6),
            accuracy_unlocked=unlocked,
            decay_weighted_accuracy=round(dwa, 6),
            total_predictions=total,
            correct_predictions=correct,
            categories_active=len(cat_data),
        )

        # ── Per-category scores ─────────────────────────────────────────
        for cat, cd in cat_data.items():
            cat_dwa = cd["wc"] / cd["wt"] if cd["wt"] > 0 else PRIOR
            cat_cred = (cd["total"] * cat_dwa + STRENGTH * PRIOR) / (cd["total"] + STRENGTH)
            upsert_category_credibility(
                source_handle=handle,
                category=cat,
                credibility=round(cat_cred, 6),
                prediction_count=cd["total"],
                correct_count=cd["correct"],
            )

        # Compute calibration alongside credibility (F9).
        try:
            compute_calibration(handle)
        except Exception:
            pass  # calibration is best-effort; don't fail the whole recompute

        count += 1

    return count


# ── Prediction operations ────────────────────────────────────────────────────


def create_prediction(
    source_handle: str, content: str, category: str = "other",
    market_id: Optional[str] = None, direction: Optional[str] = None,
    predicted_probability: Optional[float] = None, source_url: Optional[str] = None,
) -> int:
    now = int(time.time())
    with conn() as c:
        cur = c.execute(
            "INSERT INTO predictions (source_handle, market_id, category, direction, "
            "predicted_probability, content, source_url, extracted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source_handle, market_id, category, direction,
             predicted_probability, content, source_url, now),
        )
        return cur.lastrowid


def get_unresolved_market_ids() -> list[str]:
    """Return distinct market_ids that have unresolved predictions."""
    with conn() as c:
        rows = c.execute(
            "SELECT DISTINCT market_id FROM predictions "
            "WHERE resolved = 0 AND market_id IS NOT NULL AND market_id != ''"
        ).fetchall()
    return [r["market_id"] for r in rows]


def resolve_predictions_for_market(market_id: str, outcome_yes: bool) -> int:
    """Mark all unresolved predictions for *market_id* as resolved.

    direction == "YES" → resolved_correct = 1 if outcome_yes, else 0
    direction == "NO"  → resolved_correct = 0 if outcome_yes, else 1
    direction == NULL or other → resolved_correct = NULL (unknown)

    Returns the number of rows updated.
    """
    now = int(time.time())
    with conn() as c:
        c.execute(
            "UPDATE predictions SET resolved = 1, resolved_at = ?, "
            "resolved_correct = CASE "
            "  WHEN direction = 'YES' THEN ? "
            "  WHEN direction = 'NO'  THEN ? "
            "  ELSE NULL END "
            "WHERE market_id = ? AND resolved = 0",
            (now, 1 if outcome_yes else 0, 0 if outcome_yes else 1, market_id),
        )
        return c.execute("SELECT changes()").fetchone()[0]


def get_predictions_for_market(market_id: str) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT p.*, sc.global_credibility, sc.accuracy_unlocked, sc.decay_weighted_accuracy, "
            "scc.category_credibility "
            "FROM predictions p "
            "LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle "
            "LEFT JOIN source_category_credibility scc ON scc.source_handle = p.source_handle AND scc.category = p.category "
            "WHERE p.market_id = ? ORDER BY p.extracted_at DESC",
            (market_id,),
        ).fetchall()


def list_recent_predictions(limit: int = 50, category: Optional[str] = None) -> list[sqlite3.Row]:
    with conn() as c:
        if category:
            return c.execute(
                "SELECT p.*, sc.global_credibility, sc.accuracy_unlocked "
                "FROM predictions p "
                "LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle "
                "WHERE p.category = ? ORDER BY p.extracted_at DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        return c.execute(
            "SELECT p.*, sc.global_credibility, sc.accuracy_unlocked "
            "FROM predictions p "
            "LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle "
            "ORDER BY p.extracted_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


# ── Probability calculation ──────────────────────────────────────────────────


def calculate_betyc_probability(predictions: list) -> dict:
    """
    Credibility-weighted average of predicted probabilities.

    For predictions with explicit probability stated by source:
      use that probability, weighted by source category_credibility

    For YES/NO directional predictions without explicit %:
      YES from source with credibility X -> probability = 0.5 + (X - 0.5) * 0.8
      NO from source with credibility X -> probability = 0.5 - (X - 0.5) * 0.8

    Final result clamped to [0.05, 0.95]
    """
    if not predictions:
        return {
            "betyc_yes_probability": None,
            "betyc_no_probability": None,
            "betyc_edge": None,
            "betyc_source_count": 0,
            "betyc_confidence": "Insufficient data",
        }

    weighted_sum = 0.0
    weight_total = 0.0
    qualifying_sources = 0
    accuracy_unlocked_count = 0
    cred_sum = 0.0

    for p in predictions:
        cred = p.get("category_credibility") or p.get("global_credibility") or 0.5
        prob = p.get("predicted_probability")

        if prob is not None:
            weighted_sum += prob * cred
            weight_total += cred
        else:
            direction = (p.get("direction") or "").upper()
            if direction == "YES":
                inferred = 0.5 + (cred - 0.5) * 0.8
            elif direction == "NO":
                inferred = 0.5 - (cred - 0.5) * 0.8
            else:
                continue
            weighted_sum += inferred * cred
            weight_total += cred

        qualifying_sources += 1
        cred_sum += cred
        if p.get("accuracy_unlocked"):
            accuracy_unlocked_count += 1

    if weight_total == 0 or qualifying_sources == 0:
        return {
            "betyc_yes_probability": None,
            "betyc_no_probability": None,
            "betyc_edge": None,
            "betyc_source_count": 0,
            "betyc_confidence": "Insufficient data",
        }

    raw_prob = weighted_sum / weight_total
    clamped = max(0.05, min(0.95, raw_prob))
    avg_cred = cred_sum / qualifying_sources

    # Confidence levels
    if qualifying_sources >= 5 and avg_cred >= 0.6 and accuracy_unlocked_count > qualifying_sources / 2:
        confidence = "High"
    elif qualifying_sources >= 3 or 0.4 <= avg_cred <= 0.6:
        confidence = "Medium"
    elif qualifying_sources >= 1:
        confidence = "Low"
    else:
        confidence = "Insufficient data"

    return {
        "betyc_yes_probability": round(clamped, 4),
        "betyc_no_probability": round(1 - clamped, 4),
        "betyc_edge": None,  # Caller sets this based on market price
        "betyc_source_count": qualifying_sources,
        "betyc_confidence": confidence,
    }


# ── Topic operations (Signal Search) ────────────────────────────────────────


def create_topic(user_id: int, name: str, keywords: list[str], schedule_minutes: int = 60) -> int:
    import json as _json
    now = int(time.time())
    next_pull = now + schedule_minutes * 60
    with conn() as c:
        cur = c.execute(
            "INSERT INTO user_topics (user_id, name, keywords, schedule_minutes, next_pull_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, name, _json.dumps(keywords), schedule_minutes, next_pull, now),
        )
        return cur.lastrowid


def list_topics(user_id: int) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM user_topics WHERE user_id = ? AND is_active = 1 ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()


def get_topic(topic_id: int) -> Optional[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT * FROM user_topics WHERE id = ?", (topic_id,)).fetchone()


def delete_topic(topic_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM user_topic_analyses WHERE user_topic_id = ?", (topic_id,))
        c.execute("DELETE FROM user_topic_predictions WHERE user_topic_id = ?", (topic_id,))
        c.execute("DELETE FROM user_topics WHERE id = ?", (topic_id,))


def count_user_topics(user_id: int) -> int:
    with conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM user_topics WHERE user_id = ? AND is_active = 1",
            (user_id,),
        ).fetchone()
        return row[0] if row else 0


def update_topic_pull(topic_id: int, posts_found: int = 0, predictions_extracted: int = 0) -> None:
    now = int(time.time())
    topic = get_topic(topic_id)
    if not topic:
        return
    schedule = topic["schedule_minutes"] or 60
    with conn() as c:
        c.execute(
            "UPDATE user_topics SET last_pulled_at = ?, next_pull_at = ?, "
            "posts_found_total = posts_found_total + ?, predictions_extracted_total = predictions_extracted_total + ? "
            "WHERE id = ?",
            (now, now + schedule * 60, posts_found, predictions_extracted, topic_id),
        )


def get_due_topics() -> list[sqlite3.Row]:
    """Get topics that are due for a pull (next_pull_at <= now)."""
    now = int(time.time())
    with conn() as c:
        return c.execute(
            "SELECT t.*, u.email FROM user_topics t "
            "JOIN users u ON u.id = t.user_id "
            "WHERE t.is_active = 1 AND t.next_pull_at <= ?",
            (now,),
        ).fetchall()


def add_topic_prediction(topic_id: int, prediction_id: int) -> None:
    now = int(time.time())
    with conn() as c:
        c.execute(
            "INSERT INTO user_topic_predictions (user_topic_id, prediction_id, pulled_at) VALUES (?, ?, ?)",
            (topic_id, prediction_id, now),
        )


def get_topic_predictions(topic_id: int, limit: int = 50) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT p.*, sc.global_credibility, sc.accuracy_unlocked, "
            "scc.category_credibility "
            "FROM user_topic_predictions tp "
            "JOIN predictions p ON p.id = tp.prediction_id "
            "LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle "
            "LEFT JOIN source_category_credibility scc ON scc.source_handle = p.source_handle AND scc.category = p.category "
            "WHERE tp.user_topic_id = ? ORDER BY tp.pulled_at DESC LIMIT ?",
            (topic_id, limit),
        ).fetchall()


def save_topic_analysis(
    topic_id: int, signal_direction: str, summary: str,
    top_signals: list, contradictions: list, relevant_markets: list,
    confidence: str, confidence_reason: str,
) -> int:
    import json as _json
    now = int(time.time())
    with conn() as c:
        cur = c.execute(
            "INSERT INTO user_topic_analyses "
            "(user_topic_id, signal_direction, summary, top_signals, contradictions, "
            "relevant_markets, confidence, confidence_reason, generated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (topic_id, signal_direction, summary, _json.dumps(top_signals),
             _json.dumps(contradictions), _json.dumps(relevant_markets),
             confidence, confidence_reason, now),
        )
        return cur.lastrowid


def get_latest_topic_analysis(topic_id: int) -> Optional[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM user_topic_analyses WHERE user_topic_id = ? ORDER BY generated_at DESC LIMIT 1",
            (topic_id,),
        ).fetchone()


# ── Onboarding (Feature 4) ────────────────────────────────────────────────


def get_onboarding_status(user_id: int) -> dict:
    """Return onboarding state for a user.

    Returns {completed, completed_at, categories, notify_push, notify_email,
             notify_ev_threshold, notify_cred_threshold}.
    """
    import json as _json
    with conn() as c:
        row = c.execute(
            "SELECT onboarding_completed, onboarding_completed_at, onboarding_categories, "
            "notify_push, notify_email, notify_ev_threshold, notify_cred_threshold "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"completed": False, "completed_at": None, "categories": []}
    cats = []
    if row["onboarding_categories"]:
        try:
            cats = _json.loads(row["onboarding_categories"])
        except Exception:
            cats = []
    return {
        "completed": bool(row["onboarding_completed"]),
        "completed_at": row["onboarding_completed_at"],
        "categories": cats,
        "notify_push": bool(row["notify_push"]),
        "notify_email": bool(row["notify_email"]),
        "notify_ev_threshold": row["notify_ev_threshold"],
        "notify_cred_threshold": row["notify_cred_threshold"],
    }


def set_onboarding_categories(user_id: int, categories: list[str]) -> None:
    import json as _json
    with conn() as c:
        c.execute(
            "UPDATE users SET onboarding_categories = ? WHERE id = ?",
            (_json.dumps(categories), user_id),
        )


def set_onboarding_notifications(
    user_id: int,
    push: bool,
    email: bool,
    ev_threshold: Optional[float] = None,
    cred_threshold: Optional[float] = None,
) -> None:
    with conn() as c:
        c.execute(
            "UPDATE users SET notify_push = ?, notify_email = ?, "
            "notify_ev_threshold = ?, notify_cred_threshold = ? WHERE id = ?",
            (1 if push else 0, 1 if email else 0, ev_threshold, cred_threshold, user_id),
        )


def complete_onboarding(user_id: int) -> None:
    with conn() as c:
        c.execute(
            "UPDATE users SET onboarding_completed = 1, onboarding_completed_at = ? WHERE id = ?",
            (int(time.time()), user_id),
        )


# ── Feedback (Feature 5) ──────────────────────────────────────────────────


def create_feedback(
    user_id: Optional[int],
    type_: str,
    message: str,
    priority: Optional[str],
    page_url: Optional[str],
    user_tier: Optional[str],
    screenshot_url: Optional[str] = None,
) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO feedback_submissions "
            "(user_id, type, message, priority, page_url, user_tier, screenshot_url, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (user_id, type_, message, priority, page_url, user_tier, screenshot_url, int(time.time())),
        )
        return cur.lastrowid


def list_feedback(status_filter: Optional[str] = None, limit: int = 200) -> list[sqlite3.Row]:
    with conn() as c:
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
    with conn() as c:
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
    with conn() as c:
        row = c.execute("SELECT COUNT(*) FROM feedback_submissions WHERE status = ?", (status,)).fetchone()
    return row[0] if row else 0


# ── Analytics (Feature 6) ─────────────────────────────────────────────────


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
    with conn() as c:
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
    with conn() as c:
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
        with conn() as c:
            row = c.execute(
                "SELECT COUNT(DISTINCT ip_hash) AS u FROM analytics_events WHERE created_at >= ?",
                (since,),
            ).fetchone()
            out["unique_visitors"] = row["u"] if row else 0
    return out


def get_analytics_users(since: int) -> dict:
    """Growth series — users per day since `since`. Returns totals + a series."""
    with conn() as c:
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
    with conn() as c:
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
    with conn() as c:
        rows = c.execute(
            "SELECT event_type, COUNT(*) AS c FROM analytics_events "
            "WHERE created_at >= ? GROUP BY event_type",
            (since,),
        ).fetchall()
    by_type = {r["event_type"]: r["c"] for r in rows}
    top_markets: dict[str, int] = {}
    top_sources: dict[str, int] = {}
    top_keywords: dict[str, int] = {}
    with conn() as c:
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


# ── Gifted subscriptions (Feature 7) ──────────────────────────────────────


def create_gift(
    user_id: int,
    gifted_by_admin_id: int,
    subscription_type: str,
    ends_at: Optional[int],
    is_permanent: bool,
    is_enterprise: bool = False,
    enterprise_config: Optional[dict] = None,
    internal_notes: Optional[str] = None,
) -> int:
    import json as _json
    with conn() as c:
        cur = c.execute(
            "INSERT INTO gifted_subscriptions "
            "(user_id, gifted_by_admin_id, subscription_type, is_enterprise, starts_at, ends_at, "
            "is_permanent, enterprise_config, internal_notes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                gifted_by_admin_id,
                subscription_type,
                1 if is_enterprise else 0,
                int(time.time()),
                ends_at,
                1 if is_permanent else 0,
                _json.dumps(enterprise_config) if enterprise_config else None,
                internal_notes,
                int(time.time()),
            ),
        )
        return cur.lastrowid


def list_active_gifts() -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT g.*, u.email AS user_email, a.email AS granted_by_email "
            "FROM gifted_subscriptions g "
            "LEFT JOIN users u ON g.user_id = u.id "
            "LEFT JOIN users a ON g.gifted_by_admin_id = a.id "
            "WHERE g.revoked = 0 ORDER BY g.created_at DESC"
        ).fetchall()


def get_user_active_gifts(user_id: int) -> list[sqlite3.Row]:
    now = int(time.time())
    with conn() as c:
        return c.execute(
            "SELECT * FROM gifted_subscriptions "
            "WHERE user_id = ? AND revoked = 0 AND (is_permanent = 1 OR ends_at IS NULL OR ends_at > ?)",
            (user_id, now),
        ).fetchall()


def revoke_gift(gift_id: int, admin_id: int) -> None:
    with conn() as c:
        c.execute(
            "UPDATE gifted_subscriptions SET revoked = 1, revoked_at = ?, revoked_by_admin_id = ? WHERE id = ?",
            (int(time.time()), admin_id, gift_id),
        )


def get_user_intelligence_addon_active(user_id: int) -> bool:
    """True if user has an active Intelligence add-on gift or flag."""
    with conn() as c:
        row = c.execute(
            "SELECT intelligence_addon_active, intelligence_addon_period_end FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row and row["intelligence_addon_active"]:
            if not row["intelligence_addon_period_end"] or row["intelligence_addon_period_end"] > int(time.time()):
                return True
    for g in get_user_active_gifts(user_id):
        if g["subscription_type"] == "intelligence_addon":
            return True
        if g["is_enterprise"] and g["enterprise_config"]:
            import json as _json
            try:
                cfg = _json.loads(g["enterprise_config"])
            except Exception:
                cfg = {}
            if cfg.get("intelligence_addon_included"):
                return True
    return False


def set_user_intelligence_addon(user_id: int, active: bool, period_end: Optional[int] = None) -> None:
    with conn() as c:
        c.execute(
            "UPDATE users SET intelligence_addon_active = ?, intelligence_addon_period_end = ? WHERE id = ?",
            (1 if active else 0, period_end, user_id),
        )


def get_user_subscription_tier(user_id: int) -> str:
    """Best-effort tier label: pro | trader | none (admins map to pro)."""
    with conn() as c:
        admin_row = c.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if admin_row and admin_row["is_admin"]:
            return "pro"
        subs = c.execute(
            "SELECT plan FROM subscriptions WHERE user_id = ? AND status = 'active'",
            (user_id,),
        ).fetchall()
    has_pro = any((s["plan"] or "").startswith("pro") for s in subs)
    has_trader = any((s["plan"] or "").startswith("trader") for s in subs)
    if has_pro:
        return "pro"
    if has_trader or subs:
        return "trader"
    return "none"


# ── Intelligence conversations (Feature 8) ────────────────────────────────


def create_intelligence_conversation(user_id: int, title: Optional[str] = None) -> int:
    now = int(time.time())
    with conn() as c:
        cur = c.execute(
            "INSERT INTO intelligence_conversations (user_id, title, message_count, created_at, updated_at) "
            "VALUES (?, ?, 0, ?, ?)",
            (user_id, title, now, now),
        )
        return cur.lastrowid


def list_intelligence_conversations(user_id: int, limit: int = 50) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM intelligence_conversations WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def get_intelligence_conversation(conv_id: int, user_id: int) -> Optional[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM intelligence_conversations WHERE id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()


def list_intelligence_messages(conv_id: int, limit: int = 200) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM intelligence_messages WHERE conversation_id = ? ORDER BY created_at ASC LIMIT ?",
            (conv_id, limit),
        ).fetchall()


def append_intelligence_message(
    conv_id: int,
    role: str,
    content: str,
    context_used: Optional[dict] = None,
    tokens_used: Optional[int] = None,
) -> int:
    import json as _json
    now = int(time.time())
    with conn() as c:
        cur = c.execute(
            "INSERT INTO intelligence_messages (conversation_id, role, content, context_used, tokens_used, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (conv_id, role, content, _json.dumps(context_used) if context_used else None, tokens_used, now),
        )
        title_candidate = content[:80] if role == "user" else None
        c.execute(
            "UPDATE intelligence_conversations SET message_count = message_count + 1, updated_at = ?, "
            "title = COALESCE(title, ?) WHERE id = ?",
            (now, title_candidate, conv_id),
        )
        return cur.lastrowid


def delete_intelligence_conversation(conv_id: int, user_id: int) -> bool:
    with conn() as c:
        cur = c.execute(
            "DELETE FROM intelligence_conversations WHERE id = ? AND user_id = ?",
            (conv_id, user_id),
        )
        return cur.rowcount > 0


def count_intelligence_messages_today(user_id: int) -> int:
    day_cut = int(time.time()) - 86400
    with conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM intelligence_messages im "
            "INNER JOIN intelligence_conversations ic ON im.conversation_id = ic.id "
            "WHERE ic.user_id = ? AND im.role = 'user' AND im.created_at >= ?",
            (user_id, day_cut),
        ).fetchone()
    return row[0] if row else 0


# ── Two-factor authentication (Migration 006) ────────────────────────────────
#
# TOTP secrets are stored Fernet-encrypted (backend.markets.encryption).
# Backup codes and email OTPs reuse _hash_password (PBKDF2-HMAC-SHA256).
# Rate limiting for 2FA attempts uses the persistent rate_limits table.

import json as _json_2fa


def get_user_2fa_status(user_id: int) -> Optional[sqlite3.Row]:
    """Return the 2FA-relevant columns from users, or None if user not found."""
    with conn() as c:
        return c.execute(
            "SELECT id, email, username, is_admin, totp_enabled, totp_secret, "
            "totp_setup_at, email_otp_enabled, two_fa_method, two_fa_verified_at, "
            "backup_codes, backup_codes_generated_at "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()


def set_user_2fa_method(
    user_id: int,
    method: Optional[str],
    encrypted_secret: Optional[str] = None,
) -> None:
    """Enable 2FA with *method* ("totp"|"email_otp"|None to disable).

    When method is "totp", *encrypted_secret* must be the Fernet-encrypted base32.
    Flips the matching `*_enabled` column and sets `totp_setup_at` for TOTP.
    """
    now = int(time.time())
    with conn() as c:
        if method == "totp":
            c.execute(
                "UPDATE users SET two_fa_method = ?, totp_enabled = 1, totp_secret = ?, "
                "totp_setup_at = ?, email_otp_enabled = 0 WHERE id = ?",
                (method, encrypted_secret, now, user_id),
            )
        elif method == "email_otp":
            c.execute(
                "UPDATE users SET two_fa_method = ?, email_otp_enabled = 1, "
                "totp_enabled = 0, totp_secret = NULL, totp_setup_at = NULL WHERE id = ?",
                (method, user_id),
            )
        else:
            # method=None → disable (use disable_user_2fa for a clean wipe)
            c.execute(
                "UPDATE users SET two_fa_method = NULL, totp_enabled = 0, "
                "totp_secret = NULL, totp_setup_at = NULL, email_otp_enabled = 0 "
                "WHERE id = ?",
                (user_id,),
            )


def disable_user_2fa(user_id: int) -> None:
    """Clear all 2FA state for a user — method, secrets, backup codes."""
    with conn() as c:
        c.execute(
            "UPDATE users SET two_fa_method = NULL, totp_enabled = 0, "
            "totp_secret = NULL, totp_setup_at = NULL, email_otp_enabled = 0, "
            "backup_codes = NULL, backup_codes_generated_at = NULL WHERE id = ?",
            (user_id,),
        )
        # Also clear any fresh-verification state on sessions for this user,
        # so subsequent admin pages re-prompt for 2FA.
        c.execute(
            "UPDATE sessions SET two_fa_verified = 0, two_fa_verified_at = NULL WHERE user_id = ?",
            (user_id,),
        )


def store_backup_codes(user_id: int, hashed_codes: list[dict]) -> None:
    """Persist backup codes as a JSON array of {hash, salt, used_at} dicts.

    Caller generates plaintext codes, hashes each, then calls this exactly once.
    The plaintext is shown to the user only at that moment.
    """
    now = int(time.time())
    blob = _json_2fa.dumps(hashed_codes)
    with conn() as c:
        c.execute(
            "UPDATE users SET backup_codes = ?, backup_codes_generated_at = ? WHERE id = ?",
            (blob, now, user_id),
        )


def get_backup_codes(user_id: int) -> list[dict]:
    """Return the raw hashed backup code list (or empty list if unset)."""
    with conn() as c:
        row = c.execute("SELECT backup_codes FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row or not row["backup_codes"]:
        return []
    try:
        return _json_2fa.loads(row["backup_codes"]) or []
    except (ValueError, TypeError):
        return []


def consume_backup_code(user_id: int, plaintext_code: str) -> bool:
    """Try to match *plaintext_code* against one unused backup code.

    On success, marks that entry's `used_at` and returns True. Constant-time
    comparison, single-pass over the JSON array.
    """
    codes = get_backup_codes(user_id)
    if not codes:
        return False
    matched = False
    for entry in codes:
        if entry.get("used_at"):
            continue
        stored_hash = entry.get("hash", "")
        salt = entry.get("salt", "")
        if not stored_hash or not salt:
            continue
        if verify_password(plaintext_code, stored_hash, salt):
            entry["used_at"] = int(time.time())
            matched = True
            break
    if not matched:
        return False
    blob = _json_2fa.dumps(codes)
    with conn() as c:
        c.execute("UPDATE users SET backup_codes = ? WHERE id = ?", (blob, user_id))
    return True


def count_remaining_backup_codes(user_id: int) -> int:
    codes = get_backup_codes(user_id)
    return sum(1 for c in codes if not c.get("used_at"))


def insert_2fa_attempt(user_id: int, method: str, success: bool, ip: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO two_fa_attempts (user_id, method, success, ip_address, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, method, 1 if success else 0, ip or "unknown", int(time.time())),
        )


def recent_2fa_failures(user_id: int, ip: str, window_seconds: int = 600) -> int:
    cutoff = int(time.time()) - window_seconds
    with conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM two_fa_attempts "
            "WHERE user_id = ? AND ip_address = ? AND success = 0 AND created_at >= ?",
            (user_id, ip or "unknown", cutoff),
        ).fetchone()
    return int(row["n"] if row else 0)


# ── Email OTP helpers ─────────────────────────────────────────────────────────


def insert_email_otp(
    user_id: int,
    code_hash: str,
    code_salt: str,
    ip: str = "",
    ttl_seconds: int = 600,
) -> int:
    now = int(time.time())
    # Supersede any prior unused OTP for this user so only one is ever active.
    with conn() as c:
        c.execute(
            "UPDATE email_otps SET used_at = ? "
            "WHERE user_id = ? AND used_at IS NULL",
            (now, user_id),
        )
        cur = c.execute(
            "INSERT INTO email_otps (user_id, code_hash, code_salt, created_at, expires_at, ip_address) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, code_hash, code_salt, now, now + ttl_seconds, ip or "unknown"),
        )
        return cur.lastrowid


def get_active_email_otp(user_id: int) -> Optional[sqlite3.Row]:
    now = int(time.time())
    with conn() as c:
        return c.execute(
            "SELECT * FROM email_otps WHERE user_id = ? AND used_at IS NULL "
            "AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
            (user_id, now),
        ).fetchone()


def mark_email_otp_used(otp_id: int) -> None:
    with conn() as c:
        c.execute(
            "UPDATE email_otps SET used_at = ? WHERE id = ?",
            (int(time.time()), otp_id),
        )


def purge_expired_email_otps() -> int:
    cutoff = int(time.time()) - 3600  # keep 1h for debugging, then drop
    with conn() as c:
        cur = c.execute("DELETE FROM email_otps WHERE expires_at < ?", (cutoff,))
        return cur.rowcount or 0


# ── Session 2FA verification flag ─────────────────────────────────────────────


def mark_session_two_fa_verified(session_token: str) -> None:
    """Flip sessions.two_fa_verified=1 for the given token and stamp the time.
    Also stamps users.two_fa_verified_at for the "last used" indicator."""
    now = int(time.time())
    with conn() as c:
        c.execute(
            "UPDATE sessions SET two_fa_verified = 1, two_fa_verified_at = ? WHERE token = ?",
            (now, session_token),
        )
        c.execute(
            "UPDATE users SET two_fa_verified_at = ? "
            "WHERE id = (SELECT user_id FROM sessions WHERE token = ?)",
            (now, session_token),
        )


def session_two_fa_verified(session_token: str) -> bool:
    if not session_token:
        return False
    with conn() as c:
        row = c.execute(
            "SELECT two_fa_verified FROM sessions WHERE token = ? AND expires_at > ?",
            (session_token, int(time.time())),
        ).fetchone()
    return bool(row and row["two_fa_verified"])


def set_pending_totp_secret(session_token: str, encrypted_secret: str) -> None:
    """Stash a pending Fernet-encrypted TOTP secret on the session row.

    Used between GET /api/auth/2fa/totp/setup and POST verify-setup so the
    candidate secret survives the round-trip without hitting a new table.
    Cleared on verify-setup.
    """
    with conn() as c:
        c.execute(
            "UPDATE sessions SET pending_totp_secret = ?, pending_totp_secret_at = ? "
            "WHERE token = ?",
            (encrypted_secret, int(time.time()), session_token),
        )


def get_pending_totp_secret(session_token: str, max_age_seconds: int = 900) -> Optional[str]:
    """Return the pending encrypted TOTP secret if set and still fresh (<15min)."""
    if not session_token:
        return None
    with conn() as c:
        row = c.execute(
            "SELECT pending_totp_secret, pending_totp_secret_at FROM sessions WHERE token = ?",
            (session_token,),
        ).fetchone()
    if not row or not row["pending_totp_secret"]:
        return None
    if int(time.time()) - int(row["pending_totp_secret_at"] or 0) > max_age_seconds:
        return None
    return row["pending_totp_secret"]


def clear_pending_totp_secret(session_token: str) -> None:
    with conn() as c:
        c.execute(
            "UPDATE sessions SET pending_totp_secret = NULL, pending_totp_secret_at = NULL "
            "WHERE token = ?",
            (session_token,),
        )


# ── Audit log (Migration 006, Feature 2) ─────────────────────────────────────


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
    with conn() as c:
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
    with conn() as c:
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
    with conn() as c:
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


# ── Hardened session store (token-first auth) ─────────────────────────────
#
# `user_sessions` stores session tokens as SHA-256 hashes at rest. The cookie
# the client holds contains the raw token; validate_user_session() hashes it
# and looks up. Also tracks last-activity + device metadata so users can
# review and revoke sessions from Settings → Security.
#
# The older `sessions` table is kept so CSRF / 2FA / admin-audit code paths
# keep working — new logins write to BOTH tables in the same txn.

SESSION_HARDENED_TTL = 7 * 24 * 60 * 60  # 7 days
MAX_SESSIONS_PER_USER = 5


def _hash_session_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_user_session(
    user_id: int,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    legacy_token: Optional[str] = None,
    ttl_seconds: int = SESSION_HARDENED_TTL,
) -> str:
    """Issue a new hardened session. Returns the raw token (store in cookie).

    Enforces MAX_SESSIONS_PER_USER by revoking the oldest active session
    before inserting the new one. If `legacy_token` is provided, it's
    recorded alongside so the legacy `sessions` table lookup (CSRF etc)
    keeps working for this session.
    """
    raw = secrets.token_hex(32)  # 64 hex chars
    token_hash = _hash_session_token(raw)
    now = int(time.time())
    with conn() as c:
        active = c.execute(
            "SELECT id FROM user_sessions "
            "WHERE user_id = ? AND revoked = 0 AND expires_at > ? "
            "ORDER BY last_active_at ASC",
            (user_id, now),
        ).fetchall()
        if len(active) >= MAX_SESSIONS_PER_USER:
            to_revoke = len(active) - MAX_SESSIONS_PER_USER + 1
            oldest_ids = [r["id"] for r in active[:to_revoke]]
            c.executemany(
                "UPDATE user_sessions SET revoked = 1, revoked_at = ? WHERE id = ?",
                [(now, sid) for sid in oldest_ids],
            )
        c.execute(
            "INSERT INTO user_sessions "
            "(user_id, token_hash, legacy_token, created_at, expires_at, "
            "last_active_at, ip_address, user_agent, revoked) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                user_id,
                token_hash,
                legacy_token,
                now,
                now + ttl_seconds,
                now,
                (ip_address or "")[:64],
                (user_agent or "")[:256],
            ),
        )
    return raw


def validate_user_session(raw_token: str) -> Optional[sqlite3.Row]:
    """Look up a hardened session by raw cookie value.

    Hashes the raw token, finds the row, and updates last_active_at.
    Returns None for unknown / revoked / expired sessions.
    """
    if not raw_token:
        return None
    token_hash = _hash_session_token(raw_token)
    now = int(time.time())
    with conn() as c:
        row = c.execute(
            "SELECT us.*, u.username, u.email, u.is_admin "
            "FROM user_sessions us "
            "JOIN users u ON u.id = us.user_id "
            "WHERE us.token_hash = ? AND us.revoked = 0 AND us.expires_at > ?",
            (token_hash, now),
        ).fetchone()
        if row:
            c.execute(
                "UPDATE user_sessions SET last_active_at = ? WHERE id = ?",
                (now, row["id"]),
            )
    return row


def list_user_sessions(user_id: int) -> list[sqlite3.Row]:
    """Active sessions for a user, most-recently-active first."""
    now = int(time.time())
    with conn() as c:
        return c.execute(
            "SELECT * FROM user_sessions "
            "WHERE user_id = ? AND revoked = 0 AND expires_at > ? "
            "ORDER BY last_active_at DESC",
            (user_id, now),
        ).fetchall()


def revoke_user_session(session_id: int, user_id: int) -> bool:
    """Revoke a single session by id. Returns False if not owned."""
    now = int(time.time())
    with conn() as c:
        cur = c.execute(
            "UPDATE user_sessions SET revoked = 1, revoked_at = ? "
            "WHERE id = ? AND user_id = ? AND revoked = 0",
            (now, session_id, user_id),
        )
        return cur.rowcount > 0


def revoke_user_session_by_token(raw_token: str) -> bool:
    """Revoke a session by its raw cookie value. Used by POST /auth/logout."""
    if not raw_token:
        return False
    token_hash = _hash_session_token(raw_token)
    now = int(time.time())
    with conn() as c:
        cur = c.execute(
            "UPDATE user_sessions SET revoked = 1, revoked_at = ? WHERE token_hash = ? AND revoked = 0",
            (now, token_hash),
        )
        return cur.rowcount > 0


def revoke_all_other_user_sessions(user_id: int, current_token_hash: str) -> int:
    """Revoke every active session for this user except the current one.

    Used by "Sign out all other sessions" in settings. Returns count revoked.
    """
    now = int(time.time())
    with conn() as c:
        cur = c.execute(
            "UPDATE user_sessions SET revoked = 1, revoked_at = ? "
            "WHERE user_id = ? AND revoked = 0 AND token_hash != ?",
            (now, user_id, current_token_hash),
        )
        return cur.rowcount


def revoke_all_user_sessions(user_id: int) -> int:
    """Kill every active session for a user (used on password reset)."""
    now = int(time.time())
    with conn() as c:
        cur = c.execute(
            "UPDATE user_sessions SET revoked = 1, revoked_at = ? "
            "WHERE user_id = ? AND revoked = 0",
            (now, user_id),
        )
        return cur.rowcount


def rotate_session(
    old_raw_token: str,
    user_id: int,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> Optional[str]:
    """Revoke the current session and issue a fresh one for the same user.

    Spec STEP 9: "After any role change (promotion to admin, 2FA completion),
    revoke old session and issue a new session token." Callers should swap
    the cookie on the response object after calling this.

    Returns the new raw token, or None if the old token could not be
    validated (e.g. already revoked, wrong user, expired). Never raises.
    """
    if not old_raw_token:
        return None
    old = validate_user_session(old_raw_token)
    if not old or old["user_id"] != user_id:
        return None
    # Revoke first so a crash between the two calls can never leave both
    # tokens alive for the same privilege-change transition.
    revoke_user_session_by_token(old_raw_token)
    return create_user_session(
        user_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )


# ── Environmental Impact (Feature 008) ──────────────────────────────────────
#
# Cache layer for Claude-generated environmental analysis of prediction
# markets. See migrations/008_environmental_impact.py for the schema and
# intelligence/environmental.py for the analyser that produces these rows.

ENV_VALID_UNITS = frozenset({"co2_mt", "trees", "cars", "homes", "flights"})


def get_environmental_impact(market_id: str) -> Optional[sqlite3.Row]:
    """Return the cached env analysis for *market_id*, or None if absent or
    expired. The caller decides whether to regenerate on a None result —
    this function never calls Claude itself.
    """
    if not market_id:
        return None
    now = int(time.time())
    with conn() as c:
        return c.execute(
            "SELECT * FROM environmental_impacts "
            "WHERE market_id = ? AND cache_valid_until > ?",
            (market_id, now),
        ).fetchone()


def get_environmental_impact_any_age(market_id: str) -> Optional[sqlite3.Row]:
    """Return the cached row regardless of TTL — used by the analyser to
    decide whether to regenerate based on price drift.
    """
    if not market_id:
        return None
    with conn() as c:
        return c.execute(
            "SELECT * FROM environmental_impacts WHERE market_id = ?",
            (market_id,),
        ).fetchone()


def upsert_environmental_impact(market_id: str, payload: dict) -> int:
    """Atomically replace any existing row for *market_id* with *payload*.

    *payload* must include all schema fields the analyser produces. Missing
    optional fields are persisted as NULL. Returns the row id.
    """
    import json as _json
    sources_json = _json.dumps(payload.get("data_sources") or [])
    with conn() as c:
        c.execute("DELETE FROM environmental_impacts WHERE market_id = ?", (market_id,))
        cur = c.execute(
            """
            INSERT INTO environmental_impacts (
                market_id, market_question, market_category,
                generated_at, generated_by, cache_valid_until,
                is_relevant, irrelevance_reason,
                yes_outcome_label, no_outcome_label,
                yes_co2_impact_mt, no_co2_impact_mt,
                yes_impact_description, no_impact_description,
                yes_impact_timeframe, no_impact_timeframe,
                confidence, confidence_reason, data_sources, category,
                yes_market_price_at_gen
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                market_id,
                payload.get("market_question") or "",
                payload.get("market_category"),
                int(payload.get("generated_at") or time.time()),
                payload.get("generated_by") or "unknown",
                int(payload.get("cache_valid_until") or (time.time() + 86400)),
                1 if payload.get("is_relevant") else 0,
                payload.get("irrelevance_reason"),
                payload.get("yes_outcome_label") or "YES",
                payload.get("no_outcome_label") or "NO",
                payload.get("yes_co2_impact_mt"),
                payload.get("no_co2_impact_mt"),
                payload.get("yes_impact_description"),
                payload.get("no_impact_description"),
                payload.get("yes_impact_timeframe"),
                payload.get("no_impact_timeframe"),
                payload.get("confidence"),
                payload.get("confidence_reason"),
                sources_json,
                payload.get("category"),
                payload.get("yes_market_price_at_gen"),
            ),
        )
        return cur.lastrowid


def list_top_environmental_impacts(limit: int = 20) -> list[sqlite3.Row]:
    """Return env-relevant rows ordered by total absolute CO2 impact.

    Used by GET /api/markets/environmental/top and the Intelligence context
    builder. Reads from cache only — never triggers generation. Excludes
    rows with both yes/no impacts NULL (degenerate analyses).
    """
    limit = max(1, min(100, int(limit)))
    with conn() as c:
        return c.execute(
            """
            SELECT *,
                   COALESCE(ABS(yes_co2_impact_mt), 0) +
                   COALESCE(ABS(no_co2_impact_mt), 0) AS total_abs_impact
            FROM environmental_impacts
            WHERE is_relevant = 1
              AND (yes_co2_impact_mt IS NOT NULL OR no_co2_impact_mt IS NOT NULL)
            ORDER BY total_abs_impact DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def get_user_env_preferences(user_id: int) -> dict:
    """Return {"show": bool, "unit": str} for *user_id*. New users get the
    schema defaults (show=True, unit='co2_mt') even if their row was created
    before migration 008 ran (the ALTER TABLE default backfills automatically).
    """
    with conn() as c:
        row = c.execute(
            "SELECT env_show, env_unit FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"show": True, "unit": "co2_mt"}
    unit = row["env_unit"] if row["env_unit"] in ENV_VALID_UNITS else "co2_mt"
    return {"show": bool(row["env_show"]), "unit": unit}


def set_user_env_preferences(user_id: int, *, show: bool, unit: str) -> bool:
    """Persist environmental display preferences. Validates *unit* against
    ENV_VALID_UNITS — invalid units raise ValueError so callers can return
    a 400 to the client. Returns True if the row was updated.
    """
    if unit not in ENV_VALID_UNITS:
        raise ValueError(f"unit must be one of {sorted(ENV_VALID_UNITS)}")
    with conn() as c:
        cur = c.execute(
            "UPDATE users SET env_show = ?, env_unit = ? WHERE id = ?",
            (1 if show else 0, unit, user_id),
        )
    return cur.rowcount > 0


# ── Embed widgets ────────────────────────────────────────────────────────────
#
# Token-gated, domain-locked widgets that subscribers embed on their own
# sites. See migrations/021_embed_widgets.py for the table. Token signing
# helpers live in embed_tokens.py — imported lazily here so db.py stays
# importable by processes that never use embeds.

EMBED_WIDGET_TYPES = frozenset({"source_credibility", "market_probability", "best_bets"})
EMBED_WIDGET_THEMES = frozenset({"light", "dark", "auto"})
MAX_EMBED_WIDGETS_PER_USER = 10


def has_any_active_subscription(user_id: int) -> bool:
    """True if the user has at least one active subscription on any dashboard.

    Admins bypass this check. Used by cross-dashboard features that require
    being a paying narve.ai customer but aren't scoped to a single product
    (e.g. embed widgets). Distinct from ``has_active_subscription`` which
    takes a dashboard_key.
    """
    now = int(time.time())
    with conn() as c:
        admin_row = c.execute(
            "SELECT is_admin FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if admin_row and admin_row[0]:
            return True
        row = c.execute(
            "SELECT 1 FROM subscriptions "
            "WHERE user_id = ? AND status = 'active' "
            "AND (expires_at IS NULL OR expires_at > ?) "
            "LIMIT 1",
            (user_id, now),
        ).fetchone()
    return row is not None


def count_user_active_embed_widgets(user_id: int) -> int:
    with conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM embed_widgets "
            "WHERE user_id = ? AND is_active = 1",
            (user_id,),
        ).fetchone()
    return row["n"] if row else 0


def create_embed_widget(
    user_id: int,
    widget_type: str,
    target: str,
    domain: str,
    theme: str = "auto",
) -> Optional[sqlite3.Row]:
    """Create a widget for ``user_id``. Returns the row or ``None`` if over limit.

    Caller validates ``widget_type``, ``target``, ``domain``, and ``theme``
    before calling. The limit check lives inside the same transaction as
    the insert so two concurrent creates can't both slip past.
    """
    import embed_tokens  # lazy import: avoids a cycle at module load
    widget_id = embed_tokens.new_widget_id()
    token_salt = embed_tokens.new_salt()
    now = int(time.time())
    with conn() as c:
        existing = c.execute(
            "SELECT COUNT(*) AS n FROM embed_widgets "
            "WHERE user_id = ? AND is_active = 1",
            (user_id,),
        ).fetchone()
        if existing and existing["n"] >= MAX_EMBED_WIDGETS_PER_USER:
            return None
        c.execute(
            "INSERT INTO embed_widgets "
            "(widget_id, user_id, widget_type, target, domain, token_salt, "
            " theme, created_at, is_active, impressions) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0)",
            (
                widget_id, user_id, widget_type, target, domain.lower(),
                token_salt, theme, now,
            ),
        )
        return c.execute(
            "SELECT * FROM embed_widgets WHERE widget_id = ?", (widget_id,)
        ).fetchone()


def list_user_embed_widgets(user_id: int, include_inactive: bool = True) -> list[sqlite3.Row]:
    """Return all widgets for the user, newest first.

    Deactivated widgets are included by default so the management UI can
    show historical impression counts. Pass ``include_inactive=False`` to
    scope to live widgets only.
    """
    with conn() as c:
        if include_inactive:
            return c.execute(
                "SELECT * FROM embed_widgets WHERE user_id = ? "
                "ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return c.execute(
            "SELECT * FROM embed_widgets WHERE user_id = ? AND is_active = 1 "
            "ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()


def get_embed_widget_by_widget_id(widget_id: str) -> Optional[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM embed_widgets WHERE widget_id = ?", (widget_id,)
        ).fetchone()


def get_user_embed_widget(user_id: int, widget_id: str) -> Optional[sqlite3.Row]:
    """Scoped lookup: returns the row only if ``user_id`` owns it."""
    with conn() as c:
        return c.execute(
            "SELECT * FROM embed_widgets WHERE user_id = ? AND widget_id = ?",
            (user_id, widget_id),
        ).fetchone()


def deactivate_embed_widget(user_id: int, widget_id: str) -> bool:
    """Flip is_active=0 for a user's widget. Idempotent."""
    with conn() as c:
        cur = c.execute(
            "UPDATE embed_widgets SET is_active = 0 "
            "WHERE user_id = ? AND widget_id = ?",
            (user_id, widget_id),
        )
    return cur.rowcount > 0


def rotate_embed_widget_token(user_id: int, widget_id: str) -> Optional[sqlite3.Row]:
    """Replace token_salt with a fresh nonce. Returns the updated row or None.

    Only rotates tokens for active widgets — rotating a deactivated widget
    would be pointless and may indicate a mistake, so it's a no-op that
    returns ``None``.
    """
    import embed_tokens
    fresh_salt = embed_tokens.new_salt()
    with conn() as c:
        cur = c.execute(
            "UPDATE embed_widgets SET token_salt = ? "
            "WHERE user_id = ? AND widget_id = ? AND is_active = 1",
            (fresh_salt, user_id, widget_id),
        )
        if cur.rowcount == 0:
            return None
        return c.execute(
            "SELECT * FROM embed_widgets WHERE widget_id = ?", (widget_id,)
        ).fetchone()


def increment_embed_widget_impression(widget_id: str) -> None:
    """Bump impressions + last_used_at for a widget. Background-safe."""
    now = int(time.time())
    with conn() as c:
        c.execute(
            "UPDATE embed_widgets SET impressions = impressions + 1, "
            "last_used_at = ? WHERE widget_id = ? AND is_active = 1",
            (now, widget_id),
        )


def deactivate_all_user_embed_widgets(user_id: int) -> int:
    """Deactivate every live widget for a user. Called when a sub lapses.

    Returns the number of rows flipped — useful for telemetry and tests.
    """
    with conn() as c:
        cur = c.execute(
            "UPDATE embed_widgets SET is_active = 0 "
            "WHERE user_id = ? AND is_active = 1",
            (user_id,),
        )
    return cur.rowcount


# ── Claude usage log ───────────────────────────────────────────────────────

CLAUDE_FEATURES = frozenset({
    "extraction",
    "categorisation",
    "summarisation",
    "intelligence_chat",
    "environmental",
    "retrospective",
})


def log_claude_usage(
    *,
    feature: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    cached_hit: bool = False,
) -> int:
    """Append one row to claude_usage_log. Never raises."""
    try:
        with conn() as c:
            cur = c.execute(
                "INSERT INTO claude_usage_log "
                "(timestamp, feature, model, input_tokens, output_tokens, cost_usd, cached_hit) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    int(time.time()), feature, model,
                    int(input_tokens or 0), int(output_tokens or 0),
                    float(cost_usd or 0.0),
                    1 if cached_hit else 0,
                ),
            )
            return cur.lastrowid
    except Exception:
        return 0


def claude_usage_between(start_ts: int, end_ts: int) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM claude_usage_log "
            "WHERE timestamp >= ? AND timestamp < ? "
            "ORDER BY timestamp DESC",
            (int(start_ts), int(end_ts)),
        ).fetchall()


def claude_usage_daily_rollup(days: int = 7) -> list[dict]:
    days = max(1, min(90, int(days)))
    now = int(time.time())
    start = now - days * 86400
    with conn() as c:
        rows = c.execute(
            """
            SELECT
                strftime('%Y-%m-%d', timestamp, 'unixepoch') AS day,
                feature,
                COUNT(*) AS calls,
                SUM(cached_hit) AS cache_hits,
                SUM(input_tokens) AS input_tokens,
                SUM(output_tokens) AS output_tokens,
                SUM(cost_usd) AS cost_usd
            FROM claude_usage_log
            WHERE timestamp >= ?
            GROUP BY day, feature
            ORDER BY day DESC, feature ASC
            """,
            (start,),
        ).fetchall()
    return [
        {
            "day": r["day"],
            "feature": r["feature"],
            "calls": int(r["calls"] or 0),
            "cache_hits": int(r["cache_hits"] or 0),
            "input_tokens": int(r["input_tokens"] or 0),
            "output_tokens": int(r["output_tokens"] or 0),
            "cost_usd": float(r["cost_usd"] or 0.0),
        }
        for r in rows
    ]


def claude_usage_day_total(day_utc: str) -> dict:
    with conn() as c:
        rows = c.execute(
            """
            SELECT feature, COUNT(*) AS calls,
                   SUM(cached_hit) AS cache_hits,
                   SUM(cost_usd) AS cost_usd
            FROM claude_usage_log
            WHERE strftime('%Y-%m-%d', timestamp, 'unixepoch') = ?
            GROUP BY feature
            """,
            (day_utc,),
        ).fetchall()
    by_feature = {
        r["feature"]: {
            "calls": int(r["calls"] or 0),
            "cache_hits": int(r["cache_hits"] or 0),
            "cost_usd": float(r["cost_usd"] or 0.0),
        }
        for r in rows
    }
    return {
        "day": day_utc,
        "calls": sum(f["calls"] for f in by_feature.values()),
        "cost_usd": round(sum(f["cost_usd"] for f in by_feature.values()), 4),
        "by_feature": by_feature,
    }


# ── Prediction extraction cache ────────────────────────────────────────────


def get_prediction_extraction(post_hash: str) -> Optional[sqlite3.Row]:
    if not post_hash:
        return None
    now = int(time.time())
    with conn() as c:
        return c.execute(
            "SELECT * FROM prediction_extractions "
            "WHERE post_hash = ? AND cache_valid_until > ?",
            (post_hash, now),
        ).fetchone()


def upsert_prediction_extraction(post_hash: str, payload: dict) -> int:
    import json as _json
    with conn() as c:
        c.execute("DELETE FROM prediction_extractions WHERE post_hash = ?", (post_hash,))
        cur = c.execute(
            """
            INSERT INTO prediction_extractions (
                post_hash, schema_version, source_post_id, source_handle,
                generated_at, generated_by, cache_valid_until,
                is_prediction, claim, direction, explicit_probability,
                implicit_confidence, time_frame, category,
                contains_sarcasm, is_conditional, raw_payload
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                post_hash,
                int(payload.get("schema_version", 1)),
                payload.get("source_post_id"),
                payload.get("source_handle"),
                int(payload.get("generated_at") or time.time()),
                payload.get("generated_by") or "unknown",
                int(payload.get("cache_valid_until") or (time.time() + 30 * 86400)),
                1 if payload.get("is_prediction") else 0,
                payload.get("claim"),
                payload.get("direction"),
                payload.get("explicit_probability"),
                payload.get("implicit_confidence"),
                payload.get("time_frame"),
                payload.get("category"),
                1 if payload.get("contains_sarcasm") else 0,
                1 if payload.get("is_conditional") else 0,
                _json.dumps(payload.get("raw_payload") or {}),
            ),
        )
        return cur.lastrowid


def insert_reextracted_prediction(payload: dict) -> int:
    with conn() as c:
        cur = c.execute(
            """
            INSERT INTO predictions_reextracted (
                original_prediction_id, source_handle, market_id, category,
                direction, predicted_probability, content, source_url,
                extracted_at, claim, explicit_probability, implicit_confidence,
                time_frame, contains_sarcasm, is_conditional,
                matches_original, diff_summary
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                payload.get("original_prediction_id"),
                payload.get("source_handle"),
                payload.get("market_id"),
                payload.get("category"),
                payload.get("direction"),
                payload.get("predicted_probability"),
                payload.get("content") or "",
                payload.get("source_url"),
                int(payload.get("extracted_at") or time.time()),
                payload.get("claim"),
                payload.get("explicit_probability"),
                payload.get("implicit_confidence"),
                payload.get("time_frame"),
                1 if payload.get("contains_sarcasm") else 0,
                1 if payload.get("is_conditional") else 0,
                1 if payload.get("matches_original") else 0,
                payload.get("diff_summary"),
            ),
        )
        return cur.lastrowid


def reextraction_diff_summary() -> dict:
    with conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN matches_original = 1 THEN 1 ELSE 0 END) AS matches "
            "FROM predictions_reextracted"
        ).fetchone()
    total = int(row["total"] or 0) if row else 0
    matches = int(row["matches"] or 0) if row else 0
    return {
        "total": total,
        "matches": matches,
        "diffs": total - matches,
        "match_rate": round(matches / total, 4) if total else 0.0,
    }


def apply_reextraction_switchover() -> dict:
    updated = 0
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM predictions_reextracted WHERE original_prediction_id IS NOT NULL"
        ).fetchall()
        for r in rows:
            c.execute(
                "UPDATE predictions SET category = ?, direction = ?, "
                "predicted_probability = ? WHERE id = ?",
                (r["category"], r["direction"],
                 r["predicted_probability"], r["original_prediction_id"]),
            )
            updated += 1
        c.execute("DELETE FROM predictions_reextracted")
    return {"updated": updated}


# ── Market categorisation cache ────────────────────────────────────────────


def get_market_categorisation(market_id: str) -> Optional[sqlite3.Row]:
    if not market_id:
        return None
    now = int(time.time())
    with conn() as c:
        return c.execute(
            "SELECT * FROM market_categorisations "
            "WHERE market_id = ? AND cache_valid_until > ?",
            (market_id, now),
        ).fetchone()


def upsert_market_categorisation(market_id: str, payload: dict) -> int:
    import json as _json
    tags_json = _json.dumps(payload.get("tags") or [])
    with conn() as c:
        c.execute("DELETE FROM market_categorisations WHERE market_id = ?", (market_id,))
        cur = c.execute(
            """
            INSERT INTO market_categorisations (
                market_id, market_title, generated_at, generated_by,
                cache_valid_until, primary_category, sub_category, tags,
                political_leaning, sensitivity,
                insider_trading_relevant, environmental_relevant,
                requires_expert_knowledge
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                market_id,
                payload.get("market_title") or "",
                int(payload.get("generated_at") or time.time()),
                payload.get("generated_by") or "unknown",
                int(payload.get("cache_valid_until") or (time.time() + 365 * 86400)),
                payload.get("primary_category") or "other",
                payload.get("sub_category"),
                tags_json,
                payload.get("political_leaning"),
                payload.get("sensitivity") or "normal",
                1 if payload.get("insider_trading_relevant") else 0,
                1 if payload.get("environmental_relevant") else 0,
                1 if payload.get("requires_expert_knowledge") else 0,
            ),
        )
        return cur.lastrowid


def list_uncategorised_market_ids(market_ids: list[str]) -> list[str]:
    if not market_ids:
        return []
    now = int(time.time())
    placeholders = ",".join("?" * len(market_ids))
    with conn() as c:
        rows = c.execute(
            f"SELECT market_id FROM market_categorisations "
            f"WHERE market_id IN ({placeholders}) AND cache_valid_until > ?",
            (*market_ids, now),
        ).fetchall()
    cached = {r["market_id"] for r in rows}
    return [mid for mid in market_ids if mid not in cached]


# ── Source summaries ───────────────────────────────────────────────────────


def get_source_summary(source_handle: str) -> Optional[sqlite3.Row]:
    if not source_handle:
        return None
    now = int(time.time())
    with conn() as c:
        return c.execute(
            "SELECT * FROM source_summaries "
            "WHERE source_handle = ? AND cache_valid_until > ?",
            (source_handle, now),
        ).fetchone()


def upsert_source_summary(source_handle: str, payload: dict) -> int:
    with conn() as c:
        c.execute("DELETE FROM source_summaries WHERE source_handle = ?", (source_handle,))
        cur = c.execute(
            """
            INSERT INTO source_summaries (
                source_handle, summary, generated_at, generated_by,
                cache_valid_until, predictions_considered
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                source_handle,
                payload.get("summary") or "",
                int(payload.get("generated_at") or time.time()),
                payload.get("generated_by") or "unknown",
                int(payload.get("cache_valid_until") or (time.time() + 30 * 86400)),
                int(payload.get("predictions_considered") or 0),
            ),
        )
        return cur.lastrowid


def list_stale_source_summaries(limit: int = 50) -> list[sqlite3.Row]:
    now = int(time.time())
    with conn() as c:
        return c.execute(
            """
            SELECT sc.source_handle
            FROM source_credibility sc
            LEFT JOIN source_summaries ss ON ss.source_handle = sc.source_handle
            WHERE sc.accuracy_unlocked = 1
              AND (ss.cache_valid_until IS NULL OR ss.cache_valid_until <= ?)
            ORDER BY sc.global_credibility DESC
            LIMIT ?
            """,
            (now, int(limit)),
        ).fetchall()


def get_source_prediction_context(source_handle: str, limit: int = 50) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            """
            SELECT content, category, direction, predicted_probability,
                   resolved, resolved_correct, extracted_at
            FROM predictions
            WHERE source_handle = ?
            ORDER BY extracted_at DESC
            LIMIT ?
            """,
            (source_handle, int(limit)),
        ).fetchall()

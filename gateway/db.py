"""SQLite layer for the gateway — users, sessions, subscriptions."""

from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

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
        if "expires_at" not in invite_cols:
            # Previously wired via a top-level helper in db.py; folded into
            # init_db so fresh databases get the column on first boot.
            c.execute("ALTER TABLE invite_tokens ADD COLUMN expires_at INTEGER")
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


# ── Saved predictions (watchlist) ─────────────────────────────────────────────


# ── Source following ──────────────────────────────────────────────────────────


# ── Market snapshots (historical odds) ────────────────────────────────────────


# ── Password hashing ──────────────────────────────────────────────────────────
# Using PBKDF2-HMAC-SHA256 (stdlib, no external deps). 600k iterations (OWASP
# 2023+). Legacy 200k hashes still verified so existing users can log in;
# callers should check `password_needs_rehash()` after a successful verify
# and re-hash at the modern iteration count to upgrade the row.
# (PBKDF2_ITERATIONS / PBKDF2_LEGACY_ITERATIONS now live in queries/auth.py.)


# ── User operations ───────────────────────────────────────────────────────────


# ── Session operations ────────────────────────────────────────────────────────


# ── Subscription operations ───────────────────────────────────────────────────


# ── Invite token operations ──────────────────────────────────────────────────
# (The top-level ``_ensure_invite_expires_at_column()`` call now runs from
# queries/auth.py where the helper lives — it fires when queries.auth is
# imported, which is the last thing this module does.)


# ── User management (admin) ─────────────────────────────────────────────────


# ── Password reset operations ────────────────────────────────────────────────


# ── Newsletter ────────────────────────────────────────────────────────────────


# ── Market credential operations ──────────────────────────────────────────


# ── Bet history operations ──────────────────────────────────────────────────


# ── Market connection activation ───────────────────────────────────────────


# ── User positions (Polymarket + Kalshi snapshots) ────────────────────────


# ── User bankroll (Kelly preferences) ──────────────────────────────────────


# ── Trading add-on operations ────────────────────────────────────────────────


# ── Credibility engine operations ────────────────────────────────────────────


# ── Prediction operations ────────────────────────────────────────────────────


# ── Probability calculation ──────────────────────────────────────────────────


# ── Topic operations (Signal Search) ────────────────────────────────────────


# ── Onboarding (Feature 4) ────────────────────────────────────────────────


# ── Feedback (Feature 5) ──────────────────────────────────────────────────


# ── Analytics (Feature 6) ─────────────────────────────────────────────────


# ── Gifted subscriptions (Feature 7) ──────────────────────────────────────


# ── Intelligence conversations (Feature 8) ────────────────────────────────


# ── Two-factor authentication (Migration 006) ────────────────────────────────
#
# TOTP secrets are stored Fernet-encrypted (backend.markets.encryption).
# Backup codes and email OTPs reuse _hash_password (PBKDF2-HMAC-SHA256).
# Rate limiting for 2FA attempts uses the persistent rate_limits table.



# ── Email OTP helpers ─────────────────────────────────────────────────────────


# ── Session 2FA verification flag ─────────────────────────────────────────────


# ── Audit log (Migration 006, Feature 2) ─────────────────────────────────────


# ── Hardened session store (token-first auth) ─────────────────────────────
#
# `user_sessions` stores session tokens as SHA-256 hashes at rest. The cookie
# the client holds contains the raw token; validate_user_session() hashes it
# and looks up. Also tracks last-activity + device metadata so users can
# review and revoke sessions from Settings → Security.
#
# The older `sessions` table is kept so CSRF / 2FA / admin-audit code paths
# keep working — new logins write to BOTH tables in the same txn.


# ── Environmental Impact (Feature 008) ──────────────────────────────────────
#
# Cache layer for Claude-generated environmental analysis of prediction
# markets. See migrations/008_environmental_impact.py for the schema and
# intelligence/environmental.py for the analyser that produces these rows.


# ── Embed widgets ────────────────────────────────────────────────────────────
#
# Token-gated, domain-locked widgets that subscribers embed on their own
# sites. See migrations/021_embed_widgets.py for the table. Token signing
# helpers live in embed_tokens.py — imported lazily here so db.py stays
# importable by processes that never use embeds.


# ── Claude usage log ───────────────────────────────────────────────────────


# ── Prediction extraction cache ────────────────────────────────────────────


# ── Market categorisation cache ────────────────────────────────────────────


# ── Source summaries ───────────────────────────────────────────────────────


# ── Impersonation sessions (Migration 022) ──────────────────────────────────


# ── Feature flags (Migration 022) ────────────────────────────────────────────


# ── Email templates (Migration 022) ──────────────────────────────────────────


# ── Data export requests (Migration 030/032) ────────────────────────────────


# ── User predictions (Migration 026) ────────────────────────────────────────


# ─── Re-exports from queries/* ──────────────────────────────
# Historical call sites do ``import db; db.<name>``. After the
# split, the query functions live in queries/<domain>.py; this
# block rebinds every one of them onto the db module namespace
# so zero downstream code has to change.
from queries.auth import (  # noqa: F401,E402
    _hash_password,
    _hash_session_token,
    SESSION_TTL,
    INVITE_TOKEN_TTL_SECONDS,
    RESET_TTL,
    SESSION_HARDENED_TTL,
    MAX_SESSIONS_PER_USER,
    rate_limit_hit,
    rate_limit_check,
    rate_limit_gc,
    record_login_failure,
    is_login_locked,
    clear_login_failures,
    login_failures_gc,
    delete_sessions_for_user,
    verify_password,
    password_needs_rehash,
    create_user,
    get_user_by_email,
    get_user_by_username,
    get_user_by_email_or_username,
    get_user_by_id,
    set_default_dashboard,
    get_default_dashboard,
    create_session,
    get_session,
    delete_session,
    purge_expired_sessions,
    set_session_csrf,
    get_session_csrf,
    clear_session_csrf,
    generate_invite_token,
    create_invite_token,
    get_invite_token,
    claim_invite_token,
    revoke_invite_token,
    list_invite_tokens,
    list_all_users,
    set_user_role,
    set_user_admin,
    set_user_suspended,
    mask_email,
    create_password_reset,
    get_password_reset,
    use_password_reset,
    purge_expired_resets,
    get_user_2fa_status,
    set_user_2fa_method,
    disable_user_2fa,
    store_backup_codes,
    get_backup_codes,
    consume_backup_code,
    count_remaining_backup_codes,
    insert_2fa_attempt,
    recent_2fa_failures,
    insert_email_otp,
    get_active_email_otp,
    mark_email_otp_used,
    purge_expired_email_otps,
    mark_session_two_fa_verified,
    session_two_fa_verified,
    set_pending_totp_secret,
    get_pending_totp_secret,
    clear_pending_totp_secret,
    create_user_session,
    validate_user_session,
    list_user_sessions,
    revoke_user_session,
    cascade_delete_user,
    revoke_user_session_by_token,
    revoke_all_other_user_sessions,
    revoke_all_user_sessions,
    rotate_session,
)
from queries.watchlist import (  # noqa: F401,E402
    save_prediction,
    unsave_prediction,
    is_prediction_saved,
    list_saved_predictions,
    update_saved_prediction_notes,
    saved_prediction_ids_for_user,
    saved_predictions_pending_resolution_notification,
    mark_saved_prediction_notified,
    follow_source,
    unfollow_source,
    is_following_source,
    update_follow_preferences,
    list_followed_sources,
    followed_source_handles,
)
from queries.markets import (  # noqa: F401,E402
    search_markets,
    insert_market_snapshot,
    get_market_history,
    get_latest_market_snapshot,
    get_market_snapshot_at,
    get_prediction_markers_for_market,
    upsert_market_credential,
    get_market_credential,
    get_all_market_credentials,
    delete_market_credential,
    update_market_credential_last_used,
    record_bet,
    list_bet_history,
    set_market_credential_active,
    disconnect_market_credential,
    upsert_user_position,
    get_user_positions,
    delete_user_positions,
    prune_stale_positions,
    get_portfolio_stats,
    get_user_bankroll,
    set_user_bankroll,
    get_trading_addon_status,
    set_trading_addon,
    has_trading_addon,
    get_trading_addon_settings,
    upsert_trading_addon_settings,
    get_market_categorisation,
    upsert_market_categorisation,
    list_uncategorised_market_ids,
)
from queries.sources import (  # noqa: F401,E402
    search_sources,
    get_source_credibility,
    upsert_source_credibility,
    get_category_credibility,
    get_all_category_credibilities,
    upsert_category_credibility,
    get_credibility_snapshots,
    list_all_source_credibilities,
    compute_calibration,
    get_source_calibration,
    recompute_all_credibilities,
    get_source_summary,
    upsert_source_summary,
    list_stale_source_summaries,
    get_source_prediction_context,
)
from queries.predictions import (  # noqa: F401,E402
    search_predictions,
    create_prediction,
    get_unresolved_market_ids,
    resolve_predictions_for_market,
    get_predictions_for_market,
    list_recent_predictions,
    calculate_betyc_probability,
    get_prediction_extraction,
    upsert_prediction_extraction,
    insert_reextracted_prediction,
    reextraction_diff_summary,
    apply_reextraction_switchover,
    create_user_prediction,
    get_user_prediction,
    get_active_user_prediction,
    update_user_prediction,
    list_user_predictions,
    list_public_user_predictions,
    get_user_prediction_stats,
    upsert_user_prediction_stats,
)
from queries.topics import (  # noqa: F401,E402
    create_topic,
    list_topics,
    get_topic,
    delete_topic,
    count_user_topics,
    update_topic_pull,
    get_due_topics,
    add_topic_prediction,
    get_topic_predictions,
    save_topic_analysis,
    get_latest_topic_analysis,
)
from queries.onboarding import (  # noqa: F401,E402
    get_onboarding_status,
    set_onboarding_categories,
    set_onboarding_notifications,
    complete_onboarding,
)
from queries.subscriptions import (  # noqa: F401,E402
    list_subscriptions,
    has_active_subscription,
    upsert_subscription,
    cancel_subscription,
    list_all_subscriptions,
    get_active_subscription_counts_by_dashboard,
    count_active_subscribers,
    get_mrr_by_dashboard,
    get_churn_rate,
    get_new_signups,
    get_signups_daily_series,
    get_revenue_stats,
    create_gift,
    list_active_gifts,
    get_user_active_gifts,
    revoke_gift,
    get_user_intelligence_addon_active,
    set_user_intelligence_addon,
    get_user_subscription_tier,
    get_user_primary_subscription,
    has_any_active_subscription,
    get_user_active_subproducts,
)
from queries.intelligence import (  # noqa: F401,E402
    create_intelligence_conversation,
    list_intelligence_conversations,
    get_intelligence_conversation,
    list_intelligence_messages,
    append_intelligence_message,
    delete_intelligence_conversation,
    count_intelligence_messages_today,
)
from queries.newsletter import (  # noqa: F401,E402
    subscribe_newsletter,
    get_newsletter_position,
    confirm_newsletter,
    unsubscribe_newsletter,
    VALID_SEGMENTS as NEWSLETTER_VALID_SEGMENTS,
    VALID_FREQUENCIES as NEWSLETTER_VALID_FREQUENCIES,
    CONFIRMATION_RESEND_COOLDOWN_S as NEWSLETTER_CONFIRMATION_RESEND_COOLDOWN_S,
)
from queries.environmental import (  # noqa: F401,E402
    ENV_VALID_UNITS,
    get_environmental_impact,
    get_environmental_impact_any_age,
    upsert_environmental_impact,
    list_top_environmental_impacts,
    get_user_env_preferences,
    set_user_env_preferences,
)
from queries.embeds import (  # noqa: F401,E402
    EMBED_WIDGET_TYPES,
    EMBED_WIDGET_THEMES,
    MAX_EMBED_WIDGETS_PER_USER,
    count_user_active_embed_widgets,
    create_embed_widget,
    list_user_embed_widgets,
    get_embed_widget_by_widget_id,
    get_user_embed_widget,
    deactivate_embed_widget,
    rotate_embed_widget_token,
    increment_embed_widget_impression,
    deactivate_all_user_embed_widgets,
)
from queries.claude_usage import (  # noqa: F401,E402
    CLAUDE_FEATURES,
    log_claude_usage,
    claude_usage_between,
    claude_usage_daily_rollup,
    claude_usage_day_total,
)
from queries.data_exports import (  # noqa: F401,E402
    create_data_export_request,
    get_data_export_request,
    list_user_data_exports,
    last_user_data_export_ts,
    update_data_export_request,
)
from queries.admin import (  # noqa: F401,E402
    create_enquiry,
    list_enquiries,
    get_enquiry_by_id,
    mark_enquiry_read,
    count_unread_enquiries,
    create_feedback,
    list_feedback,
    update_feedback_status,
    count_feedback_by_status,
    record_analytics_event,
    get_analytics_prerelease,
    get_analytics_users,
    get_analytics_revenue,
    get_analytics_features,
    insert_audit_log,
    query_audit_log,
    export_audit_log_csv,
    create_impersonation_session,
    get_impersonation_session_by_token,
    get_impersonation_session,
    end_impersonation_session,
    record_impersonation_action,
    list_impersonation_sessions,
    list_impersonation_actions,
    list_feature_flags,
    get_feature_flag,
    create_feature_flag,
    update_feature_flag,
    delete_feature_flag,
    record_feature_flag_event,
    list_email_templates,
    get_email_template,
    upsert_email_template,
    delete_email_template,
)



# ── API keys extended (Migration 128) + webhooks (Migration 129) ────────────


def list_api_keys(user_id: int) -> list:
    """Every key belonging to *user_id*, newest first — revoked or not.

    Returned rows include `scopes` as a comma-separated string; callers are
    expected to split it themselves when they need a list. The raw key is
    never persisted, so nothing sensitive is returned.

    Also pulls the migration-180 columns (`allowed_origins`, `usage_count`)
    so the settings page can render the origin badges + lifetime call
    counter without a second round-trip. Both columns are NULL-tolerant
    on older deploys where 180 hasn't yet run.
    """
    with conn() as c:
        return c.execute(
            "SELECT id, user_id, key_prefix, name, tier, scopes, "
            "       COALESCE(allowed_origins, '') AS allowed_origins, "
            "       COALESCE(usage_count, 0) AS usage_count, "
            "       rate_limit_hour, created_at, last_used_at, revoked_at "
            "FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()


def revoke_api_key(key_id: int, user_id: int) -> bool:
    """Idempotent revoke. Returns True if a row was actually marked revoked."""
    with conn() as c:
        cur = c.execute(
            "UPDATE api_keys SET revoked_at = ? "
            "WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            (int(time.time()), key_id, user_id),
        )
        return cur.rowcount > 0


def get_api_key_by_hash(key_hash: str):
    """Used by the public API Bearer middleware. Returns None if revoked."""
    if not key_hash:
        return None
    with conn() as c:
        return c.execute(
            "SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL",
            (key_hash,),
        ).fetchone()


def bump_api_usage(api_key_id: int, hour_bucket: int) -> int:
    """UPSERT the per-hour bucket; return the post-increment count.

    Sqlite's ON CONFLICT ... DO UPDATE does the atomic increment. Falls back
    to two queries on an extremely unlikely InterfaceError (e.g. sqlite <
    3.24) so the API doesn't hard-fail on ancient hosts.
    """
    with conn() as c:
        try:
            c.execute(
                "INSERT INTO api_usage_hourly (api_key_id, hour_bucket, request_count) "
                "VALUES (?, ?, 1) "
                "ON CONFLICT(api_key_id, hour_bucket) "
                "DO UPDATE SET request_count = request_count + 1",
                (api_key_id, hour_bucket),
            )
        except sqlite3.OperationalError:
            # Older sqlite without ON CONFLICT. Best-effort fallback.
            cur = c.execute(
                "UPDATE api_usage_hourly SET request_count = request_count + 1 "
                "WHERE api_key_id = ? AND hour_bucket = ?",
                (api_key_id, hour_bucket),
            )
            if cur.rowcount == 0:
                c.execute(
                    "INSERT INTO api_usage_hourly (api_key_id, hour_bucket, request_count) "
                    "VALUES (?, ?, 1)",
                    (api_key_id, hour_bucket),
                )
        row = c.execute(
            "SELECT request_count FROM api_usage_hourly "
            "WHERE api_key_id = ? AND hour_bucket = ?",
            (api_key_id, hour_bucket),
        ).fetchone()
        return int(row["request_count"]) if row else 1


def get_api_usage(api_key_id: int, hour_bucket: int) -> int:
    with conn() as c:
        row = c.execute(
            "SELECT request_count FROM api_usage_hourly "
            "WHERE api_key_id = ? AND hour_bucket = ?",
            (api_key_id, hour_bucket),
        ).fetchone()
    return int(row["request_count"]) if row else 0


def touch_api_key_last_used(api_key_id: int) -> None:
    with conn() as c:
        c.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
            (int(time.time()), api_key_id),
        )


# ── Webhook subscriptions ───────────────────────────────────────────────────


def create_webhook_subscription(
    *,
    user_id: int,
    url: str,
    events: list,
    secret: str,
) -> int:
    """Register a new outbound webhook. Caller owns secret rotation."""
    import json as _json
    with conn() as c:
        cur = c.execute(
            "INSERT INTO webhook_subscriptions "
            "(user_id, url, events, secret, created_at, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (user_id, url, _json.dumps(events or []), secret, int(time.time())),
        )
        return cur.lastrowid


def list_webhooks_for_user(user_id: int) -> list:
    with conn() as c:
        return c.execute(
            "SELECT * FROM webhook_subscriptions "
            "WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()


def list_all_webhooks(limit: int = 200) -> list:
    """Admin-only: every webhook across all users, newest first."""
    with conn() as c:
        return c.execute(
            "SELECT w.*, u.email AS owner_email, u.username AS owner_username "
            "FROM webhook_subscriptions w "
            "LEFT JOIN users u ON u.id = w.user_id "
            "ORDER BY w.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


def get_webhook_subscription(webhook_id: int):
    with conn() as c:
        return c.execute(
            "SELECT * FROM webhook_subscriptions WHERE id = ?",
            (webhook_id,),
        ).fetchone()


def delete_webhook_subscription(webhook_id: int, user_id: int) -> bool:
    """Hard delete (cascades to deliveries). Gated on ownership."""
    with conn() as c:
        cur = c.execute(
            "DELETE FROM webhook_subscriptions WHERE id = ? AND user_id = ?",
            (webhook_id, user_id),
        )
        return cur.rowcount > 0


def deactivate_webhook(webhook_id: int) -> None:
    """Used by the delivery worker after N consecutive failures."""
    with conn() as c:
        c.execute(
            "UPDATE webhook_subscriptions SET is_active = 0 WHERE id = ?",
            (webhook_id,),
        )


def list_active_webhooks_for_event(event_type: str) -> list:
    """Every active subscription whose events list contains *event_type*.

    sqlite has no JSON_CONTAINS; we filter in Python which is fine at the
    expected scale (< a few thousand subscriptions per instance).
    """
    import json as _json
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM webhook_subscriptions WHERE is_active = 1"
        ).fetchall()
    out = []
    for r in rows:
        try:
            events = _json.loads(r["events"] or "[]")
        except (ValueError, TypeError):
            continue
        if event_type in events:
            out.append(r)
    return out


def record_webhook_delivery(
    *,
    webhook_id: int,
    event_type: str,
    payload: str,
    status_code,
    attempts: int = 1,
    error=None,
) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO webhook_deliveries "
            "(webhook_id, event_type, payload, status_code, delivered_at, attempts, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (webhook_id, event_type, payload, status_code,
             int(time.time()), attempts, error),
        )


def list_webhook_deliveries(webhook_id: int, limit: int = 20) -> list:
    with conn() as c:
        return c.execute(
            "SELECT * FROM webhook_deliveries "
            "WHERE webhook_id = ? "
            "ORDER BY delivered_at DESC LIMIT ?",
            (webhook_id, limit),
        ).fetchall()


def bump_webhook_failure(webhook_id: int) -> int:
    """Increment consecutive_failures + cumulative failure_count.
    Returns the post-increment consecutive count so the worker can decide
    whether to deactivate."""
    with conn() as c:
        c.execute(
            "UPDATE webhook_subscriptions "
            "SET consecutive_failures = consecutive_failures + 1, "
            "    failure_count = failure_count + 1 "
            "WHERE id = ?",
            (webhook_id,),
        )
        row = c.execute(
            "SELECT consecutive_failures FROM webhook_subscriptions WHERE id = ?",
            (webhook_id,),
        ).fetchone()
    return int(row["consecutive_failures"]) if row else 0


def reset_webhook_failure(webhook_id: int) -> None:
    """Zero the consecutive counter on a successful delivery + stamp time.

    Also closes the circuit breaker — a probe delivery that goes through
    after the cooldown should re-arm the subscription, not leave it in
    half-open limbo."""
    with conn() as c:
        c.execute(
            "UPDATE webhook_subscriptions "
            "SET consecutive_failures = 0, "
            "    disabled_until = NULL, "
            "    last_delivered_at = ? "
            "WHERE id = ?",
            (int(time.time()), webhook_id),
        )


def open_webhook_circuit(webhook_id: int, until_ts: int) -> None:
    """Open the circuit breaker on a subscription — disables delivery until
    ``until_ts`` (UNIX seconds). The breaker auto-heals; the next delivery
    after that timestamp triggers a probe attempt."""
    with conn() as c:
        c.execute(
            "UPDATE webhook_subscriptions "
            "SET disabled_until = ? "
            "WHERE id = ?",
            (int(until_ts), webhook_id),
        )


# ── Webhook dead-letter queue ──────────────────────────────────────────


def record_webhook_dead_letter(
    *,
    subscription_id: int,
    event_type: str,
    payload: str,
    last_error: str | None,
    attempts: int,
    first_failed_at: int,
) -> int:
    """Insert a row into webhook_dead_letter for a permanently-failed delivery.

    The admin panel reads these to surface stuck deliveries. ``payload`` is
    stored verbatim so re-queueing is a straight POST without any
    schema-versioning gymnastics."""
    now = int(time.time())
    with conn() as c:
        cur = c.execute(
            "INSERT INTO webhook_dead_letter "
            "(subscription_id, event_type, payload, last_error, attempts, "
            " first_failed_at, last_attempt_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (subscription_id, event_type, payload, last_error,
             attempts, int(first_failed_at), now),
        )
        return cur.lastrowid


def get_webhook_dead_letter(dlq_id: int):
    with conn() as c:
        return c.execute(
            "SELECT * FROM webhook_dead_letter WHERE id = ?",
            (dlq_id,),
        ).fetchone()


def list_webhook_dead_letter(
    *, limit: int = 200, include_requeued: bool = False
) -> list:
    """Admin-only: every DLQ row, newest first. Open entries (not yet
    re-queued) come first by default."""
    q = (
        "SELECT d.*, w.url AS subscription_url, u.email AS owner_email "
        "FROM webhook_dead_letter d "
        "LEFT JOIN webhook_subscriptions w ON w.id = d.subscription_id "
        "LEFT JOIN users u ON u.id = w.user_id "
    )
    if not include_requeued:
        q += "WHERE d.requeued_at IS NULL "
    q += "ORDER BY d.first_failed_at DESC LIMIT ?"
    with conn() as c:
        return c.execute(q, (int(limit),)).fetchall()


def mark_webhook_dead_letter_requeued(dlq_id: int) -> None:
    """Stamp ``requeued_at`` on a DLQ row so the open-only admin view drops it."""
    with conn() as c:
        c.execute(
            "UPDATE webhook_dead_letter SET requeued_at = ? WHERE id = ?",
            (int(time.time()), int(dlq_id)),
        )


# ── System secrets (admin-rotatable, Fernet-encrypted at rest) ─────────


def set_system_secret(key: str, value: str, *, admin_user_id: int | None) -> None:
    """Encrypt + UPSERT a system secret. Empty string clears it.

    Encryption uses the same Fernet helper that backs Kalshi/Polymarket
    credentials so we don't grow a second key-management surface.
    """
    if not key:
        raise ValueError("system_secret key required")
    if value == "" or value is None:
        delete_system_secret(key)
        return
    from backend.markets.encryption import encrypt_token
    enc = encrypt_token(value)
    with conn() as c:
        c.execute(
            "INSERT INTO system_secrets (key, value_enc, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "  value_enc = excluded.value_enc, "
            "  updated_at = excluded.updated_at, "
            "  updated_by = excluded.updated_by",
            (key, enc, int(time.time()), admin_user_id),
        )


def get_system_secret(key: str) -> str | None:
    """Decrypt + return a system secret value, or None if unset."""
    with conn() as c:
        row = c.execute(
            "SELECT value_enc FROM system_secrets WHERE key = ?", (key,),
        ).fetchone()
    if not row:
        return None
    from backend.markets.encryption import decrypt_token
    try:
        return decrypt_token(row["value_enc"])
    except Exception:
        return None


def system_secret_meta(key: str) -> dict | None:
    """Return ``{set_at, set_by, length}`` (no plaintext) for an admin UI.

    Used by the Signal Search admin panel to render "set 14 days ago"
    plus the (truncated) value length, without ever exposing the secret
    itself in a server response.
    """
    with conn() as c:
        row = c.execute(
            "SELECT s.value_enc, s.updated_at, s.updated_by, u.username "
            "FROM system_secrets s LEFT JOIN users u ON u.id = s.updated_by "
            "WHERE s.key = ?",
            (key,),
        ).fetchone()
    if not row:
        return None
    # Decrypt only to compute length — never returned to caller.
    from backend.markets.encryption import decrypt_token
    try:
        plain = decrypt_token(row["value_enc"])
        length = len(plain) if plain else 0
    except Exception:
        length = 0
    return {
        "set_at": int(row["updated_at"]) if row["updated_at"] else None,
        "set_by_username": row["username"],
        "length": length,
    }


def delete_system_secret(key: str) -> bool:
    """Wipe a system secret. Returns True if a row was deleted."""
    with conn() as c:
        cur = c.execute("DELETE FROM system_secrets WHERE key = ?", (key,))
    return cur.rowcount > 0

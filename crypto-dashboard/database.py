#!/usr/bin/env python3
"""
Database layer for CryptoEdge — SQLite-backed.

Uses SQLite with WAL mode for dashboard-specific data (predictions, watchlists,
alerts, accuracy, Kalshi markets). Auth is handled by the gateway; this module
only manages dashboard-specific data.

DB file: data.db (stored alongside this file)
"""

from __future__ import annotations

import atexit
import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("crypto.db")

DB_PATH = Path(__file__).parent / "data.db"

# ── Schema ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS crypto_predictions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL,
    window_start      TEXT NOT NULL,
    pred_direction    TEXT NOT NULL,
    pred_delta        REAL,
    pred_prob         REAL,
    confidence        REAL,
    ensemble_agreement TEXT DEFAULT '',
    model_details     TEXT DEFAULT '',
    actual_direction  TEXT,
    actual_delta      REAL,
    was_correct       INTEGER,
    resolved_at       TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(ticker, window_start)
);

CREATE TABLE IF NOT EXISTS crypto_watchlists (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   TEXT NOT NULL,
    name      TEXT NOT NULL,
    tickers   TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS crypto_alert_preferences (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        TEXT NOT NULL,
    ticker         TEXT NOT NULL,
    min_confidence REAL NOT NULL DEFAULT 0.6,
    alert_email    INTEGER NOT NULL DEFAULT 1,
    alert_browser  INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, ticker)
);

CREATE TABLE IF NOT EXISTS crypto_alert_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT,
    ticker     TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    message    TEXT NOT NULL,
    confidence REAL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS crypto_kalshi_markets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker       TEXT UNIQUE NOT NULL,
    title        TEXT NOT NULL,
    category     TEXT,
    status       TEXT,
    yes_price    REAL,
    no_price     REAL,
    volume       INTEGER DEFAULT 0,
    data         TEXT DEFAULT '{}',
    last_updated TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS profiles (
    id           TEXT PRIMARY KEY,
    email        TEXT,
    username     TEXT
);

CREATE INDEX IF NOT EXISTS idx_predictions_ticker ON crypto_predictions(ticker);
CREATE INDEX IF NOT EXISTS idx_predictions_created ON crypto_predictions(created_at);
CREATE INDEX IF NOT EXISTS idx_watchlists_user ON crypto_watchlists(user_id);
CREATE INDEX IF NOT EXISTS idx_alert_prefs_user ON crypto_alert_preferences(user_id);
CREATE INDEX IF NOT EXISTS idx_alert_prefs_ticker ON crypto_alert_preferences(ticker);
CREATE INDEX IF NOT EXISTS idx_alert_history_ticker ON crypto_alert_history(ticker);
CREATE INDEX IF NOT EXISTS idx_kalshi_ticker ON crypto_kalshi_markets(ticker);

CREATE TABLE IF NOT EXISTS news_trade_alerts (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    link         TEXT,
    source       TEXT,
    published    TEXT,
    description  TEXT,
    score        INTEGER DEFAULT 0,
    keywords     TEXT DEFAULT '[]',
    event_keywords TEXT DEFAULT '[]',
    reasons      TEXT DEFAULT '[]',
    amounts      TEXT DEFAULT '[]',
    related_markets TEXT DEFAULT '[]',
    scanned_at   TEXT NOT NULL DEFAULT (datetime('now')),
    notified     INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS news_trade_watchlist (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    alert_id     TEXT NOT NULL,
    notes        TEXT DEFAULT '',
    notify_email INTEGER DEFAULT 1,
    notify_push  INTEGER DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, alert_id)
);

CREATE INDEX IF NOT EXISTS idx_news_alerts_score ON news_trade_alerts(score DESC);
CREATE INDEX IF NOT EXISTS idx_news_alerts_scanned ON news_trade_alerts(scanned_at);
CREATE INDEX IF NOT EXISTS idx_news_watchlist_user ON news_trade_watchlist(user_id);
CREATE INDEX IF NOT EXISTS idx_news_watchlist_alert ON news_trade_watchlist(alert_id);

CREATE TABLE IF NOT EXISTS clob_credentials (
    user_id      TEXT PRIMARY KEY,
    encrypted    TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS clob_trade_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    order_id        TEXT,
    condition_id    TEXT,
    token_id        TEXT,
    market_question TEXT,
    outcome         TEXT,
    side            TEXT NOT NULL,
    order_type      TEXT NOT NULL DEFAULT 'market',
    price           REAL,
    size            REAL,
    amount          REAL,
    status          TEXT NOT NULL DEFAULT 'submitted',
    response_data   TEXT DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_clob_trades_user ON clob_trade_log(user_id);
CREATE INDEX IF NOT EXISTS idx_clob_trades_status ON clob_trade_log(status);
CREATE INDEX IF NOT EXISTS idx_clob_trades_created ON clob_trade_log(created_at);

CREATE TABLE IF NOT EXISTS clob_favorites (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    question     TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, condition_id)
);

CREATE INDEX IF NOT EXISTS idx_clob_favorites_user ON clob_favorites(user_id);

CREATE TABLE IF NOT EXISTS kalshi_credentials (
    user_id      TEXT PRIMARY KEY,
    encrypted    TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Long-term holding tables ────────────────────────────────────────────────
-- Daily OHLCV bars used by the long_term analytics module. Cheap to keep in
-- the same DB as the rest of the dashboard data; ~5 KB per asset per year.
CREATE TABLE IF NOT EXISTS crypto_daily_bars (
    ticker     TEXT NOT NULL,
    date       TEXT NOT NULL,           -- ISO YYYY-MM-DD
    open       REAL NOT NULL,
    high       REAL NOT NULL,
    low        REAL NOT NULL,
    close      REAL NOT NULL,
    volume     REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (ticker, date)
);

-- Long, narrow on-chain metrics table — one row per (ticker, metric, date).
-- Lets us add new metrics without schema migrations.
CREATE TABLE IF NOT EXISTS crypto_onchain_metrics (
    ticker  TEXT NOT NULL,
    metric  TEXT NOT NULL,
    date    TEXT NOT NULL,
    value   REAL NOT NULL,
    PRIMARY KEY (ticker, metric, date)
);
CREATE INDEX IF NOT EXISTS idx_onchain_ticker_metric ON crypto_onchain_metrics(ticker, metric);

-- User holdings, lot-aware. One row per acquisition lot so we can track
-- long-term vs short-term capital gains correctly.
CREATE TABLE IF NOT EXISTS crypto_holdings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    qty           REAL NOT NULL,
    cost_basis    REAL NOT NULL,        -- USD per unit at acquisition
    acquired_at   TEXT NOT NULL,        -- ISO date
    note          TEXT DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_holdings_user ON crypto_holdings(user_id);
CREATE INDEX IF NOT EXISTS idx_holdings_user_ticker ON crypto_holdings(user_id, ticker);

-- Per-user target portfolio weights. Sum should be ~1.0; we don't enforce it.
CREATE TABLE IF NOT EXISTS crypto_target_weights (
    user_id     TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    weight      REAL NOT NULL,
    drift_band  REAL NOT NULL DEFAULT 0.05,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, ticker)
);

-- DCA schedule: one row per (user, ticker). The bot/cron consumes this.
CREATE TABLE IF NOT EXISTS crypto_dca_schedule (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    frequency       TEXT NOT NULL DEFAULT 'weekly',  -- daily | weekly | monthly
    base_amount_usd REAL NOT NULL,
    use_multiplier  INTEGER NOT NULL DEFAULT 1,      -- apply cycle-aware multiplier?
    active          INTEGER NOT NULL DEFAULT 1,
    next_run_at     TEXT,
    last_run_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, ticker)
);

-- Long-term alert preferences (drawdown depth, MVRV cross, vol regime change).
CREATE TABLE IF NOT EXISTS crypto_long_term_alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    alert_type   TEXT NOT NULL,         -- drawdown | mvrv_high | mvrv_low | vol_regime | risk_off
    threshold    REAL,
    last_fired_at TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, ticker, alert_type)
);
CREATE INDEX IF NOT EXISTS idx_lt_alerts_user ON crypto_long_term_alerts(user_id);
"""


# ── Connection management ────────────────────────────────────────────────────

def _configure_connection(c: sqlite3.Connection) -> None:
    """Apply performance pragmas to a fresh connection."""
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("PRAGMA synchronous = NORMAL")
    c.execute("PRAGMA cache_size = -8000")   # 8 MB page cache
    c.execute("PRAGMA busy_timeout = 5000")  # wait up to 5 s on lock


_local = threading.local()
_all_connections: list[sqlite3.Connection] = []
_conn_list_lock = threading.Lock()


def _close_all_connections():
    """Close all tracked thread-local SQLite connections at exit."""
    with _conn_list_lock:
        for c in _all_connections:
            try:
                c.close()
            except Exception:
                pass
        _all_connections.clear()


atexit.register(_close_all_connections)


def _get_conn() -> sqlite3.Connection:
    """Return the thread-local SQLite connection, creating it if needed."""
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, check_same_thread=False)
        _configure_connection(c)
        _local.conn = c
        with _conn_list_lock:
            _all_connections.append(c)
    return c


@contextmanager
def _conn():
    c = _get_conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


def init_db() -> None:
    """Create tables if they don't exist. Called on startup."""
    with _conn() as c:
        c.executescript(SCHEMA)
    log.info("SQLite database initialized at %s", DB_PATH)


# ── Helper: Row wrapper ─────────────────────────────────────────────────────

class Row(dict):
    """Dict subclass that supports both dict['key'] and dict.key access,
    mimicking sqlite3.Row interface for backward compatibility."""
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)

    def keys(self):
        return super().keys()


def _row(data) -> Optional[Row]:
    if data is None:
        return None
    if isinstance(data, sqlite3.Row):
        return Row({k: data[k] for k in data.keys()})
    return Row(data)


def _rows(data: list) -> list[Row]:
    return [_row(d) for d in data]


# ─── Predictions & Accuracy ─────────────────────────────────────────

def log_prediction(ticker: str, window_start: str, pred_direction: str,
                   pred_delta: float, pred_prob: float, confidence: float,
                   ensemble_agreement: str = "", model_details: str = ""):
    """Insert a prediction, ignoring duplicates on (ticker, window_start)."""
    try:
        with _conn() as c:
            c.execute(
                """INSERT OR IGNORE INTO crypto_predictions
                   (ticker, window_start, pred_direction, pred_delta, pred_prob,
                    confidence, ensemble_agreement, model_details)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ticker, window_start, pred_direction, pred_delta, pred_prob,
                 confidence, ensemble_agreement, model_details),
            )
    except Exception as e:
        log.warning("log_prediction error: %s", e)


def resolve_prediction(ticker: str, window_start: str, actual_direction: str, actual_delta: float):
    """Resolve an open prediction with the actual outcome."""
    with _conn() as c:
        row = c.execute(
            "SELECT id, pred_direction FROM crypto_predictions "
            "WHERE ticker = ? AND window_start = ? AND was_correct IS NULL LIMIT 1",
            (ticker, window_start),
        ).fetchone()
        if not row:
            return
        was_correct = 1 if row["pred_direction"] == actual_direction else 0
        c.execute(
            """UPDATE crypto_predictions
               SET actual_direction = ?, actual_delta = ?, was_correct = ?,
                   resolved_at = ?
               WHERE id = ?""",
            (actual_direction, actual_delta, was_correct,
             datetime.now(timezone.utc).isoformat(), row["id"]),
        )


def get_accuracy_stats(ticker: str = None, days: int = 30) -> dict:
    """Compute accuracy statistics from resolved predictions."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with _conn() as c:
        if ticker:
            rows = c.execute(
                "SELECT * FROM crypto_predictions "
                "WHERE was_correct IS NOT NULL AND created_at > ? AND ticker = ? "
                "ORDER BY created_at DESC",
                (since, ticker),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM crypto_predictions "
                "WHERE was_correct IS NOT NULL AND created_at > ? "
                "ORDER BY created_at DESC",
                (since,),
            ).fetchall()

    if not rows:
        return {"total": 0, "correct": 0, "accuracy": 0,
                "high_conf_total": 0, "high_conf_correct": 0, "high_conf_accuracy": 0}

    total = len(rows)
    correct = sum(1 for r in rows if r["was_correct"])
    hc = [r for r in rows if (r["confidence"] or 0) >= 0.6]
    hc_correct = sum(1 for r in hc if r["was_correct"])

    return {
        "total": total,
        "correct": correct,
        "accuracy": correct / total if total else 0,
        "high_conf_total": len(hc),
        "high_conf_correct": hc_correct,
        "high_conf_accuracy": hc_correct / len(hc) if hc else 0,
        "avg_mae": sum(abs((r["pred_delta"] or 0) - (r["actual_delta"] or 0)) for r in rows) / total,
    }


def get_recent_predictions(ticker: str = None, limit: int = 50) -> list:
    """Fetch the most recent predictions."""
    with _conn() as c:
        if ticker:
            rows = c.execute(
                "SELECT * FROM crypto_predictions WHERE ticker = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (ticker, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM crypto_predictions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return _rows(rows)


# ─── Watchlists ──────────────────────────────────────────────────────

def get_watchlists(user_id: str) -> list:
    """Get all watchlists for a user."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM crypto_watchlists WHERE user_id = ?", (user_id,)
        ).fetchall()
    return _rows(rows)


def create_watchlist(user_id: str, name: str, tickers: list) -> int:
    """Create a new watchlist. Returns the new row ID."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO crypto_watchlists (user_id, name, tickers) VALUES (?, ?, ?)",
            (user_id, name, json.dumps(tickers)),
        )
        return cur.lastrowid or 0


def update_watchlist(watchlist_id: int, user_id: str, tickers: list):
    """Update the tickers in a watchlist (owner-scoped)."""
    with _conn() as c:
        c.execute(
            "UPDATE crypto_watchlists SET tickers = ? WHERE id = ? AND user_id = ?",
            (json.dumps(tickers), watchlist_id, user_id),
        )


def delete_watchlist(watchlist_id: int, user_id: str):
    """Delete a watchlist (owner-scoped)."""
    with _conn() as c:
        c.execute(
            "DELETE FROM crypto_watchlists WHERE id = ? AND user_id = ?",
            (watchlist_id, user_id),
        )


# ─── Alert Preferences ──────────────────────────────────────────────

def get_alert_prefs(user_id: str) -> list:
    """Get all alert preferences for a user."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM crypto_alert_preferences WHERE user_id = ?", (user_id,)
        ).fetchall()
    return _rows(rows)


def set_alert_pref(user_id: str, ticker: str, min_confidence: float = 0.6,
                   alert_email: bool = True, alert_browser: bool = True):
    """Upsert an alert preference for a user+ticker pair."""
    with _conn() as c:
        c.execute(
            """INSERT INTO crypto_alert_preferences
               (user_id, ticker, min_confidence, alert_email, alert_browser)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, ticker) DO UPDATE SET
                   min_confidence = excluded.min_confidence,
                   alert_email    = excluded.alert_email,
                   alert_browser  = excluded.alert_browser""",
            (user_id, ticker, min_confidence,
             1 if alert_email else 0, 1 if alert_browser else 0),
        )


def get_alert_prefs_for_ticker(ticker: str) -> list:
    """Get all alert preferences for a specific ticker (across all users),
    joining with profiles to get the email."""
    with _conn() as c:
        rows = c.execute(
            """SELECT a.*, COALESCE(p.email, '') AS email
               FROM crypto_alert_preferences a
               LEFT JOIN profiles p ON p.id = a.user_id
               WHERE a.ticker = ? AND a.alert_email = 1""",
            (ticker,),
        ).fetchall()
    return _rows(rows)


def log_alert(user_id: str | None, ticker: str, alert_type: str, message: str, confidence: float = 0):
    """Log an alert that was sent."""
    with _conn() as c:
        c.execute(
            "INSERT INTO crypto_alert_history (user_id, ticker, alert_type, message, confidence) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, ticker, alert_type, message, confidence),
        )


# ─── Kalshi ──────────────────────────────────────────────────────────

def upsert_kalshi_market(ticker: str, title: str, category: str, status: str,
                         yes_price: float, no_price: float, volume: int, data: dict):
    """Insert or update a Kalshi market entry."""
    with _conn() as c:
        c.execute(
            """INSERT INTO crypto_kalshi_markets
               (ticker, title, category, status, yes_price, no_price, volume, data, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                   title        = excluded.title,
                   category     = excluded.category,
                   status       = excluded.status,
                   yes_price    = excluded.yes_price,
                   no_price     = excluded.no_price,
                   volume       = excluded.volume,
                   data         = excluded.data,
                   last_updated = excluded.last_updated""",
            (ticker, title, category, status, yes_price, no_price, volume,
             json.dumps(data), datetime.now(timezone.utc).isoformat()),
        )


def get_kalshi_markets(category: str = None, limit: int = 100) -> list:
    """Fetch Kalshi markets, optionally filtered by category."""
    with _conn() as c:
        if category:
            rows = c.execute(
                "SELECT * FROM crypto_kalshi_markets WHERE category = ? "
                "ORDER BY volume DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM crypto_kalshi_markets ORDER BY volume DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return _rows(rows)


# ─── User lookup (reads from local profiles table) ────────────────

def get_user(user_id: str) -> dict | None:
    """Look up a user profile by UUID. Used for email alert lookups."""
    with _conn() as c:
        row = c.execute(
            "SELECT id, email, username FROM profiles WHERE id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
    if row:
        return {
            "id": row["id"],
            "email": row["email"],
            "display_name": row["username"] or "",
            "tier": "admin",  # tier is managed by gateway subscriptions now
        }
    return None


# ─── News-Trade Alerts ──────────────────────────────────────────────

def upsert_news_alert(alert: dict):
    """Insert or update a news-trade alert."""
    with _conn() as c:
        c.execute(
            """INSERT INTO news_trade_alerts
               (id, title, link, source, published, description, score,
                keywords, event_keywords, reasons, amounts, related_markets, scanned_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   score           = MAX(excluded.score, news_trade_alerts.score),
                   related_markets = excluded.related_markets,
                   scanned_at      = excluded.scanned_at""",
            (alert["id"], alert["title"], alert.get("link", ""),
             alert.get("source", ""), alert.get("published", ""),
             alert.get("description", ""), alert.get("score", 0),
             json.dumps(alert.get("insider_keywords", [])),
             json.dumps(alert.get("event_keywords", [])),
             json.dumps(alert.get("reasons", [])),
             json.dumps(alert.get("amounts", [])),
             json.dumps(alert.get("related_markets", [])),
             alert.get("scanned_at", datetime.now(timezone.utc).isoformat())),
        )


def get_news_alerts(min_score: int = 0, limit: int = 50, hours: int = 72) -> list:
    """Fetch recent news-trade alerts sorted by score."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM news_trade_alerts WHERE score >= ? AND scanned_at > ? "
            "ORDER BY score DESC, scanned_at DESC LIMIT ?",
            (min_score, since, limit),
        ).fetchall()
    result = []
    for r in _rows(rows):
        # Parse JSON fields
        for field in ("keywords", "event_keywords", "reasons", "amounts", "related_markets"):
            try:
                r[field] = json.loads(r.get(field, "[]"))
            except (json.JSONDecodeError, TypeError):
                r[field] = []
        result.append(r)
    return result


def get_unnotified_alerts(min_score: int = 30) -> list:
    """Fetch high-score alerts that haven't been pushed yet."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM news_trade_alerts WHERE score >= ? AND notified = 0 "
            "ORDER BY score DESC LIMIT 20",
            (min_score,),
        ).fetchall()
    result = []
    for r in _rows(rows):
        for field in ("keywords", "event_keywords", "reasons", "amounts", "related_markets"):
            try:
                r[field] = json.loads(r.get(field, "[]"))
            except (json.JSONDecodeError, TypeError):
                r[field] = []
        result.append(r)
    return result


def mark_alert_notified(alert_id: str):
    """Mark a news-trade alert as notified."""
    with _conn() as c:
        c.execute("UPDATE news_trade_alerts SET notified = 1 WHERE id = ?", (alert_id,))


# ─── News-Trade Watchlist ──────────────────────────────────────────

def get_news_watchlist(user_id: str) -> list:
    """Get a user's news-trade watchlist with alert details."""
    with _conn() as c:
        rows = c.execute(
            """SELECT w.*, a.title, a.link, a.source, a.score, a.published,
                      a.description, a.related_markets, a.reasons, a.keywords
               FROM news_trade_watchlist w
               JOIN news_trade_alerts a ON a.id = w.alert_id
               WHERE w.user_id = ?
               ORDER BY a.score DESC""",
            (user_id,),
        ).fetchall()
    result = []
    for r in _rows(rows):
        for field in ("related_markets", "reasons", "keywords"):
            try:
                r[field] = json.loads(r.get(field, "[]"))
            except (json.JSONDecodeError, TypeError):
                r[field] = []
        result.append(r)
    return result


def add_to_news_watchlist(user_id: str, alert_id: str, notes: str = "",
                          notify_email: bool = True, notify_push: bool = True) -> bool:
    """Add an alert to a user's watchlist. Returns True if added, False if duplicate."""
    try:
        with _conn() as c:
            cursor = c.execute(
                """INSERT OR IGNORE INTO news_trade_watchlist
                   (user_id, alert_id, notes, notify_email, notify_push)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, alert_id, notes,
                 1 if notify_email else 0, 1 if notify_push else 0),
            )
        return cursor.rowcount > 0
    except Exception:
        return False


def remove_from_news_watchlist(user_id: str, alert_id: str):
    """Remove an alert from a user's watchlist."""
    with _conn() as c:
        c.execute(
            "DELETE FROM news_trade_watchlist WHERE user_id = ? AND alert_id = ?",
            (user_id, alert_id),
        )


def get_watchlist_users_for_alert(alert_id: str) -> list:
    """Get all users watching a specific alert (for push notifications)."""
    with _conn() as c:
        rows = c.execute(
            """SELECT w.user_id, w.notify_email, w.notify_push,
                      COALESCE(p.email, '') AS email
               FROM news_trade_watchlist w
               LEFT JOIN profiles p ON p.id = w.user_id
               WHERE w.alert_id = ?""",
            (alert_id,),
        ).fetchall()
    return _rows(rows)


# ─── CLOB Credentials ──────────────────────────────────────────────

def save_clob_credentials(user_id: str, encrypted: str):
    """Save encrypted CLOB API credentials for a user."""
    with _conn() as c:
        c.execute(
            """INSERT INTO clob_credentials (user_id, encrypted, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(user_id) DO UPDATE SET
                   encrypted = excluded.encrypted,
                   updated_at = datetime('now')""",
            (user_id, encrypted),
        )


def get_clob_credentials(user_id: str) -> Optional[str]:
    """Get encrypted CLOB credentials for a user. Returns the encrypted blob."""
    with _conn() as c:
        row = c.execute(
            "SELECT encrypted FROM clob_credentials WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row["encrypted"] if row else None


def delete_clob_credentials(user_id: str):
    """Delete CLOB credentials for a user."""
    with _conn() as c:
        c.execute("DELETE FROM clob_credentials WHERE user_id = ?", (user_id,))


def has_clob_credentials(user_id: str) -> bool:
    """Check if a user has CLOB credentials stored."""
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM clob_credentials WHERE user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
    return row is not None


# ─── Kalshi Credentials ──────────────────────────────────────────────

def save_kalshi_credentials(user_id: str, encrypted: str):
    """Save encrypted Kalshi API credentials for a user."""
    with _conn() as c:
        c.execute(
            """INSERT INTO kalshi_credentials (user_id, encrypted, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(user_id) DO UPDATE SET
                   encrypted = excluded.encrypted,
                   updated_at = datetime('now')""",
            (user_id, encrypted),
        )


def get_kalshi_credentials(user_id: str) -> Optional[str]:
    """Get encrypted Kalshi credentials for a user. Returns the encrypted blob."""
    with _conn() as c:
        row = c.execute(
            "SELECT encrypted FROM kalshi_credentials WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row["encrypted"] if row else None


def delete_kalshi_credentials(user_id: str):
    """Delete Kalshi credentials for a user."""
    with _conn() as c:
        c.execute("DELETE FROM kalshi_credentials WHERE user_id = ?", (user_id,))


def has_kalshi_credentials(user_id: str) -> bool:
    """Check if a user has Kalshi credentials stored."""
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM kalshi_credentials WHERE user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
    return row is not None


# ─── CLOB Trade Log ───────────────────────────────────────────────

def log_clob_trade(user_id: str, order_id: str, condition_id: str,
                   token_id: str, market_question: str, outcome: str,
                   side: str, order_type: str, price: float,
                   size: float, amount: float, status: str,
                   response_data: dict) -> int:
    """Log a CLOB trade. Returns the log row ID."""
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO clob_trade_log
               (user_id, order_id, condition_id, token_id, market_question,
                outcome, side, order_type, price, size, amount, status, response_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, order_id, condition_id, token_id, market_question,
             outcome, side, order_type, price, size, amount, status,
             json.dumps(response_data)),
        )
        return cur.lastrowid or 0


def get_clob_trades(user_id: str, limit: int = 50) -> list:
    """Get recent CLOB trades for a user."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM clob_trade_log WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    result = []
    for r in _rows(rows):
        try:
            r["response_data"] = json.loads(r.get("response_data", "{}"))
        except (json.JSONDecodeError, TypeError):
            r["response_data"] = {}
        result.append(r)
    return result


def update_clob_trade_status(trade_id: int, status: str, response_data: dict = None):
    """Update the status of a logged trade."""
    with _conn() as c:
        if response_data:
            c.execute(
                "UPDATE clob_trade_log SET status = ?, response_data = ? WHERE id = ?",
                (status, json.dumps(response_data), trade_id),
            )
        else:
            c.execute(
                "UPDATE clob_trade_log SET status = ? WHERE id = ?",
                (status, trade_id),
            )


# ─── CLOB Favorites ──────────────────────────────────────────────

def get_clob_favorites(user_id: str) -> list:
    """Get a user's favorite markets."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM clob_favorites WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return _rows(rows)


def add_clob_favorite(user_id: str, condition_id: str, question: str) -> bool:
    """Add a market to favorites. Returns True if added."""
    try:
        with _conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO clob_favorites (user_id, condition_id, question) "
                "VALUES (?, ?, ?)",
                (user_id, condition_id, question),
            )
        return True
    except Exception:
        return False


def remove_clob_favorite(user_id: str, condition_id: str):
    """Remove a market from favorites."""
    with _conn() as c:
        c.execute(
            "DELETE FROM clob_favorites WHERE user_id = ? AND condition_id = ?",
            (user_id, condition_id),
        )


# ── Long-term: daily bars ───────────────────────────────────────────────────

def upsert_daily_bars(rows: list[tuple]) -> None:
    """Bulk upsert. rows: list of (ticker, date, open, high, low, close, volume)."""
    if not rows:
        return
    with _conn() as c:
        c.executemany(
            """INSERT INTO crypto_daily_bars (ticker, date, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ticker, date) DO UPDATE SET
                 open=excluded.open, high=excluded.high, low=excluded.low,
                 close=excluded.close, volume=excluded.volume""",
            rows,
        )


def get_daily_bars(ticker: str, days: int = 365 * 4) -> list[Row]:
    """Oldest→newest. `days` is just a window cap; we still return everything stored
    if you have less than that."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT date, open, high, low, close, volume FROM crypto_daily_bars "
            "WHERE ticker = ? AND date >= ? ORDER BY date ASC",
            (ticker, cutoff),
        ).fetchall()
    return _rows(rows)


def get_latest_daily_bar_date(ticker: str) -> Optional[str]:
    with _conn() as c:
        row = c.execute(
            "SELECT MAX(date) AS d FROM crypto_daily_bars WHERE ticker = ?",
            (ticker,),
        ).fetchone()
    return row["d"] if row and row["d"] else None


# ── Long-term: on-chain metrics ─────────────────────────────────────────────

def upsert_onchain_metrics(rows: list[tuple]) -> None:
    """Bulk upsert. rows: list of (ticker, metric, date, value)."""
    if not rows:
        return
    with _conn() as c:
        c.executemany(
            """INSERT INTO crypto_onchain_metrics (ticker, metric, date, value)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(ticker, metric, date) DO UPDATE SET value=excluded.value""",
            rows,
        )


def get_onchain_metric(ticker: str, metric: str, days: int = 365) -> list[Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT date, value FROM crypto_onchain_metrics "
            "WHERE ticker = ? AND metric = ? AND date >= ? ORDER BY date ASC",
            (ticker, metric, cutoff),
        ).fetchall()
    return _rows(rows)


def get_latest_onchain_date(ticker: str) -> Optional[str]:
    with _conn() as c:
        row = c.execute(
            "SELECT MAX(date) AS d FROM crypto_onchain_metrics WHERE ticker = ?",
            (ticker,),
        ).fetchone()
    return row["d"] if row and row["d"] else None


# ── Long-term: holdings (lot-aware) ─────────────────────────────────────────

def add_holding(user_id: str, ticker: str, qty: float, cost_basis: float,
                acquired_at: str, note: str = "") -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO crypto_holdings (user_id, ticker, qty, cost_basis, acquired_at, note)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, ticker, qty, cost_basis, acquired_at, note),
        )
        return int(cur.lastrowid)


def remove_holding(user_id: str, holding_id: int) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM crypto_holdings WHERE id = ? AND user_id = ?",
            (holding_id, user_id),
        )


def get_holdings(user_id: str) -> list[Row]:
    """All lots, oldest first — caller can roll up by ticker."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ticker, qty, cost_basis, acquired_at, note "
            "FROM crypto_holdings WHERE user_id = ? ORDER BY acquired_at ASC, id ASC",
            (user_id,),
        ).fetchall()
    return _rows(rows)


def get_holdings_rollup(user_id: str) -> list[dict]:
    """Sum qty and weighted-avg cost basis per ticker."""
    lots = get_holdings(user_id)
    rollup: dict[str, dict] = {}
    for lot in lots:
        t = lot["ticker"]
        agg = rollup.setdefault(t, {"ticker": t, "qty": 0.0, "cost_total": 0.0, "lots": 0})
        agg["qty"] += float(lot["qty"])
        agg["cost_total"] += float(lot["qty"]) * float(lot["cost_basis"])
        agg["lots"] += 1
    out = []
    for t, agg in rollup.items():
        avg_cost = agg["cost_total"] / agg["qty"] if agg["qty"] else 0.0
        out.append({
            "ticker": t, "qty": agg["qty"],
            "avg_cost_basis": avg_cost, "cost_total": agg["cost_total"],
            "lots": agg["lots"],
        })
    return out


# ── Long-term: target weights ───────────────────────────────────────────────

def set_target_weight(user_id: str, ticker: str, weight: float, drift_band: float = 0.05) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO crypto_target_weights (user_id, ticker, weight, drift_band, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(user_id, ticker) DO UPDATE SET
                 weight=excluded.weight, drift_band=excluded.drift_band,
                 updated_at=datetime('now')""",
            (user_id, ticker, weight, drift_band),
        )


def remove_target_weight(user_id: str, ticker: str) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM crypto_target_weights WHERE user_id = ? AND ticker = ?",
            (user_id, ticker),
        )


def get_target_weights(user_id: str) -> list[Row]:
    with _conn() as c:
        rows = c.execute(
            "SELECT ticker, weight, drift_band, updated_at "
            "FROM crypto_target_weights WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return _rows(rows)


# ── Long-term: DCA schedule ─────────────────────────────────────────────────

def upsert_dca_schedule(user_id: str, ticker: str, frequency: str, base_amount_usd: float,
                        use_multiplier: bool = True, active: bool = True) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO crypto_dca_schedule
                 (user_id, ticker, frequency, base_amount_usd, use_multiplier, active)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, ticker) DO UPDATE SET
                 frequency=excluded.frequency,
                 base_amount_usd=excluded.base_amount_usd,
                 use_multiplier=excluded.use_multiplier,
                 active=excluded.active""",
            (user_id, ticker, frequency, base_amount_usd,
             1 if use_multiplier else 0, 1 if active else 0),
        )


def remove_dca_schedule(user_id: str, ticker: str) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM crypto_dca_schedule WHERE user_id = ? AND ticker = ?",
            (user_id, ticker),
        )


def get_dca_schedules(user_id: str) -> list[Row]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ticker, frequency, base_amount_usd, use_multiplier, active, "
            "       next_run_at, last_run_at "
            "FROM crypto_dca_schedule WHERE user_id = ? ORDER BY ticker",
            (user_id,),
        ).fetchall()
    return _rows(rows)


def mark_dca_run(user_id: str, ticker: str, next_run_at: str) -> None:
    with _conn() as c:
        c.execute(
            """UPDATE crypto_dca_schedule
               SET last_run_at = datetime('now'), next_run_at = ?
               WHERE user_id = ? AND ticker = ?""",
            (next_run_at, user_id, ticker),
        )


# ── Long-term: alerts ───────────────────────────────────────────────────────

def upsert_long_term_alert(user_id: str, ticker: str, alert_type: str,
                           threshold: Optional[float], active: bool = True) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO crypto_long_term_alerts (user_id, ticker, alert_type, threshold, active)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, ticker, alert_type) DO UPDATE SET
                 threshold=excluded.threshold, active=excluded.active""",
            (user_id, ticker, alert_type, threshold, 1 if active else 0),
        )


def get_long_term_alerts(user_id: str | None = None) -> list[Row]:
    with _conn() as c:
        if user_id:
            rows = c.execute(
                "SELECT id, user_id, ticker, alert_type, threshold, last_fired_at, active "
                "FROM crypto_long_term_alerts WHERE user_id = ? AND active = 1",
                (user_id,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, user_id, ticker, alert_type, threshold, last_fired_at, active "
                "FROM crypto_long_term_alerts WHERE active = 1",
            ).fetchall()
    return _rows(rows)


def mark_long_term_alert_fired(alert_id: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE crypto_long_term_alerts SET last_fired_at = datetime('now') WHERE id = ?",
            (alert_id,),
        )


def remove_long_term_alert(user_id: str, ticker: str, alert_type: str) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM crypto_long_term_alerts "
            "WHERE user_id = ? AND ticker = ? AND alert_type = ?",
            (user_id, ticker, alert_type),
        )


# ── Stubs for removed functions (gateway handles auth now) ──────────────────
# These are kept as no-ops so any residual server.py calls don't crash.

def validate_session(token: str) -> dict | None:
    """Sessions are managed by the gateway. This is a no-op stub."""
    return None


def create_session(user_id: str, ip: str = "", user_agent: str = "", max_age: int = 604800) -> str:
    """Sessions are managed by the gateway. This is a no-op stub."""
    return ""


def delete_session(token: str):
    """Sessions are managed by the gateway. This is a no-op stub."""
    pass


def create_user(email: str, password: str, display_name: str = "", tier: str = "free") -> str | None:
    """User creation is managed by the gateway. This is a no-op stub."""
    return None


def cleanup_sessions():
    """Sessions are managed by the gateway. This is a no-op stub."""
    pass

#!/usr/bin/env python3
"""
Database layer for CryptoEdge.
SQLite-backed storage for users, predictions, alerts, watchlists, and accuracy tracking.
"""

import sqlite3
import hashlib
import secrets
import time
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

DB_PATH = Path(__file__).parent / "cryptoedge.db"


@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create all tables if they don't exist."""
    with get_db() as db:
        db.executescript("""
        -- Users
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            display_name TEXT DEFAULT '',
            tier TEXT DEFAULT 'free' CHECK(tier IN ('free', 'premium', 'admin')),
            created_at TEXT DEFAULT (datetime('now')),
            last_login TEXT,
            email_verified INTEGER DEFAULT 0,
            email_verify_token TEXT,
            reset_token TEXT,
            reset_token_expiry TEXT,
            settings TEXT DEFAULT '{}'
        );

        -- Sessions
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT
        );

        -- Prediction log (for accuracy tracking)
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            window_start TEXT NOT NULL,
            pred_direction TEXT NOT NULL,
            pred_delta REAL NOT NULL,
            pred_prob REAL NOT NULL,
            confidence REAL NOT NULL,
            ensemble_agreement TEXT,
            model_details TEXT,
            actual_direction TEXT,
            actual_delta REAL,
            was_correct INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_predictions_ticker ON predictions(ticker);
        CREATE INDEX IF NOT EXISTS idx_predictions_resolved ON predictions(was_correct);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_predictions_unique ON predictions(ticker, window_start);

        -- User watchlists
        CREATE TABLE IF NOT EXISTS watchlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT DEFAULT 'Default',
            tickers TEXT NOT NULL DEFAULT '[]',
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- User alert preferences
        CREATE TABLE IF NOT EXISTS alert_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            ticker TEXT NOT NULL,
            min_confidence REAL DEFAULT 0.6,
            alert_email INTEGER DEFAULT 1,
            alert_browser INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, ticker)
        );

        -- Alert history
        CREATE TABLE IF NOT EXISTS alert_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            ticker TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            message TEXT NOT NULL,
            confidence REAL,
            delivered INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- Accuracy stats (daily rollup)
        CREATE TABLE IF NOT EXISTS accuracy_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            total_predictions INTEGER DEFAULT 0,
            correct_predictions INTEGER DEFAULT 0,
            high_conf_total INTEGER DEFAULT 0,
            high_conf_correct INTEGER DEFAULT 0,
            avg_confidence REAL DEFAULT 0,
            avg_mae REAL DEFAULT 0,
            UNIQUE(ticker, date)
        );

        -- Kalshi markets cache
        CREATE TABLE IF NOT EXISTS kalshi_markets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            title TEXT NOT NULL,
            category TEXT,
            status TEXT,
            yes_price REAL,
            no_price REAL,
            volume INTEGER DEFAULT 0,
            last_updated TEXT DEFAULT (datetime('now')),
            data TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_kalshi_ticker ON kalshi_markets(ticker);
        """)
    print("[DB] Database initialized.")


# ─── User Management ─────────────────────────────────────────────────

def _hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()


def create_user(email: str, password: str, display_name: str = "", tier: str = "free") -> int | None:
    salt = secrets.token_hex(16)
    pw_hash = _hash_pw(password, salt)
    verify_token = secrets.token_urlsafe(32)
    try:
        with get_db() as db:
            cur = db.execute(
                "INSERT INTO users (email, password_hash, salt, display_name, tier, email_verify_token) VALUES (?, ?, ?, ?, ?, ?)",
                (email.lower().strip(), pw_hash, salt, display_name, tier, verify_token),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # email already exists


def verify_user(email: str, password: str) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE email = ?", (email.lower().strip(),)).fetchone()
        if not row:
            return None
        if _hash_pw(password, row["salt"]) != row["password_hash"]:
            return None
        db.execute("UPDATE users SET last_login = datetime('now') WHERE id = ?", (row["id"],))
        return dict(row)


def get_user(user_id: int) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_email(email: str) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE email = ?", (email.lower().strip(),)).fetchone()
        return dict(row) if row else None


def update_user_tier(user_id: int, tier: str):
    with get_db() as db:
        db.execute("UPDATE users SET tier = ? WHERE id = ?", (tier, user_id))


def update_user_settings(user_id: int, settings: dict):
    with get_db() as db:
        db.execute("UPDATE users SET settings = ? WHERE id = ?", (json.dumps(settings), user_id))


def set_reset_token(email: str) -> str | None:
    token = secrets.token_urlsafe(32)
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with get_db() as db:
        cur = db.execute(
            "UPDATE users SET reset_token = ?, reset_token_expiry = ? WHERE email = ?",
            (token, expiry, email.lower().strip()),
        )
        return token if cur.rowcount > 0 else None


def reset_password(token: str, new_password: str) -> bool:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM users WHERE reset_token = ? AND reset_token_expiry > datetime('now')",
            (token,),
        ).fetchone()
        if not row:
            return False
        salt = secrets.token_hex(16)
        pw_hash = _hash_pw(new_password, salt)
        db.execute(
            "UPDATE users SET password_hash = ?, salt = ?, reset_token = NULL, reset_token_expiry = NULL WHERE id = ?",
            (pw_hash, salt, row["id"]),
        )
        return True


# ─── Sessions ────────────────────────────────────────────────────────

def create_session(user_id: int, ip: str = "", user_agent: str = "", max_age: int = 604800) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(seconds=max_age)).isoformat()
    with get_db() as db:
        db.execute(
            "INSERT INTO sessions (token, user_id, expires_at, ip_address, user_agent) VALUES (?, ?, ?, ?, ?)",
            (token, user_id, expires, ip, user_agent),
        )
    return token


def validate_session(token: str) -> dict | None:
    if not token:
        return None
    with get_db() as db:
        row = db.execute(
            "SELECT s.*, u.email, u.tier, u.display_name FROM sessions s JOIN users u ON s.user_id = u.id WHERE s.token = ? AND s.expires_at > datetime('now')",
            (token,),
        ).fetchone()
        return dict(row) if row else None


def delete_session(token: str):
    with get_db() as db:
        db.execute("DELETE FROM sessions WHERE token = ?", (token,))


def cleanup_sessions():
    with get_db() as db:
        db.execute("DELETE FROM sessions WHERE expires_at < datetime('now')")


# ─── Predictions & Accuracy ─────────────────────────────────────────

def log_prediction(ticker: str, window_start: str, pred_direction: str,
                   pred_delta: float, pred_prob: float, confidence: float,
                   ensemble_agreement: str = "", model_details: str = ""):
    with get_db() as db:
        db.execute(
            """INSERT OR IGNORE INTO predictions
               (ticker, window_start, pred_direction, pred_delta, pred_prob, confidence, ensemble_agreement, model_details)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticker, window_start, pred_direction, pred_delta, pred_prob, confidence, ensemble_agreement, model_details),
        )


def resolve_prediction(ticker: str, window_start: str, actual_direction: str, actual_delta: float):
    was_correct = 1 if actual_direction else 0
    with get_db() as db:
        row = db.execute(
            "SELECT pred_direction FROM predictions WHERE ticker = ? AND window_start = ? AND was_correct IS NULL",
            (ticker, window_start),
        ).fetchone()
        if row:
            was_correct = 1 if row["pred_direction"] == actual_direction else 0
            db.execute(
                "UPDATE predictions SET actual_direction = ?, actual_delta = ?, was_correct = ?, resolved_at = datetime('now') WHERE ticker = ? AND window_start = ?",
                (actual_direction, actual_delta, was_correct, ticker, window_start),
            )


def get_accuracy_stats(ticker: str = None, days: int = 30) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_db() as db:
        if ticker:
            rows = db.execute(
                "SELECT * FROM predictions WHERE ticker = ? AND was_correct IS NOT NULL AND created_at > ? ORDER BY created_at DESC",
                (ticker, since),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM predictions WHERE was_correct IS NOT NULL AND created_at > ? ORDER BY created_at DESC",
                (since,),
            ).fetchall()

    if not rows:
        return {"total": 0, "correct": 0, "accuracy": 0, "high_conf_total": 0, "high_conf_correct": 0, "high_conf_accuracy": 0}

    total = len(rows)
    correct = sum(1 for r in rows if r["was_correct"])
    hc = [r for r in rows if r["confidence"] >= 0.6]
    hc_correct = sum(1 for r in hc if r["was_correct"])

    return {
        "total": total,
        "correct": correct,
        "accuracy": correct / total if total else 0,
        "high_conf_total": len(hc),
        "high_conf_correct": hc_correct,
        "high_conf_accuracy": hc_correct / len(hc) if hc else 0,
        "avg_mae": sum(abs(r["pred_delta"] - (r["actual_delta"] or 0)) for r in rows) / total,
    }


def get_recent_predictions(ticker: str = None, limit: int = 50) -> list:
    with get_db() as db:
        if ticker:
            rows = db.execute(
                "SELECT * FROM predictions WHERE ticker = ? ORDER BY created_at DESC LIMIT ?",
                (ticker, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM predictions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


# ─── Watchlists ──────────────────────────────────────────────────────

def get_watchlists(user_id: int) -> list:
    with get_db() as db:
        rows = db.execute("SELECT * FROM watchlists WHERE user_id = ?", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def create_watchlist(user_id: int, name: str, tickers: list) -> int:
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO watchlists (user_id, name, tickers) VALUES (?, ?, ?)",
            (user_id, name, json.dumps(tickers)),
        )
        return cur.lastrowid


def update_watchlist(watchlist_id: int, user_id: int, tickers: list):
    with get_db() as db:
        db.execute(
            "UPDATE watchlists SET tickers = ? WHERE id = ? AND user_id = ?",
            (json.dumps(tickers), watchlist_id, user_id),
        )


def delete_watchlist(watchlist_id: int, user_id: int):
    with get_db() as db:
        db.execute("DELETE FROM watchlists WHERE id = ? AND user_id = ?", (watchlist_id, user_id))


# ─── Alert Preferences ──────────────────────────────────────────────

def get_alert_prefs(user_id: int) -> list:
    with get_db() as db:
        rows = db.execute("SELECT * FROM alert_preferences WHERE user_id = ?", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def set_alert_pref(user_id: int, ticker: str, min_confidence: float = 0.6,
                   alert_email: bool = True, alert_browser: bool = True):
    with get_db() as db:
        db.execute(
            """INSERT INTO alert_preferences (user_id, ticker, min_confidence, alert_email, alert_browser)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, ticker) DO UPDATE SET min_confidence=?, alert_email=?, alert_browser=?""",
            (user_id, ticker, min_confidence, int(alert_email), int(alert_browser),
             min_confidence, int(alert_email), int(alert_browser)),
        )


def get_alert_prefs_for_ticker(ticker: str) -> list:
    """Get all alert preferences for a specific ticker (across all users)."""
    with get_db() as db:
        rows = db.execute(
            "SELECT ap.*, u.email FROM alert_preferences ap JOIN users u ON ap.user_id = u.id WHERE ap.ticker = ? AND ap.alert_email = 1",
            (ticker,),
        ).fetchall()
    return [dict(r) for r in rows]


def log_alert(user_id: int | None, ticker: str, alert_type: str, message: str, confidence: float = 0):
    with get_db() as db:
        db.execute(
            "INSERT INTO alert_history (user_id, ticker, alert_type, message, confidence) VALUES (?, ?, ?, ?, ?)",
            (user_id, ticker, alert_type, message, confidence),
        )


# ─── Kalshi ──────────────────────────────────────────────────────────

def upsert_kalshi_market(ticker: str, title: str, category: str, status: str,
                         yes_price: float, no_price: float, volume: int, data: dict):
    with get_db() as db:
        db.execute(
            """INSERT INTO kalshi_markets (ticker, title, category, status, yes_price, no_price, volume, data, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(id) DO UPDATE SET title=?, yes_price=?, no_price=?, volume=?, data=?, last_updated=datetime('now')""",
            (ticker, title, category, status, yes_price, no_price, volume, json.dumps(data),
             title, yes_price, no_price, volume, json.dumps(data)),
        )


def get_kalshi_markets(category: str = None, limit: int = 100) -> list:
    with get_db() as db:
        if category:
            rows = db.execute(
                "SELECT * FROM kalshi_markets WHERE category = ? ORDER BY volume DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM kalshi_markets ORDER BY volume DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


# Initialize on import
init_db()

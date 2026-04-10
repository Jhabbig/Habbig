from __future__ import annotations
"""SQLite database layer for the Midterm Elections Dashboard.

Uses sqlite3 with WAL mode, a threading lock, and a contextmanager for
connections.  The DB file lives at ``data.db`` in the backend directory.

User auth (users, sessions) is NO LONGER handled here -- the gateway
manages that.  User profiles live in the shared ``profiles`` table.
User IDs are UUID strings, not integers.
"""

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB path
# ---------------------------------------------------------------------------

_DB_DIR = Path(__file__).resolve().parent
DB_PATH = _DB_DIR / "data.db"

_lock = threading.Lock()


@contextmanager
def _get_conn():
    """Yield a sqlite3 connection with WAL mode and row_factory set."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS midterm_markets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    event_id        TEXT,
    title           TEXT NOT NULL,
    event_title     TEXT,
    slug            TEXT,
    race_type       TEXT,
    state           TEXT,
    outcomes        TEXT,           -- JSON array stored as TEXT
    volume          REAL DEFAULT 0,
    liquidity       REAL DEFAULT 0,
    active          INTEGER DEFAULT 1,
    closed          INTEGER DEFAULT 0,
    end_date        TEXT,
    last_updated    TEXT,
    UNIQUE(source, source_id)
);

CREATE TABLE IF NOT EXISTS midterm_price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id   INTEGER NOT NULL,
    source      TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    prices      TEXT,               -- JSON object stored as TEXT
    volume      REAL
);

CREATE TABLE IF NOT EXISTS midterm_polling_data (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_type   TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL DEFAULT '',
    candidate   TEXT NOT NULL DEFAULT '',
    party       TEXT,
    percentage  REAL,
    pollster    TEXT NOT NULL DEFAULT '',
    sample_size INTEGER,
    population  TEXT,
    start_date  TEXT,
    end_date    TEXT NOT NULL DEFAULT '',
    race_id     TEXT,
    source      TEXT DEFAULT '538',
    UNIQUE(poll_type, state, candidate, pollster, end_date)
);

CREATE TABLE IF NOT EXISTS midterm_polling_averages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_type   TEXT,
    state       TEXT,
    candidate   TEXT,
    party       TEXT,
    avg_pct     REAL,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS midterm_divergence_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    race_key            TEXT,
    state               TEXT,
    race_type           TEXT,
    polymarket_prob     REAL,
    kalshi_prob         REAL,
    predictit_prob      REAL,
    polling_avg         REAL,
    max_divergence      REAL,
    divergence_details  TEXT,       -- JSON object stored as TEXT
    snapshot_time       TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS midterm_user_watchlists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    race_key    TEXT NOT NULL,
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(user_id, race_key)
);

CREATE TABLE IF NOT EXISTS midterm_alert_settings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    race_key    TEXT NOT NULL,
    alert_type  TEXT NOT NULL DEFAULT 'divergence',
    threshold   REAL DEFAULT 5.0,
    enabled     INTEGER DEFAULT 1,
    UNIQUE(user_id, race_key, alert_type)
);

CREATE TABLE IF NOT EXISTS midterm_alert_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT,
    race_key    TEXT,
    alert_type  TEXT,
    message     TEXT,
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS midterm_audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT,
    action      TEXT NOT NULL DEFAULT '',
    details     TEXT,
    ip_address  TEXT,
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS profiles (
    id              TEXT PRIMARY KEY,
    email           TEXT,
    display_name    TEXT,
    tier            TEXT,
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_login      TEXT
);

CREATE TABLE IF NOT EXISTS midterm_district_profiles (
    state           TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    profile_data    TEXT NOT NULL,       -- JSON object stored as TEXT
    auto_generated  INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- New jurisdiction table supports US states, US House districts, and countries.
-- jurisdiction_type: 'us_state' | 'us_district' | 'country'
-- jurisdiction_code: 'WY' | 'TX-28' | 'HU'
CREATE TABLE IF NOT EXISTS midterm_jurisdiction_profiles (
    jurisdiction_type TEXT NOT NULL,
    jurisdiction_code TEXT NOT NULL,
    name              TEXT NOT NULL,
    profile_data      TEXT NOT NULL,
    candidates_data   TEXT,
    auto_generated    INTEGER DEFAULT 0,
    created_at        TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at        TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (jurisdiction_type, jurisdiction_code)
);

CREATE INDEX IF NOT EXISTS idx_jurisdiction_type ON midterm_jurisdiction_profiles(jurisdiction_type);

CREATE TABLE IF NOT EXISTS midterm_market_match_flags (
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    race_key        TEXT NOT NULL,
    reviewer_id     TEXT,
    reviewer_email  TEXT,
    note            TEXT,
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (source, source_id, race_key)
);

CREATE INDEX IF NOT EXISTS idx_match_flags_race_key ON midterm_market_match_flags(race_key);

CREATE TABLE IF NOT EXISTS midterm_market_race_verifications (
    race_key        TEXT PRIMARY KEY,
    reviewer_id     TEXT,
    reviewer_email  TEXT,
    note            TEXT,
    verified_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


def _init_db():
    """Create all tables if they don't exist."""
    with _get_conn() as conn:
        conn.executescript(_SCHEMA)


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row)


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------


class Database:
    """SQLite-backed database for the midterm dashboard.

    Keeps the same public API as the previous version so callers
    (main.py, background tasks) require no changes.
    """

    def __init__(self):
        pass

    def connect(self):
        """Create the database file and initialize all tables."""
        _init_db()
        logger.info("SQLite database initialized at %s", DB_PATH)

    def close(self):
        """No-op -- connections are opened and closed per-operation."""
        pass

    # === Helper =============================================================

    @staticmethod
    def _parse_outcomes(row: dict) -> dict:
        """Ensure 'outcomes' field is a Python list, not a JSON string."""
        if row and "outcomes" in row:
            o = row["outcomes"]
            if isinstance(o, str):
                try:
                    row["outcomes"] = json.loads(o)
                except (json.JSONDecodeError, TypeError):
                    pass
        return row

    # === Market Data ========================================================

    def upsert_market(self, market: dict):
        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, (list, dict)):
            outcomes = json.dumps(outcomes)

        row = (
            market["source"],
            market["source_id"],
            market.get("event_id"),
            market["title"],
            market.get("event_title"),
            market.get("slug"),
            market.get("race_type"),
            market.get("state"),
            outcomes,
            market.get("volume", 0),
            market.get("liquidity", 0),
            1 if market.get("active") else 0,
            1 if market.get("closed") else 0,
            market.get("end_date"),
            market.get("last_updated"),
        )
        with _lock:
            with _get_conn() as conn:
                conn.execute(
                    """INSERT INTO midterm_markets
                        (source, source_id, event_id, title, event_title, slug,
                         race_type, state, outcomes, volume, liquidity, active,
                         closed, end_date, last_updated)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(source, source_id) DO UPDATE SET
                         event_id=excluded.event_id,
                         title=excluded.title,
                         event_title=excluded.event_title,
                         slug=excluded.slug,
                         race_type=excluded.race_type,
                         state=excluded.state,
                         outcomes=excluded.outcomes,
                         volume=excluded.volume,
                         liquidity=excluded.liquidity,
                         active=excluded.active,
                         closed=excluded.closed,
                         end_date=excluded.end_date,
                         last_updated=excluded.last_updated
                    """,
                    row,
                )

    def upsert_markets_batch(self, markets: list[dict]):
        for market in markets:
            self.upsert_market(market)

    def get_markets(
        self,
        source: str = None,
        race_type: str = None,
        state: str = None,
        active_only: bool = True,
        search: str = None,
        min_volume: float = None,
    ) -> list[dict]:
        clauses = []
        params = []

        if source:
            clauses.append("source = ?")
            params.append(source)
        if race_type:
            clauses.append("race_type = ?")
            params.append(race_type)
        if state:
            clauses.append("state = ?")
            params.append(state)
        if active_only:
            clauses.append("active = 1 AND (closed IS NULL OR closed = 0)")
        if search:
            # Sanitize: strip PostgREST special characters and SQL wildcards
            sanitized = search
            for ch in ("(", ")", ",", ".", ":", "!", "&", "|", "%", "_"):
                sanitized = sanitized.replace(ch, "")
            sanitized = sanitized.strip()
            clauses.append("(title LIKE ? OR event_title LIKE ?)")
            pattern = f"%{sanitized}%"
            params.extend([pattern, pattern])
        if min_volume is not None:
            clauses.append("volume >= ?")
            params.append(min_volume)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM midterm_markets{where} ORDER BY volume DESC"

        with _get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._parse_outcomes(_row_to_dict(r)) for r in rows]

    def get_all_markets(self, active_only: bool = True) -> list[dict]:
        clauses = []
        if active_only:
            clauses.append("active = 1 AND (closed IS NULL OR closed = 0)")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM midterm_markets{where} ORDER BY volume DESC"

        with _get_conn() as conn:
            rows = conn.execute(sql).fetchall()
        return [self._parse_outcomes(_row_to_dict(r)) for r in rows]

    # === Price History ======================================================

    def record_price_snapshot(
        self, market_id: int, source: str, prices: dict, volume: float = None
    ):
        ts = datetime.now(timezone.utc).isoformat()
        prices_json = json.dumps(prices) if isinstance(prices, (dict, list)) else prices
        with _lock:
            with _get_conn() as conn:
                conn.execute(
                    """INSERT INTO midterm_price_history
                        (market_id, source, timestamp, prices, volume)
                       VALUES (?,?,?,?,?)""",
                    (market_id, source, ts, prices_json, volume),
                )

    def get_price_history(self, market_id: int, days: int = 30) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with _get_conn() as conn:
            rows = conn.execute(
                """SELECT * FROM midterm_price_history
                   WHERE market_id = ? AND timestamp >= ?
                   ORDER BY timestamp""",
                (market_id, cutoff),
            ).fetchall()

        results = []
        for row in rows:
            d = _row_to_dict(row)
            if isinstance(d.get("prices"), str):
                try:
                    d["prices"] = json.loads(d["prices"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        return results

    # === Divergence =========================================================

    def record_divergence(self, race_key: str, state: str, race_type: str, data: dict):
        details = data.get("details", {})
        details_json = json.dumps(details) if isinstance(details, (dict, list)) else details
        with _lock:
            with _get_conn() as conn:
                conn.execute(
                    """INSERT INTO midterm_divergence_snapshots
                        (race_key, state, race_type, polymarket_prob, kalshi_prob,
                         predictit_prob, polling_avg, max_divergence, divergence_details)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        race_key,
                        state,
                        race_type,
                        data.get("polymarket"),
                        data.get("kalshi"),
                        data.get("predictit"),
                        data.get("polling"),
                        data.get("max_divergence"),
                        details_json,
                    ),
                )

    # === Human-review market match flags ====================================

    def flag_market_as_wrong(
        self,
        source: str,
        source_id: str,
        race_key: str,
        reviewer_id: str | None = None,
        reviewer_email: str | None = None,
        note: str | None = None,
    ) -> None:
        """Record that a human reviewed (source, source_id) as NOT belonging
        to *race_key*. The matching layer will exclude this pair from the
        race bucket."""
        with _lock:
            with _get_conn() as conn:
                conn.execute(
                    """INSERT INTO midterm_market_match_flags
                        (source, source_id, race_key, reviewer_id, reviewer_email, note)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source, source_id, race_key) DO UPDATE SET
                         reviewer_id = excluded.reviewer_id,
                         reviewer_email = excluded.reviewer_email,
                         note = excluded.note,
                         created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')""",
                    (source, source_id, race_key, reviewer_id, reviewer_email, note),
                )

    def unflag_market(self, source: str, source_id: str, race_key: str) -> bool:
        """Remove a wrong-market flag. Returns True if a row was deleted."""
        with _lock:
            with _get_conn() as conn:
                cur = conn.execute(
                    "DELETE FROM midterm_market_match_flags WHERE source=? AND source_id=? AND race_key=?",
                    (source, source_id, race_key),
                )
                return cur.rowcount > 0

    def get_flags_for_race(self, race_key: str) -> list[dict]:
        """All flags attached to *race_key*, newest first."""
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM midterm_market_match_flags WHERE race_key=? ORDER BY created_at DESC",
                (race_key,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_all_wrong_flags(self) -> dict[str, set[tuple[str, str]]]:
        """Return race_key → set of (source, source_id) flagged as wrong.

        Called once per matching pass so the scheduler loop isn't fetching
        per-market from SQLite.
        """
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT race_key, source, source_id FROM midterm_market_match_flags"
            ).fetchall()
        out: dict[str, set[tuple[str, str]]] = {}
        for r in rows:
            out.setdefault(r["race_key"], set()).add((r["source"], r["source_id"]))
        return out

    def verify_race(
        self,
        race_key: str,
        reviewer_id: str | None = None,
        reviewer_email: str | None = None,
        note: str | None = None,
    ) -> None:
        """Mark a race_key as human-verified. Upserts."""
        with _lock:
            with _get_conn() as conn:
                conn.execute(
                    """INSERT INTO midterm_market_race_verifications
                        (race_key, reviewer_id, reviewer_email, note)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(race_key) DO UPDATE SET
                         reviewer_id = excluded.reviewer_id,
                         reviewer_email = excluded.reviewer_email,
                         note = excluded.note,
                         verified_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')""",
                    (race_key, reviewer_id, reviewer_email, note),
                )

    def unverify_race(self, race_key: str) -> bool:
        with _lock:
            with _get_conn() as conn:
                cur = conn.execute(
                    "DELETE FROM midterm_market_race_verifications WHERE race_key=?",
                    (race_key,),
                )
                return cur.rowcount > 0

    def get_race_verification(self, race_key: str) -> dict | None:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM midterm_market_race_verifications WHERE race_key=?",
                (race_key,),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def get_all_verifications(self) -> dict[str, dict]:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM midterm_market_race_verifications"
            ).fetchall()
        return {r["race_key"]: _row_to_dict(r) for r in rows}

    # === Divergence history =================================================

    def get_divergence_history(
        self, race_key: str = None, days: int = 30
    ) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        if race_key:
            sql = """SELECT * FROM midterm_divergence_snapshots
                     WHERE snapshot_time >= ? AND race_key = ?
                     ORDER BY snapshot_time"""
            params = (cutoff, race_key)
        else:
            sql = """SELECT * FROM midterm_divergence_snapshots
                     WHERE snapshot_time >= ?
                     ORDER BY max_divergence DESC"""
            params = (cutoff,)

        with _get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()

        results = []
        for row in rows:
            d = _row_to_dict(row)
            dd = d.get("divergence_details")
            if isinstance(dd, str):
                try:
                    d["divergence_details"] = json.loads(dd)
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        return results

    # === Polling Data =======================================================

    def store_polls_batch(self, polls: list[dict]):
        rows = []
        for p in polls:
            rows.append((
                p.get("poll_type") or "",
                p.get("state") or "",
                p.get("candidate") or "",
                p.get("party"),
                p.get("percentage"),
                p.get("pollster") or "",
                p.get("sample_size"),
                p.get("population"),
                p.get("start_date"),
                p.get("end_date") or "",
                p.get("race_id"),
                p.get("source", "538"),
            ))
        if rows:
            with _lock:
                with _get_conn() as conn:
                    conn.executemany(
                        """INSERT OR IGNORE INTO midterm_polling_data
                            (poll_type, state, candidate, party, percentage,
                             pollster, sample_size, population, start_date,
                             end_date, race_id, source)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        rows,
                    )

    def get_polls(self, state: str = None, poll_type: str = None) -> list[dict]:
        clauses = []
        params = []
        if state:
            clauses.append("state = ?")
            params.append(state)
        if poll_type:
            clauses.append("poll_type = ?")
            params.append(poll_type)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM midterm_polling_data{where} ORDER BY end_date DESC LIMIT 500"

        with _get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_recent_polls(self, limit: int = 50) -> list[dict]:
        cap = min(limit, 200)
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM midterm_polling_data ORDER BY end_date DESC, id DESC LIMIT ?",
                (cap,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # === User Watchlists ====================================================

    def get_watchlist(self, user_id: str) -> list[dict]:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT race_key, created_at FROM midterm_user_watchlists WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def add_to_watchlist(self, user_id: str, race_key: str):
        with _lock:
            with _get_conn() as conn:
                conn.execute(
                    """INSERT INTO midterm_user_watchlists (user_id, race_key)
                       VALUES (?,?)
                       ON CONFLICT(user_id, race_key) DO NOTHING""",
                    (user_id, race_key),
                )

    def remove_from_watchlist(self, user_id: str, race_key: str):
        with _lock:
            with _get_conn() as conn:
                conn.execute(
                    "DELETE FROM midterm_user_watchlists WHERE user_id = ? AND race_key = ?",
                    (user_id, race_key),
                )

    # === Alert Settings =====================================================

    def get_alerts(self, user_id: str) -> list[dict]:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM midterm_alert_settings WHERE user_id = ? AND enabled = 1",
                (user_id,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def upsert_alert(self, user_id: str, race_key: str, threshold: float = 5.0, alert_type: str = "divergence"):
        with _lock:
            with _get_conn() as conn:
                conn.execute(
                    """INSERT INTO midterm_alert_settings
                        (user_id, race_key, alert_type, threshold, enabled)
                       VALUES (?,?,?,?,1)
                       ON CONFLICT(user_id, race_key, alert_type) DO UPDATE SET
                         threshold=excluded.threshold,
                         enabled=excluded.enabled""",
                    (user_id, race_key, alert_type, threshold),
                )

    # === Audit Log ==========================================================

    def log_action(
        self,
        user_id: Optional[str] = None,
        action: str = "",
        details: str = None,
        ip: str = None,
    ):
        with _lock:
            with _get_conn() as conn:
                conn.execute(
                    """INSERT INTO midterm_audit_log (user_id, action, details, ip_address)
                       VALUES (?,?,?,?)""",
                    (user_id, action, details, ip),
                )

    def get_audit_log(self, user_id: str = None, limit: int = 100) -> list[dict]:
        if user_id:
            sql = "SELECT * FROM midterm_audit_log WHERE user_id = ? ORDER BY created_at DESC LIMIT ?"
            params = (user_id, limit)
        else:
            sql = "SELECT * FROM midterm_audit_log ORDER BY created_at DESC LIMIT ?"
            params = (limit,)

        with _get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    # === Admin Analytics ====================================================

    def get_admin_stats(self) -> dict:
        stats = {}
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM midterm_markets WHERE active = 1"
            ).fetchone()
            stats["active_markets"] = row["cnt"] if row else 0

            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM midterm_price_history"
            ).fetchone()
            stats["price_snapshots"] = row["cnt"] if row else 0

            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM midterm_divergence_snapshots"
            ).fetchone()
            stats["divergence_snapshots"] = row["cnt"] if row else 0

        return stats

    def get_all_users(self, limit: int = 100, offset: int = 0) -> list[dict]:
        """Fetch user profiles from the shared profiles table."""
        with _get_conn() as conn:
            rows = conn.execute(
                """SELECT id, email, display_name, tier, created_at, last_login
                   FROM profiles
                   ORDER BY created_at DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # === District Profiles ==================================================

    def upsert_district_profile(self, state: str, name: str, profile_data: dict, auto_generated: bool = False):
        """Insert or update a district/state profile."""
        data_json = json.dumps(profile_data) if isinstance(profile_data, (dict, list)) else profile_data
        now = datetime.now(timezone.utc).isoformat()
        with _lock:
            with _get_conn() as conn:
                conn.execute(
                    """INSERT INTO midterm_district_profiles
                        (state, name, profile_data, auto_generated, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(state) DO UPDATE SET
                         name=excluded.name,
                         profile_data=excluded.profile_data,
                         auto_generated=excluded.auto_generated,
                         updated_at=excluded.updated_at""",
                    (state.upper(), name, data_json, 1 if auto_generated else 0, now),
                )

    def get_district_profile(self, state: str) -> dict | None:
        """Get a district profile by state abbreviation."""
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM midterm_district_profiles WHERE state = ?",
                (state.upper(),),
            ).fetchone()
        if not row:
            return None
        d = _row_to_dict(row)
        if isinstance(d.get("profile_data"), str):
            try:
                d["profile_data"] = json.loads(d["profile_data"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def get_all_district_profiles(self) -> list[dict]:
        """Get all district profiles."""
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM midterm_district_profiles ORDER BY state"
            ).fetchall()
        results = []
        for row in rows:
            d = _row_to_dict(row)
            if isinstance(d.get("profile_data"), str):
                try:
                    d["profile_data"] = json.loads(d["profile_data"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        return results

    def get_profiled_states(self) -> set[str]:
        """Return set of state abbreviations that already have profiles."""
        with _get_conn() as conn:
            rows = conn.execute("SELECT state FROM midterm_district_profiles").fetchall()
        return {r["state"] for r in rows}

    # === Jurisdiction Profiles (states / districts / countries) =============

    def upsert_jurisdiction_profile(
        self,
        jurisdiction_type: str,
        jurisdiction_code: str,
        name: str,
        profile_data: dict,
        candidates_data: list | None = None,
        auto_generated: bool = False,
    ):
        """Upsert a jurisdiction profile (US state, US district, or country)."""
        prof_json = json.dumps(profile_data) if isinstance(profile_data, (dict, list)) else profile_data
        cand_json = json.dumps(candidates_data) if candidates_data is not None else None
        now = datetime.now(timezone.utc).isoformat()
        with _lock:
            with _get_conn() as conn:
                conn.execute(
                    """INSERT INTO midterm_jurisdiction_profiles
                        (jurisdiction_type, jurisdiction_code, name, profile_data,
                         candidates_data, auto_generated, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(jurisdiction_type, jurisdiction_code) DO UPDATE SET
                         name=excluded.name,
                         profile_data=excluded.profile_data,
                         candidates_data=COALESCE(excluded.candidates_data, midterm_jurisdiction_profiles.candidates_data),
                         auto_generated=excluded.auto_generated,
                         updated_at=excluded.updated_at""",
                    (
                        jurisdiction_type,
                        jurisdiction_code.upper(),
                        name,
                        prof_json,
                        cand_json,
                        1 if auto_generated else 0,
                        now,
                    ),
                )

    def get_jurisdiction_profile(
        self, jurisdiction_type: str, jurisdiction_code: str
    ) -> dict | None:
        """Fetch a single jurisdiction profile."""
        with _get_conn() as conn:
            row = conn.execute(
                """SELECT * FROM midterm_jurisdiction_profiles
                   WHERE jurisdiction_type = ? AND jurisdiction_code = ?""",
                (jurisdiction_type, jurisdiction_code.upper()),
            ).fetchone()
        if not row:
            return None
        d = _row_to_dict(row)
        for k in ("profile_data", "candidates_data"):
            if isinstance(d.get(k), str):
                try:
                    d[k] = json.loads(d[k])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    def get_all_jurisdiction_profiles(
        self, jurisdiction_type: str | None = None
    ) -> list[dict]:
        """List jurisdiction profiles, optionally filtered by type."""
        with _get_conn() as conn:
            if jurisdiction_type:
                rows = conn.execute(
                    """SELECT jurisdiction_type, jurisdiction_code, name, auto_generated, updated_at
                       FROM midterm_jurisdiction_profiles
                       WHERE jurisdiction_type = ?
                       ORDER BY jurisdiction_code""",
                    (jurisdiction_type,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT jurisdiction_type, jurisdiction_code, name, auto_generated, updated_at
                       FROM midterm_jurisdiction_profiles
                       ORDER BY jurisdiction_type, jurisdiction_code"""
                ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_profiled_jurisdictions(
        self, jurisdiction_type: str | None = None
    ) -> set[str]:
        """Return set of (jurisdiction_type, jurisdiction_code) tuples already profiled,
        or just codes if filtered by type."""
        with _get_conn() as conn:
            if jurisdiction_type:
                rows = conn.execute(
                    "SELECT jurisdiction_code FROM midterm_jurisdiction_profiles WHERE jurisdiction_type = ?",
                    (jurisdiction_type,),
                ).fetchall()
                return {r["jurisdiction_code"] for r in rows}
            rows = conn.execute(
                "SELECT jurisdiction_type, jurisdiction_code FROM midterm_jurisdiction_profiles"
            ).fetchall()
        return {f"{r['jurisdiction_type']}:{r['jurisdiction_code']}" for r in rows}

#!/usr/bin/env python3
"""
Sports Betting Dashboard — Polymarket vs Bookmaker Odds Comparison

Serves a live HTML dashboard that compares bookmaker odds (via The Odds API)
with Polymarket market prices to help spot mispriced markets.
Signals only — no trading logic.
"""

import asyncio
import copy
import hashlib
import hmac
import json
import logging
import math
import os
import re
import tempfile
import statistics
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from rapidfuzz import fuzz

# ── Simple encryption for sensitive fields (Telegram tokens) ──
try:
    from cryptography.fernet import Fernet as _Fernet, InvalidToken as _InvalidToken

    def _get_sports_fernet():
        key_file = Path(__file__).parent / ".secret_key"
        if key_file.exists():
            raw = key_file.read_bytes().strip()
        else:
            raw = _Fernet.generate_key()
            key_file.write_bytes(raw)
            key_file.chmod(0o600)
        import base64
        if len(raw) == 44 and raw.endswith(b"="):
            return _Fernet(raw)
        dk = hashlib.pbkdf2_hmac("sha256", raw, b"sports-dash-v1", 100000)
        return _Fernet(base64.urlsafe_b64encode(dk))

    _sports_fernet = _get_sports_fernet()

    def _encrypt_field(plaintext: str) -> str:
        if not plaintext:
            return ""
        return "enc:" + _sports_fernet.encrypt(plaintext.encode()).decode()

    def _decrypt_field(ciphertext: str) -> str:
        if not ciphertext:
            return ""
        if ciphertext.startswith("enc:"):
            return _sports_fernet.decrypt(ciphertext[4:].encode()).decode()
        return ciphertext  # legacy plaintext — will be encrypted on next save

except ImportError:
    logging.warning("cryptography not installed — Telegram tokens stored in plaintext")

    def _encrypt_field(plaintext: str) -> str:
        return plaintext

    def _decrypt_field(ciphertext: str) -> str:
        return ciphertext
import sqlite3
import threading
from contextlib import contextmanager, asynccontextmanager
from fastapi import FastAPI, HTTPException, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# Prometheus metrics (optional — degrade to no-op if not installed).
try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter as _PromCounter,
        Gauge as _PromGauge,
        Histogram as _PromHistogram,
        generate_latest as _prom_generate_latest,
    )
    _PROM_ENABLED = True
except ImportError:
    _PROM_ENABLED = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4"

    class _NoOpMetric:
        def labels(self, *a, **kw): return self
        def inc(self, *a, **kw): pass
        def observe(self, *a, **kw): pass
        def set(self, *a, **kw): pass

    def _PromCounter(*a, **kw): return _NoOpMetric()
    def _PromGauge(*a, **kw): return _NoOpMetric()
    def _PromHistogram(*a, **kw): return _NoOpMetric()
    def _prom_generate_latest(): return b""


# ── Metrics ─────────────────────────────────────────────────────────────────
M_POLL_DURATION = _PromHistogram(
    "sports_dashboard_poll_loop_seconds",
    "Time spent in one poll-loop iteration",
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120),
)
M_COMPARISONS = _PromCounter(
    "sports_dashboard_comparisons_total",
    "Comparisons computed per loop, by sport",
    labelnames=("sport",),
)
M_SIGNALS = _PromCounter(
    "sports_dashboard_signals_total",
    "Signals (above divergence threshold) emitted, by sport",
    labelnames=("sport",),
)
M_POLL_ERRORS = _PromCounter(
    "sports_dashboard_poll_errors_total",
    "Errors raised inside the poll loop, by stage",
    labelnames=("stage",),
)
M_ALERT_SEND = _PromCounter(
    "sports_dashboard_alert_send_total",
    "Alert delivery attempts, by channel and result",
    labelnames=("channel", "result"),
)
M_ODDS_REMAINING = _PromGauge(
    "sports_dashboard_odds_api_remaining",
    "Latest x-requests-remaining from The Odds API",
)
M_ODDS_USED = _PromGauge(
    "sports_dashboard_odds_api_used",
    "Latest x-requests-used from The Odds API",
)
M_ODDS_EXHAUSTED = _PromCounter(
    "sports_dashboard_odds_api_exhausted_total",
    "Number of 429 / quota-exhausted responses observed",
)
M_MATCH_REJECTS = _PromCounter(
    "sports_dashboard_match_rejects_total",
    "Near-reject matches by reason",
    labelnames=("reason",),
)
M_POLL_INTERVAL = _PromGauge(
    "sports_dashboard_poll_interval_seconds",
    "Computed sleep interval for the next poll-loop iteration",
)
M_WS_LIVE_PRICES = _PromGauge(
    "sports_dashboard_ws_live_prices",
    "Number of Polymarket assets currently receiving live WS price updates",
)
M_WS_PRICE_EVENTS = _PromCounter(
    "sports_dashboard_ws_price_events_total",
    "Polymarket WS price events processed",
)
M_WS_RECONNECTS = _PromCounter(
    "sports_dashboard_ws_reconnects_total",
    "Polymarket WS reconnect attempts",
)
M_EXPLAIN_REQUESTS = _PromCounter(
    "sports_dashboard_explain_requests_total",
    "AI-explanation requests by outcome",
    labelnames=("result",),  # cache_hit | api_call | error | disabled
)
M_PM_FILLS_CAPTURED = _PromCounter(
    "sports_dashboard_pm_fills_total",
    "Large Polymarket fills captured from the WS feed",
    labelnames=("side",),  # BUY | SELL
)
M_PM_WS_FAILURES = _PromGauge(
    "sports_dashboard_pm_ws_consecutive_failures",
    "Polymarket WS consecutive connect failures; circuit opens at threshold",
)


# ---------------------------------------------------------------------------
# Layered env loading
# ---------------------------------------------------------------------------
# Search order (later files override earlier values that are still empty):
#   1. ~/.gateway_env                                  — platform-shared secrets
#   2. ~/Polymarket/gateway/.env.production            — gateway / shared API keys
#   3. <this dashboard>/.env.production                — per-dashboard overrides
#   4. <this dashboard>/.env                           — local dev fallback
#
# Each file is optional; missing files are silently skipped. Using
# `override=False` means the **first** file that defines a key wins, so
# narrower files can sit on top of platform defaults without being clobbered
# (we walk from broadest → narrowest below). Loud startup logging lets us
# stop debugging silent "ODDS_API_KEY not set" failures by reading the log.
_DASHBOARD_DIR = Path(__file__).resolve().parent
_ENV_SEARCH = [
    Path.home() / ".gateway_env",
    _DASHBOARD_DIR.parent / "gateway" / ".env.production",
    _DASHBOARD_DIR / ".env.production",
    _DASHBOARD_DIR / ".env",
]
_loaded_from = []
for _f in _ENV_SEARCH:
    if _f.is_file():
        load_dotenv(_f, override=False)
        _loaded_from.append(str(_f))

# ── Template loader ─────────────────────────────────────────────────────────
_TEMPLATES_DIR = Path(__file__).parent / "templates"

def _load_template(name: str) -> str:
    """Load an HTML template from templates/<name>.html.

    Templates are read once at module import. They contain no Jinja syntax;
    runtime substitution still happens via .replace() at the call sites that
    used the inline strings before.
    """
    return (_TEMPLATES_DIR / f"{name}.html").read_text()



# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
POLYMARKET_HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
GAMMA_API_HOST = "https://gamma-api.polymarket.com"
POLY_LB_API_HOST = "https://lb-api.polymarket.com"
POLY_DATA_API_HOST = "https://data-api.polymarket.com"
KALSHI_API_HOST = "https://api.elections.kalshi.com/trade-api/v2"
DIVERGENCE_THRESHOLD = float(os.getenv("DIVERGENCE_THRESHOLD", "5"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))
TOP_TRADERS_LIMIT = int(os.getenv("TOP_TRADERS_LIMIT", "10"))
TOP_TRADER_TRADES_LIMIT = int(os.getenv("TOP_TRADER_TRADES_LIMIT", "500"))
TOP_TRADERS_REFRESH_SECS = int(os.getenv("TOP_TRADERS_REFRESH_SECS", "600"))

# Map our sport keys to Kalshi series tickers (list per sport)
KALSHI_SERIES: dict[str, list[str]] = {
    # --- US Major Leagues ---
    "basketball_nba": ["KXNBAGAME", "KXNBAMVP", "KXNBAROY", "KXNBAPTS", "KXNBA3PT", "KXNBAAST", "KXNBAREB"],
    "americanfootball_nfl": ["KXNFLGAME", "KXNFLMVP", "KXNFLPLAYOFF"],
    "icehockey_nhl": ["KXNHLGAME", "KXNHLVEZINA", "KXNHLCALDER", "KXNHLNORRIS"],
    "baseball_mlb": ["KXMLBGAME"],
    # --- Soccer ---
    "soccer_epl": ["KXEPLGAME"],
    "soccer_spain_la_liga": ["KXLALIGA", "KXLALIGAGAME"],
    "soccer_germany_bundesliga": ["KXBUNDESLIGA", "KXBUNDESLIGAGAME"],
    "soccer_italy_serie_a": ["KXSERIEA"],
    "soccer_france_ligue_one": ["KXLIGUE1", "KXLIGUE1GAME"],
    "soccer_uefa_champs_league": ["KXUCLGAME", "KXUCL"],
    "soccer_uefa_europa_league": ["KXUELGAME", "KXUEL"],
    "soccer_usa_mls": ["KXMLSGAME"],
    # --- Other Sports ---
    "boxing_boxing": ["KXBOXING"],
    "tennis_atp": ["KXATPMATCH"],
    "tennis_wta": ["KXWTAMATCH"],
    "motorsport_f1": ["KXF1RACE"],
    "americanfootball_ncaaf": ["KXNCAAF", "KXHEISMAN"],
}
SIGNALS_FILE = Path(__file__).parent / "sports_signals.json"

# Sport keys — the key doubles as The Odds API sport key where applicable
SPORTS = {
    "basketball_nba": "NBA",
    "americanfootball_nfl": "NFL",
    "icehockey_nhl": "NHL",
    "baseball_mlb": "MLB",
    "soccer_epl": "EPL",
    "soccer_spain_la_liga": "La Liga",
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_italy_serie_a": "Serie A",
    "soccer_france_ligue_one": "Ligue 1",
    "soccer_uefa_champs_league": "Champions League",
    "soccer_uefa_europa_league": "Europa League",
    "soccer_usa_mls": "MLS",
    "mma_mixed_martial_arts": "MMA",
    "boxing_boxing": "Boxing",
    "tennis_atp": "Tennis ATP",
    "tennis_wta": "Tennis WTA",
    "motorsport_f1": "Formula 1",
    "americanfootball_ncaaf": "NCAAF",
    # Esports
    "esports_lol_lck": "LoL: LCK",
    "esports_lol_lpl": "LoL: LPL",
    "esports_lol_lec": "LoL: LEC",
    "esports_lol_worlds": "LoL: Worlds",
    "esports_cs2": "CS2",
    "esports_valorant": "Valorant",
    "esports_dota2": "Dota 2",
}

# Sports that are Kalshi-only (no Odds API equivalent key)
KALSHI_ONLY_SPORTS = {"motorsport_f1", "tennis_atp", "tennis_wta"}

# Category groupings for the frontend two-tier nav
SPORT_CATEGORIES = {
    "Sports": [
        "basketball_nba", "americanfootball_nfl", "icehockey_nhl", "baseball_mlb",
        "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
        "soccer_italy_serie_a", "soccer_france_ligue_one",
        "soccer_uefa_champs_league", "soccer_uefa_europa_league",
        "soccer_usa_mls",
        "mma_mixed_martial_arts", "boxing_boxing",
        "tennis_atp", "tennis_wta",
        "motorsport_f1", "americanfootball_ncaaf",
    ],
    "Esports": [
        "esports_lol_lck", "esports_lol_lpl", "esports_lol_lec", "esports_lol_worlds",
        "esports_cs2", "esports_valorant", "esports_dota2",
    ],
}

# Which sport keys are esports (Polymarket-only, no bookmaker odds)
ESPORTS_KEYS = {k for k in SPORTS if k.startswith("esports_")}

# ---------------------------------------------------------------------------
# SQLite Database
# ---------------------------------------------------------------------------
log = logging.getLogger("sports_dashboard")

_DB_PATH = Path(__file__).parent / "data.db"
_db_lock = threading.Lock()


@contextmanager
def _get_db():
    """Yield a sqlite3 connection with WAL mode and row-factory.

    WAL mode allows concurrent readers, so we only hold the lock during
    writes (commit/rollback), not for the entire connection lifetime.
    """
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        with _db_lock:
            conn.commit()
    except Exception:
        with _db_lock:
            conn.rollback()
        raise
    finally:
        conn.close()


def _init_db():
    """Create all tables if they don't exist."""
    with _get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS profiles (
                id TEXT PRIMARY KEY,
                email TEXT,
                username TEXT,
                is_admin INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS sports_user_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS sports_user_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT UNIQUE NOT NULL,
                default_sport TEXT DEFAULT 'basketball_nba',
                divergence_threshold REAL DEFAULT 5.0,
                notifications_enabled INTEGER DEFAULT 1,
                theme TEXT DEFAULT 'dark'
            );
            CREATE TABLE IF NOT EXISTS sports_edge_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport TEXT,
                home_team TEXT,
                away_team TEXT,
                outcome TEXT,
                sharp_prob REAL,
                poly_prob REAL,
                divergence REAL,
                kelly_pct REAL,
                confidence_score REAL,
                resolved INTEGER DEFAULT 0,
                resolution TEXT DEFAULT '',
                detected_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS sports_market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport TEXT,
                event_name TEXT,
                outcome TEXT,
                book_prob REAL,
                poly_prob REAL,
                kalshi_prob REAL,
                divergence REAL,
                poly_volume REAL,
                kalshi_volume REAL,
                snapshot_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS sports_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                market_name TEXT NOT NULL,
                outcome TEXT DEFAULT '',
                entry_price REAL NOT NULL,
                amount REAL NOT NULL,
                exit_price REAL,
                pnl REAL,
                status TEXT DEFAULT 'open',
                resolved_at TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS sports_watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                market_key TEXT NOT NULL,
                home_team TEXT DEFAULT '',
                away_team TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(user_id, market_key)
            );
            CREATE TABLE IF NOT EXISTS sports_user_layout (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT UNIQUE NOT NULL,
                visible_widgets TEXT DEFAULT '["stats","top_opps","hero","events"]',
                visible_data_points TEXT DEFAULT '[]',
                card_expanded_default INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sports_historical_markets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport TEXT,
                event_title TEXT,
                market_question TEXT,
                outcome TEXT,
                final_price REAL,
                volume REAL,
                start_date TEXT,
                end_date TEXT,
                resolution TEXT,
                source TEXT,
                slug TEXT,
                UNIQUE(source, slug, outcome)
            );
            CREATE TABLE IF NOT EXISTS sports_match_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                home_team TEXT,
                away_team TEXT,
                poly_question TEXT,
                reason TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS sports_alert_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT UNIQUE NOT NULL,
                enabled INTEGER DEFAULT 0,
                telegram_chat_id TEXT DEFAULT '',
                telegram_bot_token TEXT DEFAULT '',
                webhook_url TEXT DEFAULT '',
                min_edge REAL DEFAULT 5.0,
                sports TEXT DEFAULT '[]',
                last_alert_at TEXT DEFAULT ''
            );
            -- Rich, per-rule alert configuration. The simple sports_alert_config
            -- above stays as a fallback / quick-start config; users can add
            -- multiple rules with structured filters on top of it.
            CREATE TABLE IF NOT EXISTS sports_alert_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                name TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
                sports TEXT DEFAULT '[]',          -- JSON list of sport keys
                market_types TEXT DEFAULT '[]',    -- JSON list: h2h/spreads/totals/futures/props
                min_divergence_pp REAL DEFAULT 5.0,
                min_volume REAL,                   -- Polymarket USD volume floor
                max_time_to_event_hours REAL,      -- only fire when game is this close
                require_sharp_consensus INTEGER DEFAULT 1,
                require_not_stale INTEGER DEFAULT 1,
                require_liquidity_ok INTEGER DEFAULT 1,
                channel TEXT DEFAULT 'telegram',   -- telegram|webhook|both
                quiet_hours_start INTEGER,         -- 0-23 UTC; null = no quiet hours
                quiet_hours_end INTEGER,
                cooldown_secs INTEGER DEFAULT 300,
                last_fired_at TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_alert_rules_user ON sports_alert_rules(user_id, enabled);
            -- Web Push subscriptions (one per device per user)
            CREATE TABLE IF NOT EXISTS sports_push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                user_agent TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                last_pushed_at TEXT,
                UNIQUE(user_id, endpoint)
            );
            CREATE INDEX IF NOT EXISTS idx_push_user ON sports_push_subscriptions(user_id);
            -- LLM-generated explanations for signals. Keyed by a stable hash
            -- of (event, outcome, divergence-rounded, sport) so repeated
            -- views of the same signal hit the cache instead of the API.
            CREATE TABLE IF NOT EXISTS sports_signal_explanations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cache_key TEXT NOT NULL UNIQUE,
                signal_summary TEXT,
                explanation TEXT NOT NULL,
                model TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_signal_expl_key ON sports_signal_explanations(cache_key, created_at);
            -- Per-user API tokens for Bearer auth. token_hash is the
            -- SHA-256 of the token; we never store the plaintext after
            -- the create response.
            CREATE TABLE IF NOT EXISTS sports_api_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                name TEXT DEFAULT '',
                token_hash TEXT NOT NULL UNIQUE,
                token_prefix TEXT DEFAULT '',
                scopes TEXT DEFAULT '[]',
                created_at TEXT DEFAULT (datetime('now')),
                last_used_at TEXT,
                revoked_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON sports_api_tokens(user_id, revoked_at);
            CREATE INDEX IF NOT EXISTS idx_api_tokens_hash ON sports_api_tokens(token_hash) WHERE revoked_at IS NULL;
            -- Opt-in roster for the public CLV leaderboard. Users
            -- choose a display name (often distinct from email); only
            -- rows in this table appear on the public /leaderboard.
            CREATE TABLE IF NOT EXISTS sports_clv_leaderboard_optin (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                joined_at TEXT DEFAULT (datetime('now'))
            );
            -- Per-user bankroll for Kelly-aware stake suggestions and
            -- drawdown alerts. Defaults are conservative half-Kelly with
            -- a 5% per-bet ceiling.
            CREATE TABLE IF NOT EXISTS sports_bankroll (
                user_id TEXT PRIMARY KEY,
                starting_bankroll REAL NOT NULL,
                current_bankroll REAL NOT NULL,
                kelly_fraction REAL DEFAULT 0.5,
                max_per_bet_pct REAL DEFAULT 5.0,
                drawdown_alert_pct REAL DEFAULT 10.0,
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS sports_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport TEXT,
                event_id TEXT,
                home_team TEXT,
                away_team TEXT,
                home_score INTEGER,
                away_score INTEGER,
                completed INTEGER DEFAULT 0,
                winner TEXT DEFAULT '',
                commence_time TEXT,
                fetched_at TEXT DEFAULT (datetime('now')),
                UNIQUE(sport, event_id)
            );
        """)
        # Historical head-to-head record across all sports (sourced from ESPN)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sports_team_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport TEXT NOT NULL,
                event_date TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                home_team_norm TEXT NOT NULL,
                away_team_norm TEXT NOT NULL,
                home_score INTEGER,
                away_score INTEGER,
                winner TEXT NOT NULL DEFAULT '',
                season TEXT DEFAULT '',
                source TEXT DEFAULT 'espn',
                fetched_at TEXT DEFAULT (datetime('now')),
                UNIQUE(sport, event_date, home_team_norm, away_team_norm)
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_team_history_pair ON sports_team_history(sport, home_team_norm, away_team_norm);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_team_history_home ON sports_team_history(sport, home_team_norm);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_team_history_away ON sports_team_history(sport, away_team_norm);")
        # Tracks when we last hit ESPN for each sport so we don't refetch needlessly
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sports_history_meta (
                sport TEXT PRIMARY KEY,
                last_fetch_at TEXT,
                last_date_covered TEXT,
                rows_total INTEGER DEFAULT 0
            );
        """)
        # Per-team season stats (record, ranking, points-for/against)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sports_team_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport TEXT NOT NULL,
                team_name TEXT NOT NULL,
                team_norm TEXT NOT NULL,
                espn_id TEXT,
                abbreviation TEXT,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                draws INTEGER DEFAULT 0,
                win_pct REAL DEFAULT 0,
                points_for REAL DEFAULT 0,
                points_against REAL DEFAULT 0,
                rank INTEGER DEFAULT 0,
                conference_rank INTEGER DEFAULT 0,
                streak TEXT DEFAULT '',
                close_game_wins INTEGER DEFAULT 0,
                close_game_losses INTEGER DEFAULT 0,
                last_10 TEXT DEFAULT '',
                logo_url TEXT DEFAULT '',
                fetched_at TEXT DEFAULT (datetime('now')),
                UNIQUE(sport, team_norm)
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_team_info_norm ON sports_team_info(sport, team_norm);")
        # Player roster + stats
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sports_player_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport TEXT NOT NULL,
                team_norm TEXT NOT NULL,
                player_name TEXT NOT NULL,
                espn_id TEXT,
                position TEXT DEFAULT '',
                jersey TEXT DEFAULT '',
                stats_json TEXT DEFAULT '{}',
                strengths TEXT DEFAULT '',
                weaknesses TEXT DEFAULT '',
                impact_score REAL DEFAULT 0,
                fetched_at TEXT DEFAULT (datetime('now')),
                UNIQUE(sport, team_norm, player_name)
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_player_team ON sports_player_info(sport, team_norm);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_player_impact ON sports_player_info(sport, team_norm, impact_score DESC);")
        # Top Polymarket traders' open positions, indexed by condition_id so we
        # can join against sports comparisons in O(1).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS top_trader_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                pseudonym TEXT DEFAULT '',
                name TEXT DEFAULT '',
                profile_image TEXT DEFAULT '',
                rank INTEGER DEFAULT 0,
                lifetime_volume REAL DEFAULT 0,
                condition_id TEXT NOT NULL,
                slug TEXT DEFAULT '',
                title TEXT DEFAULT '',
                outcome TEXT DEFAULT '',
                outcome_index INTEGER DEFAULT 0,
                net_size REAL DEFAULT 0,
                net_usd REAL DEFAULT 0,
                avg_price REAL DEFAULT 0,
                last_side TEXT DEFAULT '',
                last_traded_ts INTEGER DEFAULT 0,
                trade_count INTEGER DEFAULT 0,
                fetched_at TEXT DEFAULT (datetime('now')),
                UNIQUE(wallet, condition_id, outcome)
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ttp_condition ON top_trader_positions(condition_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ttp_wallet ON top_trader_positions(wallet);")


_init_db()

# Migrations: add columns that may not exist in older databases
def _migrate_db():
    with _get_db() as conn:
        # Add columns to sports_edge_history for auto-resolution
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sports_edge_history)").fetchall()}
        if "commence_time" not in cols:
            conn.execute("ALTER TABLE sports_edge_history ADD COLUMN commence_time TEXT DEFAULT ''")
        if "event_id" not in cols:
            conn.execute("ALTER TABLE sports_edge_history ADD COLUMN event_id TEXT DEFAULT ''")
        if "market_type" not in cols:
            conn.execute("ALTER TABLE sports_edge_history ADD COLUMN market_type TEXT DEFAULT 'h2h'")
        # Add market_type to snapshots
        snap_cols = {r[1] for r in conn.execute("PRAGMA table_info(sports_market_snapshots)").fetchall()}
        if "market_type" not in snap_cols:
            conn.execute("ALTER TABLE sports_market_snapshots ADD COLUMN market_type TEXT DEFAULT 'h2h'")
        # Watchlist alerts: per-item divergence threshold + cooldown timestamp.
        wl_cols = {r[1] for r in conn.execute("PRAGMA table_info(sports_watchlist)").fetchall()}
        if "alert_threshold_pp" not in wl_cols:
            conn.execute("ALTER TABLE sports_watchlist ADD COLUMN alert_threshold_pp REAL")
        if "last_alerted_at" not in wl_cols:
            conn.execute("ALTER TABLE sports_watchlist ADD COLUMN last_alerted_at TEXT DEFAULT ''")
        # Bet tracker enrichment: sport, book, market_type, line,
        # commence_time, source, closing_book_prob, clv_pp.
        tr_cols = {r[1] for r in conn.execute("PRAGMA table_info(sports_trades)").fetchall()}
        # Per-user webhook HMAC signing key (encrypted via Fernet).
        ac_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(sports_alert_config)").fetchall()}
        if "webhook_signing_key" not in ac_cols:
            conn.execute(
                "ALTER TABLE sports_alert_config "
                "ADD COLUMN webhook_signing_key TEXT DEFAULT ''"
            )
        for col, decl in [
            ("sport", "TEXT DEFAULT ''"),
            ("book", "TEXT DEFAULT ''"),           # which venue: polymarket, kalshi, draftkings, ...
            ("market_type", "TEXT DEFAULT 'h2h'"),
            ("line", "REAL"),                       # for spreads/totals/props
            ("commence_time", "TEXT DEFAULT ''"),
            ("source", "TEXT DEFAULT 'manual'"),    # manual | csv | webhook
            ("closing_book_prob", "REAL"),          # filled at resolution time
            ("clv_pp", "REAL"),                     # filled at resolution time
            ("notes", "TEXT DEFAULT ''"),
            ("home_team", "TEXT DEFAULT ''"),
            ("away_team", "TEXT DEFAULT ''"),
        ]:
            if col not in tr_cols:
                conn.execute(f"ALTER TABLE sports_trades ADD COLUMN {col} {decl}")

_migrate_db()


def _event_name(home: str | None, away: str | None) -> str:
    """Build a canonical 'Home vs Away' event name.

    Edge cases: when one side is missing, drop the ' vs ' join entirely.
    Do NOT use `.strip(' vs')` on the joined string — `.strip()` strips
    a character SET, not a substring, so 'Lakers vs Warriors'.strip(' vs')
    becomes 'Lakers vs Warrior' (the trailing 's' is in the set).
    """
    h = (home or "").strip()
    a = (away or "").strip()
    if h and a:
        return f"{h} vs {a}"
    return h or a


def _is_safe_webhook_url(url: str) -> bool:
    """Validate that a webhook URL is HTTPS and does not target private/reserved networks."""
    import socket
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return False
        hostname = parsed.hostname or ""
        if not hostname:
            return False
        # Resolve and check for private IPs
        for info in socket.getaddrinfo(hostname, None):
            addr = info[4][0]
            import ipaddress
            ip = ipaddress.ip_address(addr)
            if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
                return False
    except Exception:
        return False
    return True


def _get_webhook_signing_key(user_id: str) -> str | None:
    """Look up the user's webhook signing secret (plaintext, encrypted at rest).

    Returns None if no key is set (delivery still proceeds — signing is
    optional, recipients can ignore the header). Keys are stored on
    sports_alert_config to keep webhook config in one place.
    """
    with _get_db() as conn:
        row = conn.execute(
            "SELECT webhook_signing_key FROM sports_alert_config WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row or not row["webhook_signing_key"]:
        return None
    try:
        return _decrypt_field(row["webhook_signing_key"])
    except Exception:
        return None


def _signed_webhook_post(url: str, payload: dict, signing_key: str | None = None,
                          timeout: int = 10) -> bool:
    """POST a webhook with optional HMAC-SHA256 signature on the raw body.

    Headers:
      Content-Type: application/json
      X-Sharpe-Timestamp: <unix seconds at send time>
      X-Sharpe-Signature: sha256=<hex hmac of (timestamp + '.' + body)>

    Recipients verify by recomputing HMAC and constant-time comparing.
    Including the timestamp in the signature prevents replay attacks.
    """
    if not _is_safe_webhook_url(url):
        return False
    body_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    headers = {"Content-Type": "application/json"}
    if signing_key:
        ts = str(int(time.time()))
        message = ts.encode() + b"." + body_bytes
        sig = hmac.new(signing_key.encode(), message, hashlib.sha256).hexdigest()
        headers["X-Sharpe-Timestamp"] = ts
        headers["X-Sharpe-Signature"] = f"sha256={sig}"
    try:
        requests.post(url, data=body_bytes, headers=headers, timeout=timeout)
        return True
    except Exception as e:
        log.warning("Webhook POST failed (%s): %s", url, e)
        return False


def log_activity(user_id: str, action: str, detail: str = ""):
    """Log a user activity event to sports_user_activity."""
    try:
        with _get_db() as conn:
            conn.execute(
                "INSERT INTO sports_user_activity (user_id, action, detail) VALUES (?, ?, ?)",
                (user_id, action, detail),
            )
    except Exception:
        pass


def get_user_settings(user_id: str) -> dict:
    """Fetch sports-specific settings for a user from SQLite."""
    try:
        with _get_db() as conn:
            row = conn.execute(
                "SELECT * FROM sports_user_settings WHERE user_id = ? LIMIT 1", (user_id,)
            ).fetchone()
            if row:
                return dict(row)
    except Exception:
        pass
    return {"user_id": user_id, "default_sport": "basketball_nba", "divergence_threshold": 5.0, "notifications_enabled": 1, "theme": "dark"}


_BEHIND_GATEWAY = bool(os.environ.get("GATEWAY_SSO_SECRET"))
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"
if not _BEHIND_GATEWAY and not _DEV_MODE:
    logging.warning("GATEWAY_SSO_SECRET not set and DEV_MODE not enabled — sports dashboard will reject unauthenticated requests")


def get_current_user(request: Request) -> dict | None:
    """Extract current user from gateway SSO headers.

    When running behind the Habbig gateway, the upstream adds
    ``X-Gateway-User-Id`` and ``X-Gateway-User-Email`` headers after
    verifying the user's session and subscription. Trust is proved by a
    shared-secret header (``X-Gateway-Secret``).

    User IDs are now UUID strings from the gateway auth system. The gateway
    handles all authentication; this dashboard just trusts the forwarded headers.
    """
    _sso_secret = os.environ.get("GATEWAY_SSO_SECRET")
    if _sso_secret and hmac.compare_digest(request.headers.get("x-gateway-secret", ""), _sso_secret):
        gw_id = request.headers.get("x-gateway-user-id")
        gw_email = request.headers.get("x-gateway-user-email")
        if gw_id and gw_email:
            # Look up profile from local SQLite for admin status etc.
            try:
                with _get_db() as conn:
                    row = conn.execute(
                        "SELECT * FROM profiles WHERE id = ? LIMIT 1", (gw_id,)
                    ).fetchone()
                    if row:
                        profile = dict(row)
                        return {
                            "id": profile["id"],
                            "email": profile.get("email", gw_email),
                            "username": profile.get("username", gw_email.split("@")[0] if "@" in gw_email else gw_email),
                            "is_admin": profile.get("is_admin", 0),
                        }
            except Exception:
                pass
            # Fallback: gateway vouched for them, synthesize minimal record
            return {
                "id": gw_id,
                "email": gw_email,
                "username": gw_email.split("@")[0] if "@" in gw_email else gw_email,
                "is_admin": 0,
                "_gateway_sso": True,
            }

    # Bearer token auth — for programmatic API clients. Falls back to
    # other auth paths if the token is missing or invalid (we don't 401
    # here so a malformed Authorization header doesn't break sessions
    # that would otherwise auth via DEV_MODE or gateway SSO).
    authz = request.headers.get("authorization", "")
    if authz.startswith("Bearer "):
        token = authz[7:].strip()
        if token:
            user = _resolve_bearer_token(token)
            if user:
                return user

    # DEV_MODE: synthesize a local dev user so the dashboard renders without gateway SSO
    if _DEV_MODE:
        return {
            "id": "dev-user",
            "email": "dev@localhost",
            "username": "dev",
            "is_admin": 1,
            "_dev_mode": True,
        }

    # No gateway SSO -- not authenticated (auth handled by gateway)
    return None


def _hash_api_token(token: str) -> str:
    """SHA-256 of the token, hex-encoded. We never store the plaintext."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _resolve_bearer_token(token: str) -> dict | None:
    """Look up a Bearer token, return the user dict it authenticates.

    Updates last_used_at on hit. Constant-time compare via hash lookup
    rather than equality on the plaintext.
    """
    if not token or len(token) < 16 or len(token) > 100:
        return None
    token_hash = _hash_api_token(token)
    try:
        with _get_db() as conn:
            row = conn.execute(
                "SELECT t.id, t.user_id, t.scopes, p.email, p.username, p.is_admin "
                "FROM sports_api_tokens t "
                "LEFT JOIN profiles p ON p.id = t.user_id "
                "WHERE t.token_hash = ? AND t.revoked_at IS NULL LIMIT 1",
                (token_hash,),
            ).fetchone()
            if not row:
                return None
            # Bump last_used_at — best-effort, ignore failures
            try:
                conn.execute(
                    "UPDATE sports_api_tokens SET last_used_at = datetime('now') "
                    "WHERE id = ?",
                    (row["id"],),
                )
            except Exception:
                pass
    except Exception as e:
        log.debug("Bearer token lookup error: %s", e)
        return None
    return {
        "id": row["user_id"],
        "email": row["email"] or "",
        "username": row["username"] or "",
        "is_admin": row["is_admin"] or 0,
        "_bearer_token_id": row["id"],
    }


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI(title="Sports Betting Dashboard")
_cors_origins = ["http://192.168.178.160:8888", "http://localhost:8888"]
_cf_origin = os.getenv("CLOUDFLARE_ORIGIN")
if _cf_origin:
    _cors_origins.append(_cf_origin)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
)

# Static asset mount for the PWA manifest, service worker, icons.
_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # Fail closed: if no gateway secret and not in dev mode, reject all requests
    if not _BEHIND_GATEWAY and not _DEV_MODE:
        return JSONResponse({"error": "Service misconfigured"}, status_code=503)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self' wss:; frame-ancestors 'none'"
    if _BEHIND_GATEWAY:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


# In-memory state
# NOTE: dashboard_data holds data for a single sport at a time (the
# "active_sport"). When multiple users are connected and one switches
# the sport, the data_updater re-fetches for the new sport and all
# clients see that sport's data. The /api/data and /api/sports
# endpoints accept an optional ?sport= query parameter so clients can
# detect when the server-side data doesn't match their selected sport
# (e.g. another user switched it) and display a loading state.
dashboard_data = {
    "comparisons": [],
    "signals": [],
    "last_update": None,
    "odds_events_count": 0,
    "poly_markets_count": 0,
    "matched_count": 0,
    "active_sport": "basketball_nba",
    "api_requests_remaining": None,
    "error": None,
}
connected_ws: set[WebSocket] = set()

# ---------------------------------------------------------------------------
# Team name normalization for fuzzy matching
# ---------------------------------------------------------------------------
TEAM_ALIASES = {
    # ── EPL ─────────────────────────────────────────────────────────────────
    "man utd": "manchester united",
    "man city": "manchester city",
    "man united": "manchester united",
    "manchester utd": "manchester united",
    "spurs": "tottenham hotspur",
    "tottenham": "tottenham hotspur",
    "wolves": "wolverhampton wanderers",
    "wolverhampton": "wolverhampton wanderers",
    "wolverhampton wanderers": "wolverhampton wanderers",
    "newcastle": "newcastle united",
    "newcastle utd": "newcastle united",
    "brighton": "brighton and hove albion",
    "brighton & hove albion": "brighton and hove albion",
    "west ham": "west ham united",
    "nott'm forest": "nottingham forest",
    "nottm forest": "nottingham forest",
    "forest": "nottingham forest",
    "leicester": "leicester city",
    "ipswich": "ipswich town",
    "afc bournemouth": "bournemouth",
    "luton": "luton town",
    # ── La Liga ────────────────────────────────────────────────────────────
    "athletic bilbao": "athletic club",
    "atletico madrid": "atletico de madrid",
    "atletico": "atletico de madrid",
    "real": "real madrid",
    "barca": "barcelona",
    "fc barcelona": "barcelona",
    # ── Serie A ────────────────────────────────────────────────────────────
    "inter milan": "internazionale",
    "inter": "internazionale",
    "ac milan": "milan",
    "juve": "juventus",
    # ── Bundesliga ─────────────────────────────────────────────────────────
    "bayern": "bayern munich",
    "bayern munchen": "bayern munich",
    "bayern münchen": "bayern munich",
    "fc bayern": "bayern munich",
    "borussia dortmund": "dortmund",
    "bvb": "dortmund",
    "rb leipzig": "rasenballsport leipzig",
    "leipzig": "rasenballsport leipzig",
    "leverkusen": "bayer leverkusen",
    # ── Ligue 1 ────────────────────────────────────────────────────────────
    "psg": "paris saint-germain",
    "paris sg": "paris saint-germain",
    "paris": "paris saint-germain",
    # ── NBA ────────────────────────────────────────────────────────────────
    "sixers": "philadelphia 76ers",
    "76ers": "philadelphia 76ers",
    "philly": "philadelphia 76ers",
    "blazers": "portland trail blazers",
    "trail blazers": "portland trail blazers",
    "timberwolves": "minnesota timberwolves",
    "wolves nba": "minnesota timberwolves",
    "cavs": "cleveland cavaliers",
    "mavs": "dallas mavericks",
    "nuggets": "denver nuggets",
    "warriors": "golden state warriors",
    "gsw": "golden state warriors",
    "lakers": "los angeles lakers",
    "la lakers": "los angeles lakers",
    "clippers": "los angeles clippers",
    "la clippers": "los angeles clippers",
    "knicks": "new york knicks",
    "nets": "brooklyn nets",
    "bucks": "milwaukee bucks",
    "celtics": "boston celtics",
    "heat": "miami heat",
    "thunder": "oklahoma city thunder",
    "okc": "oklahoma city thunder",
    "okc thunder": "oklahoma city thunder",
    # ── NFL ────────────────────────────────────────────────────────────────
    "niners": "san francisco 49ers",
    "49ers": "san francisco 49ers",
    "sf 49ers": "san francisco 49ers",
    "bucs": "tampa bay buccaneers",
    "patriots": "new england patriots",
    "pats": "new england patriots",
    "kc": "kansas city chiefs",
    "kc chiefs": "kansas city chiefs",
    "chiefs": "kansas city chiefs",
    "packers": "green bay packers",
    "gb packers": "green bay packers",
    "ny giants": "new york giants",
    "ny jets": "new york jets",
    "la rams": "los angeles rams",
    "la chargers": "los angeles chargers",
    # ── MLB ────────────────────────────────────────────────────────────────
    "yankees": "new york yankees",
    "ny yankees": "new york yankees",
    "mets": "new york mets",
    "ny mets": "new york mets",
    "red sox": "boston red sox",
    "bosox": "boston red sox",
    "dodgers": "los angeles dodgers",
    "la dodgers": "los angeles dodgers",
    "angels": "los angeles angels",
    "la angels": "los angeles angels",
    "cubs": "chicago cubs",
    "white sox": "chicago white sox",
    "phillies": "philadelphia phillies",
    "braves": "atlanta braves",
    "astros": "houston astros",
    "rangers mlb": "texas rangers",
    # ── NHL ────────────────────────────────────────────────────────────────
    "leafs": "toronto maple leafs",
    "maple leafs": "toronto maple leafs",
    "habs": "montreal canadiens",
    "canadiens": "montreal canadiens",
    "sens": "ottawa senators",
    "jets nhl": "winnipeg jets",
    "preds": "nashville predators",
    "hawks": "chicago blackhawks",
    "blackhawks": "chicago blackhawks",
    "kings": "los angeles kings",
    "la kings": "los angeles kings",
    "ducks": "anaheim ducks",
    "sharks": "san jose sharks",
    "rangers nhl": "new york rangers",
    "ny rangers": "new york rangers",
    "isles": "new york islanders",
    "ny islanders": "new york islanders",
    "caps": "washington capitals",
    "pens": "pittsburgh penguins",
    "bruins": "boston bruins",
    "lightning": "tampa bay lightning",
    "bolts": "tampa bay lightning",
    "panthers nhl": "florida panthers",
    "stars": "dallas stars",
    "avs": "colorado avalanche",
    "vegas": "vegas golden knights",
    "golden knights": "vegas golden knights",
}


def normalize_name(name: str) -> str:
    lower = name.lower().strip()
    return TEAM_ALIASES.get(lower, lower)


# ---------------------------------------------------------------------------
# The Odds API
# ---------------------------------------------------------------------------

OUTRIGHT_SPORT_KEYS = {
    "soccer_epl": "soccer_epl_winner",
    "soccer_spain_la_liga": "soccer_spain_la_liga_winner",
    "soccer_germany_bundesliga": "soccer_germany_bundesliga_winner",
    "soccer_italy_serie_a": "soccer_italy_serie_a_winner",
    "soccer_france_ligue_one": "soccer_france_ligue_one_winner",
    "soccer_uefa_champs_league": "soccer_uefa_champs_league_winner",
    "soccer_uefa_europa_league": "soccer_uefa_europa_league_winner",
    "basketball_nba": "basketball_nba_championship_winner",
    "americanfootball_nfl": "americanfootball_nfl_super_bowl_winner",
    "icehockey_nhl": "icehockey_nhl_championship_winner",
    "baseball_mlb": "baseball_mlb_world_series_winner",
}


# ── Odds API quota tracking ─────────────────────────────────────────────────
# The Odds API returns x-requests-remaining and x-requests-used headers on
# every response. We track the most recent values so the dashboard can
# (a) surface them in diagnostics, (b) throttle polling when quota is low.
_ODDS_QUOTA: dict = {
    "remaining": None,    # int | None — most recent x-requests-remaining
    "used": None,         # int | None — most recent x-requests-used
    "last_remaining_check": None,  # ISO-8601 string of last successful read
    "low_water_mark": None,  # lowest 'remaining' we've seen this process
    "exhausted_count": 0,    # number of 429/quota-exhausted responses observed
}


def _record_odds_quota(resp: requests.Response) -> str | None:
    """Update _ODDS_QUOTA from an Odds API response. Returns remaining as str."""
    remaining_hdr = resp.headers.get("x-requests-remaining")
    used_hdr = resp.headers.get("x-requests-used")
    try:
        remaining_int = int(remaining_hdr) if remaining_hdr is not None else None
    except (TypeError, ValueError):
        remaining_int = None
    try:
        used_int = int(used_hdr) if used_hdr is not None else None
    except (TypeError, ValueError):
        used_int = None
    _ODDS_QUOTA["remaining"] = remaining_int
    _ODDS_QUOTA["used"] = used_int
    _ODDS_QUOTA["last_remaining_check"] = datetime.now(timezone.utc).isoformat()
    if remaining_int is not None:
        prev_low = _ODDS_QUOTA["low_water_mark"]
        if prev_low is None or remaining_int < prev_low:
            _ODDS_QUOTA["low_water_mark"] = remaining_int
        M_ODDS_REMAINING.set(remaining_int)
    if used_int is not None:
        M_ODDS_USED.set(used_int)
    return remaining_hdr


def odds_quota_remaining() -> int | None:
    """Best-known remaining Odds API quota, or None if not yet observed."""
    return _ODDS_QUOTA.get("remaining")


# ── Adaptive poll-interval policy ───────────────────────────────────────────
# Map (nearest game in N hours, quota remaining) -> sleep seconds. The
# closer kickoff is, the harder we poll. The lower the quota, the longer
# we sleep. Constants are tuned for the free Odds API tier (500 req/mo).

POLL_INTERVAL_PRE_GAME = 15      # <=30 min to kickoff anywhere on the active sport
POLL_INTERVAL_SOON = 60          # <=4 h to kickoff
POLL_INTERVAL_TODAY = 300        # <=24 h to kickoff
POLL_INTERVAL_IDLE = 1800        # nothing in the next day


def _hours_until_nearest_kickoff(comparisons: list[dict]) -> float | None:
    """Return hours-until-soonest commence_time across the comparison set,
    or None if no comparison has a parseable commence_time.

    Negative values (game already started) are clamped to 0 so we keep
    polling fast through live windows.
    """
    if not comparisons:
        return None
    now = datetime.now(timezone.utc)
    best: float | None = None
    for c in comparisons:
        ts = c.get("commence_time") or ""
        dt = _parse_iso_utc(ts)
        if dt is None:
            continue
        hours = (dt - now).total_seconds() / 3600.0
        hours = max(0.0, hours)
        if best is None or hours < best:
            best = hours
    return best


def _compute_poll_interval(comparisons: list[dict], remaining: int | None) -> int:
    """Decide how long to sleep before the next poll.

    Combines pre-game proximity with quota-aware throttling. The schedule
    multiplier shrinks the interval (poll faster) when a game is close;
    the quota multiplier expands it (poll slower) when the API budget is
    low. Final interval is the *max* of the two so we never poll faster
    than the quota allows.
    """
    hours = _hours_until_nearest_kickoff(comparisons)
    if hours is None:
        schedule = POLL_INTERVAL_IDLE
    elif hours <= 0.5:
        schedule = POLL_INTERVAL_PRE_GAME
    elif hours <= 4:
        schedule = POLL_INTERVAL_SOON
    elif hours <= 24:
        schedule = POLL_INTERVAL_TODAY
    else:
        schedule = POLL_INTERVAL_IDLE

    # Quota floor — when remaining is low, ignore the schedule entirely.
    floor = 0
    if remaining is not None:
        if remaining <= 25:
            floor = 1800
        elif remaining <= 100:
            floor = 900
        elif remaining <= 300:
            floor = 600

    return max(schedule, floor)


# Throttle the "ODDS_API_KEY missing" warning to once per hour so the log
# isn't swamped, but ensure it appears (every 12 polls = once per ~hour).
_ODDS_KEY_WARN_LAST = 0.0
_ODDS_KEY_WARN_INTERVAL = 3600  # seconds


def _warn_odds_key_missing(caller: str) -> None:
    global _ODDS_KEY_WARN_LAST
    now = time.time()
    if now - _ODDS_KEY_WARN_LAST >= _ODDS_KEY_WARN_INTERVAL:
        _ODDS_KEY_WARN_LAST = now
        print(f"⚠ {caller}: ODDS_API_KEY is empty — bookmaker odds will be empty. "
              f"Set it in gateway/.env.production and restart polymarket-sports.",
              flush=True)


# ── the-odds-api quota circuit breaker ───────────────────────────────────
# the-odds-api free tier is 500 requests/month. Once exhausted, the API
# returns HTTP 401 with body {"error_code": "OUT_OF_USAGE_CREDITS"} on
# every subsequent call.
#
# Without a breaker, the polling loop keeps hitting the API every 5 min,
# producing log noise AND — critically — burning through the next month's
# allowance the instant the quota resets (8,640 calls/month at 5-min poll
# vs 500 free quota → exhausted within 42h of reset).
#
# The breaker:
#   - Trips when we see 401 with OUT_OF_USAGE_CREDITS
#   - Stays open for 6h, then makes one probe to detect quota reset
#   - On subsequent 200 responses, closes again
# So during quota-exhausted periods we make ~4 calls/day instead of 288,
# which means a fresh 500-quota actually lasts a couple weeks instead of
# a couple days.
_ODDS_BREAKER_OPEN_UNTIL = 0.0
_ODDS_BREAKER_PROBE_INTERVAL_S = 6 * 3600
_ODDS_BREAKER_LAST_REASON = ""


def _is_odds_quota_error(resp: requests.Response) -> bool:
    if resp.status_code != 401:
        return False
    try:
        body = resp.json()
        return body.get("error_code") == "OUT_OF_USAGE_CREDITS"
    except Exception:
        return False


def fetch_odds(sport_key: str, markets: str = "h2h,spreads,totals") -> tuple[list[dict], str | None]:
    """Fetch match odds from The Odds API. Returns (events, requests_remaining)."""
    global _ODDS_BREAKER_OPEN_UNTIL, _ODDS_BREAKER_LAST_REASON

    if not ODDS_API_KEY:
        _warn_odds_key_missing("fetch_odds")
        return [], None

    if time.time() < _ODDS_BREAKER_OPEN_UNTIL:
        # Breaker still open — degrade silently, dashboard falls back to
        # Polymarket↔Kalshi cross-venue arbitrage as the primary signal.
        return [], None

    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "eu,uk,us",
        "markets": markets,
        "oddsFormat": "decimal",
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
    except requests.RequestException as e:
        log.warning("Odds API network error for %s: %s", sport_key, e)
        return [], None
    # the-odds-api quota: hard exhaustion returns HTTP 401 OUT_OF_USAGE_CREDITS
    # (trip the 6h breaker); transient over-rate returns 429 (record metric).
    if _is_odds_quota_error(resp):
        _ODDS_BREAKER_OPEN_UNTIL = time.time() + _ODDS_BREAKER_PROBE_INTERVAL_S
        _ODDS_BREAKER_LAST_REASON = "OUT_OF_USAGE_CREDITS"
        _ODDS_QUOTA["exhausted_count"] += 1
        M_ODDS_EXHAUSTED.inc()
        print(
            f"⚠ the-odds-api quota exhausted (HTTP 401, OUT_OF_USAGE_CREDITS). "
            f"Suspending bookmaker fetch for 6h. Dashboard will fall back to "
            f"Polymarket↔Kalshi cross-venue arbitrage. To restore: upgrade "
            f"plan at https://the-odds-api.com or rotate ODDS_API_KEY.",
            flush=True,
        )
        return [], None
    if resp.status_code == 429:
        _ODDS_QUOTA["exhausted_count"] += 1
        M_ODDS_EXHAUSTED.inc()
        log.warning("Odds API quota exhausted for %s (429)", sport_key)
        return [], None
    resp.raise_for_status()
    remaining = _record_odds_quota(resp)
    return resp.json(), remaining


def fetch_outright_odds(sport_key: str) -> tuple[list[dict], str | None]:
    """Fetch outright/futures odds for a sport (e.g. league winner)."""
    global _ODDS_BREAKER_OPEN_UNTIL, _ODDS_BREAKER_LAST_REASON

    if not ODDS_API_KEY:
        _warn_odds_key_missing("fetch_outright_odds")
        return [], None

    if time.time() < _ODDS_BREAKER_OPEN_UNTIL:
        return [], None

    outright_key = OUTRIGHT_SPORT_KEYS.get(sport_key)
    if not outright_key:
        return [], None

    url = f"https://api.the-odds-api.com/v4/sports/{outright_key}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "eu,uk,us",
        "markets": "outrights",
        "oddsFormat": "decimal",
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        if _is_odds_quota_error(resp):
            _ODDS_BREAKER_OPEN_UNTIL = time.time() + _ODDS_BREAKER_PROBE_INTERVAL_S
            _ODDS_BREAKER_LAST_REASON = "OUT_OF_USAGE_CREDITS"
            _ODDS_QUOTA["exhausted_count"] += 1
            M_ODDS_EXHAUSTED.inc()
            return [], None
        if resp.status_code == 429:
            _ODDS_QUOTA["exhausted_count"] += 1
            M_ODDS_EXHAUSTED.inc()
            return [], None
        resp.raise_for_status()
        remaining = _record_odds_quota(resp)
        return resp.json(), remaining
    except Exception:
        return [], None


# ── Player props ────────────────────────────────────────────────────────────
# The Odds API exposes player props on a per-event endpoint, which is much
# more expensive than the per-sport h2h/spreads/totals call. We cache
# aggressively (10 min TTL per event) and only fetch for events that are
# imminent (next 6 hours) to keep monthly quota burn manageable.

PROP_MARKETS_BY_SPORT = {
    # NBA props that overlap with Kalshi's coverage (KXNBAPTS / KXNBA3PT / KXNBAAST / KXNBAREB)
    "basketball_nba": "player_points,player_rebounds,player_assists,player_threes",
    # NFL — most-traded prop markets
    "americanfootball_nfl": "player_pass_tds,player_pass_yds,player_rush_yds,player_receptions,player_anytime_td",
    # MLB — batter + pitcher headliners
    "baseball_mlb": "batter_hits,batter_home_runs,batter_total_bases,pitcher_strikeouts",
    # NHL — scoring side
    "icehockey_nhl": "player_goals,player_assists,player_points,player_shots",
}

PROP_CACHE_TTL_SECONDS = 600  # 10 min — props move less than h2h
PROP_EVENT_LOOKAHEAD_HOURS = 6  # only fetch for games this close to kickoff

# {(sport, event_id): {"ts": float, "data": list[dict], "remaining": int|None}}
_PROP_CACHE: dict[tuple[str, str], dict] = {}


def fetch_player_props_for_event(sport_key: str, event_id: str) -> tuple[list[dict], str | None]:
    """Fetch player-prop odds for one event. Cached for PROP_CACHE_TTL_SECONDS.

    Returns (raw_event_dict_list, remaining_quota). The Odds API returns a
    single event-shaped dict, but we wrap it in a list so the parser
    signature matches parse_odds_events.
    """
    if not ODDS_API_KEY:
        return [], None
    markets = PROP_MARKETS_BY_SPORT.get(sport_key)
    if not markets:
        return [], None

    key = (sport_key, event_id)
    now = time.time()
    cached = _PROP_CACHE.get(key)
    if cached and (now - cached["ts"]) < PROP_CACHE_TTL_SECONDS:
        return cached["data"], cached["remaining"]

    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/events/{event_id}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",  # player props are US-side only at the major books
        "markets": markets,
        "oddsFormat": "decimal",
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
    except requests.RequestException as e:
        log.warning("Player-prop fetch network error for %s/%s: %s", sport_key, event_id, e)
        return [], None
    if resp.status_code == 429:
        _ODDS_QUOTA["exhausted_count"] += 1
        M_ODDS_EXHAUSTED.inc()
        return [], None
    if resp.status_code == 404:
        # Event has no props posted yet (typical for distant future games)
        return [], None
    try:
        resp.raise_for_status()
    except Exception as e:
        log.debug("Player-prop fetch non-OK %s/%s: %s", sport_key, event_id, e)
        return [], None
    remaining = _record_odds_quota(resp)
    data = [resp.json()] if resp.json() else []
    _PROP_CACHE[key] = {"ts": now, "data": data, "remaining": remaining}
    return data, remaining


def fetch_imminent_events(sport_key: str) -> list[dict]:
    """List events starting within PROP_EVENT_LOOKAHEAD_HOURS. Cheap call
    (no markets in the params), used to gate the per-event prop fetch.
    """
    if not ODDS_API_KEY:
        return []
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/events"
    params = {"apiKey": ODDS_API_KEY}
    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            return []
        _record_odds_quota(resp)
    except requests.RequestException:
        return []
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=PROP_EVENT_LOOKAHEAD_HOURS)
    out: list[dict] = []
    for ev in resp.json() or []:
        dt = _parse_iso_utc(ev.get("commence_time", ""))
        if dt is None:
            continue
        # Include in-progress games (started up to 4 hours ago)
        if (now - timedelta(hours=4)) <= dt <= cutoff:
            out.append(ev)
    return out


# ── Player-name normalization ───────────────────────────────────────────────
# Bookmakers, Kalshi, and Polymarket all spell player names slightly
# differently ("LeBron James" vs "L. James" vs "lebron-james"). We
# normalize to lowercase first-initial-last-name form for matching, and
# keep a small alias table for the cases where that doesn't work
# (suffixes, hyphenation, common nicknames).

PLAYER_NAME_ALIASES = {
    # Common-name collisions resolved by team affiliation in match logic;
    # this table is for spelling/nickname normalization only.
    "lebron": "lebron james",
    "kd": "kevin durant",
    "steph": "stephen curry",
    "steph curry": "stephen curry",
    "giannis": "giannis antetokounmpo",
    "luka": "luka doncic",
    "jokic": "nikola jokic",
    "embiid": "joel embiid",
    "tatum": "jayson tatum",
    "ja": "ja morant",
    "shai": "shai gilgeous-alexander",
    "sga": "shai gilgeous-alexander",
    "kawhi": "kawhi leonard",
    "pg": "paul george",
    "pg13": "paul george",
    "ad": "anthony davis",
    "cp3": "chris paul",
    "klay": "klay thompson",
    "dame": "damian lillard",
    "dame lillard": "damian lillard",
    "mahomes": "patrick mahomes",
    "lamar": "lamar jackson",
    "josh allen": "josh allen",
    "ja'marr chase": "ja'marr chase",
    "ja marr chase": "ja'marr chase",
    "mcdavid": "connor mcdavid",
    "ovi": "alexander ovechkin",
    "ohtani": "shohei ohtani",
}

_PLAYER_NAME_STRIP = re.compile(r"[^a-z'\s-]")


def normalize_player_name(name: str) -> str:
    """Lowercase, strip suffixes (Jr./Sr./II/III), collapse spaces, apply
    alias table. Returns a canonical form for fuzzy matching."""
    if not name:
        return ""
    n = name.lower().strip()
    n = _PLAYER_NAME_STRIP.sub("", n)
    # Strip suffixes
    for suffix in (" jr", " sr", " ii", " iii", " iv"):
        if n.endswith(suffix):
            n = n[: -len(suffix)].strip()
    # Collapse internal whitespace
    n = " ".join(n.split())
    return PLAYER_NAME_ALIASES.get(n, n)


def parse_player_props(raw: list[dict]) -> list[dict]:
    """Parse The Odds API per-event player-prop response into a flat list
    of {player, market, line, books{}} dicts.

    The Odds API shape for player props is:
      event:
        bookmakers: [
          {key, title, markets: [
            {key: 'player_points', outcomes: [
              {name: 'Over', description: 'LeBron James', point: 25.5, price: 1.85},
              {name: 'Under', description: 'LeBron James', point: 25.5, price: 1.95},
              ...
            ]}
          ]}
        ]

    We pivot to per-(player, market, line) rows so each row can be matched
    against Kalshi and Polymarket independently.
    """
    out: list[dict] = []
    for ev in raw:
        commence_time = ev.get("commence_time", "")
        home = ev.get("home_team", "")
        away = ev.get("away_team", "")
        event_id = ev.get("id", "")

        # First pass: collect per-(player, market, line) book quotes
        # Key: (player_norm, market_key, line)
        bucket: dict[tuple[str, str, float], dict] = {}
        for bk in ev.get("bookmakers") or []:
            bk_key = bk.get("key", "")
            for mkt in bk.get("markets") or []:
                market_key = mkt.get("key", "")
                if not market_key.startswith(("player_", "batter_", "pitcher_")):
                    continue
                for oc in mkt.get("outcomes") or []:
                    side = (oc.get("name") or "").lower()  # "over" or "under"
                    if side not in ("over", "under", "yes", "no"):
                        continue
                    player_raw = oc.get("description") or oc.get("name") or ""
                    if not player_raw or player_raw.lower() in ("over", "under", "yes", "no"):
                        continue
                    try:
                        line = float(oc.get("point") or 0)
                        price = float(oc.get("price") or 0)
                    except (TypeError, ValueError):
                        continue
                    if price <= 0:
                        continue
                    implied = (1.0 / price) * 100.0
                    player_norm = normalize_player_name(player_raw)
                    key = (player_norm, market_key, round(line, 1))
                    if key not in bucket:
                        bucket[key] = {
                            "player": player_raw,
                            "player_norm": player_norm,
                            "market": market_key,
                            "line": round(line, 1),
                            "books": {},
                        }
                    if bk_key not in bucket[key]["books"]:
                        bucket[key]["books"][bk_key] = {"title": bk.get("title", bk_key)}
                    bucket[key]["books"][bk_key][f"{side}_prob"] = round(implied, 2)
                    bucket[key]["books"][bk_key][f"{side}_odds"] = price

        # Second pass: enrich each row with event meta + de-vigged consensus
        for row in bucket.values():
            row["event"] = f"{away} @ {home}" if home and away else (home or away or "")
            row["event_id"] = event_id
            row["commence_time"] = commence_time
            # Mean over_prob and under_prob across books that quoted both sides.
            over_probs = [b["over_prob"] for b in row["books"].values() if "over_prob" in b]
            under_probs = [b["under_prob"] for b in row["books"].values() if "under_prob" in b]
            paired = [(b["over_prob"], b["under_prob"])
                      for b in row["books"].values()
                      if "over_prob" in b and "under_prob" in b]
            row["consensus_over_pp"] = round(sum(over_probs) / len(over_probs), 2) if over_probs else None
            row["consensus_under_pp"] = round(sum(under_probs) / len(under_probs), 2) if under_probs else None
            if paired:
                vig_per_book = [(o + u) - 100.0 for o, u in paired]
                row["vig_pct"] = round(sum(vig_per_book) / len(vig_per_book), 2)
                # De-vigged consensus = over_prob / (over_prob + under_prob), averaged
                devigged = [o / (o + u) * 100.0 for o, u in paired if (o + u) > 0]
                row["consensus_over_devigged"] = round(sum(devigged) / len(devigged), 2) if devigged else None
            else:
                row["vig_pct"] = 0.0
                row["consensus_over_devigged"] = row["consensus_over_pp"]
            out.append(row)
    return out


def parse_odds_events(raw: list[dict], market_type: str = "h2h") -> list[dict]:
    """Parse odds into structured events with implied probabilities."""
    events = []
    for ev in raw:
        bookmakers_data = {}
        for bk in ev.get("bookmakers", []):
            for mkt in bk.get("markets", []):
                if mkt["key"] != market_type:
                    continue
                outcomes = {}
                for o in mkt["outcomes"]:
                    # Defensively skip outcomes with missing/zero/negative price
                    # — a single bad row would otherwise ZeroDivision the entire
                    # parse loop and stall the data updater on stale data.
                    try:
                        price = float(o.get("price") or 0)
                    except (TypeError, ValueError):
                        continue
                    if price <= 0:
                        continue
                    implied = (1.0 / price) * 100
                    label = o.get("name") or ""
                    if not label:
                        continue
                    # For spreads/totals, include point value in label
                    if market_type == "spreads" and o.get("point") is not None:
                        label = f"{label} {o['point']:+g}"
                    elif market_type == "totals" and o.get("point") is not None:
                        label = f"{label} {o['point']}"
                    outcomes[label] = {
                        "decimal_odds": price,
                        "implied_prob": round(implied, 2),
                        "point": o.get("point"),
                    }
                bk_key = f"{bk['key']}_{market_type}" if market_type != "h2h" else bk["key"]
                bookmakers_data[bk_key] = {
                    "title": bk["title"],
                    "last_update": bk["last_update"],
                    "outcomes": outcomes,
                }

        if not bookmakers_data:
            continue

        # Compute consensus (average) implied probs across all bookmakers
        all_outcomes = {}
        for bk_data in bookmakers_data.values():
            for name, data in bk_data["outcomes"].items():
                if name not in all_outcomes:
                    all_outcomes[name] = []
                all_outcomes[name].append(data["implied_prob"])

        consensus = {}
        for name, probs in all_outcomes.items():
            avg = sum(probs) / len(probs)
            consensus[name] = round(avg, 2)

        # Find sharpest book (Pinnacle preferred)
        pin_key = f"pinnacle_{market_type}" if market_type != "h2h" else "pinnacle"
        sharp_key = pin_key if pin_key in bookmakers_data else next(iter(bookmakers_data), None)
        if sharp_key is None:
            continue
        sharp = bookmakers_data[sharp_key]

        events.append({
            "id": ev["id"],
            "home_team": ev["home_team"],
            "away_team": ev["away_team"],
            "commence_time": ev["commence_time"],
            "bookmakers": bookmakers_data,
            "sharp_book": sharp_key,
            "sharp_outcomes": sharp["outcomes"],
            "consensus_probs": consensus,
            "num_bookmakers": len(bookmakers_data),
            "market_type": market_type,
        })
    return events


# ── Cross-book arbitrage scanners ───────────────────────────────────────────
# Find +EV plays that exist *without* Polymarket — pure book-vs-book.
#
# Low-hold (low-vig) arb: when the best OVER price at one book and the
# best UNDER price at a different book sum to < 100% implied probability,
# betting both sides locks in a profit equal to the negative-hold margin.
#
# Middle: when book A has a spread/total at X.5 and book B has the same
# market at (X+N).5 where N >= 1, betting OVER at the lower book and
# UNDER at the higher book locks in a guaranteed win for the side that's
# right + an additional payout if the final result lands inside (X, X+N).
# These are rare but high-EV when they appear.

CROSS_BOOK_MIN_GAP_PP = 0.5  # below this the "arb" is within bookmaker margin


def _best_per_outcome(parsed_events: list[dict]) -> list[dict]:
    """For each (event, market_type), return per-outcome best implied
    probability and the book offering it. Used as input to both
    low-hold and middle scanners.

    Note: parsed_events here comes from parse_odds_events. Each event
    has `bookmakers` keyed `<bookmaker>_<market_type>` (or bare for h2h)
    with each book's outcomes inside.
    """
    out: list[dict] = []
    for ev in parsed_events:
        market_type = ev.get("market_type", "h2h")
        # outcome_name -> {"best": {"book": str, "prob": float, "title": str}}
        per_outcome: dict[str, dict] = {}
        for bk_key, bk_data in (ev.get("bookmakers") or {}).items():
            # Strip the market_type suffix that parse_odds_events appends
            bk_name = bk_key.rsplit("_", 1)[0] if "_" in bk_key else bk_key
            for oc_name, oc_data in (bk_data.get("outcomes") or {}).items():
                prob = float(oc_data.get("implied_prob") or 0)
                if prob <= 0:
                    continue
                rec = per_outcome.setdefault(oc_name, {"book": bk_name,
                                                       "title": bk_data.get("title", bk_name),
                                                       "prob": prob,
                                                       "point": oc_data.get("point")})
                # We want the LOWEST implied prob (= highest decimal odds = best price)
                if prob < rec["prob"]:
                    rec["book"] = bk_name
                    rec["title"] = bk_data.get("title", bk_name)
                    rec["prob"] = prob
                    rec["point"] = oc_data.get("point")
        out.append({
            "event_id": ev.get("id", ""),
            "home_team": ev.get("home_team", ""),
            "away_team": ev.get("away_team", ""),
            "commence_time": ev.get("commence_time", ""),
            "market_type": market_type,
            "best_per_outcome": per_outcome,
        })
    return out


def find_low_hold_opportunities(parsed_events: list[dict]) -> list[dict]:
    """Scan for low-hold (negative-vig) opportunities across books.

    For each event+market_type, sum the best implied prob per outcome.
    If the sum is < 100%, the gap is risk-free profit (subject to limits,
    bonus restrictions, and exchanges' withdrawal rules — disclaimer is
    in the UI).
    """
    rows: list[dict] = []
    for entry in _best_per_outcome(parsed_events):
        per_oc = entry["best_per_outcome"]
        if len(per_oc) < 2:
            continue
        total_implied = sum(oc["prob"] for oc in per_oc.values())
        gap_pp = round(100.0 - total_implied, 3)
        if gap_pp < CROSS_BOOK_MIN_GAP_PP:
            continue
        legs = [
            {"outcome": name, "book": rec["book"], "book_title": rec["title"],
             "implied_prob": round(rec["prob"], 2),
             "decimal_odds": round(100.0 / rec["prob"], 3) if rec["prob"] > 0 else None,
             "point": rec["point"]}
            for name, rec in per_oc.items()
        ]
        rows.append({
            "event": f"{entry['home_team']} vs {entry['away_team']}",
            "home_team": entry["home_team"],
            "away_team": entry["away_team"],
            "commence_time": entry["commence_time"],
            "market_type": entry["market_type"],
            "total_implied_pp": round(total_implied, 2),
            "gap_pp": gap_pp,
            # Profit % on a $100 stake split proportionally between legs
            "profit_pct": round((100.0 / total_implied - 1.0) * 100, 3) if total_implied > 0 else 0.0,
            "legs": legs,
        })
    rows.sort(key=lambda r: -r["gap_pp"])
    return rows


def find_middle_opportunities(parsed_events: list[dict]) -> list[dict]:
    """Scan for middling opportunities on spreads and totals.

    A middle exists when one book offers OVER at a line and another book
    offers UNDER at a higher line. If the final result lands strictly
    between the two lines, both legs win; otherwise the side that's right
    pays out and the other is a loss (you lose the vig on the wrong leg).

    Worth the bet when the implied probability of landing in the middle
    exceeds the cost of vig on the wrong leg.
    """
    rows: list[dict] = []
    for ev in parsed_events:
        market_type = ev.get("market_type", "")
        if market_type not in ("spreads", "totals"):
            continue
        # Collect every (book, side, line, implied_prob) quote.
        # For spreads, labels look like "Lakers +3.5" / "Warriors -3.5"
        # For totals, labels look like "Over 220.5" / "Under 220.5"
        over_quotes: list[dict] = []   # higher score = win
        under_quotes: list[dict] = []  # lower score = win
        for bk_key, bk_data in (ev.get("bookmakers") or {}).items():
            bk_name = bk_key.rsplit("_", 1)[0] if "_" in bk_key else bk_key
            bk_title = bk_data.get("title", bk_name)
            for oc_name, oc_data in (bk_data.get("outcomes") or {}).items():
                point = oc_data.get("point")
                prob = float(oc_data.get("implied_prob") or 0)
                if prob <= 0 or point is None:
                    continue
                label = oc_name.lower()
                if market_type == "totals":
                    if label.startswith("over"):
                        over_quotes.append({"book": bk_name, "title": bk_title,
                                             "outcome": oc_name, "line": float(point),
                                             "implied_prob": prob})
                    elif label.startswith("under"):
                        under_quotes.append({"book": bk_name, "title": bk_title,
                                              "outcome": oc_name, "line": float(point),
                                              "implied_prob": prob})
                else:
                    # spreads: positive point = underdog (wins if score margin > -point),
                    # negative point = favorite (wins if margin > -point). For middling
                    # we look at the underdog side (positive point) as "over" the line,
                    # favorite (negative point) as "under". Equivalently, pair by team:
                    # bet team A +X at book X, bet team A -Y at book Y — middle hits if
                    # team A wins by between Y and X.
                    if point > 0:
                        over_quotes.append({"book": bk_name, "title": bk_title,
                                             "outcome": oc_name, "line": float(point),
                                             "implied_prob": prob, "team": oc_name})
                    else:
                        under_quotes.append({"book": bk_name, "title": bk_title,
                                              "outcome": oc_name, "line": float(point),
                                              "implied_prob": prob, "team": oc_name})

        # Find pairs where OVER line < UNDER line (totals)
        # or for spreads where (team_underdog +X) and (team_favorite -Y) have X > Y
        # Skip same-book pairs (need cross-book to be useful)
        for over in over_quotes:
            for under in under_quotes:
                if over["book"] == under["book"]:
                    continue
                if market_type == "totals":
                    if over["line"] >= under["line"]:
                        continue
                    middle_width = round(under["line"] - over["line"], 1)
                else:  # spreads
                    # We want underdog +X and favorite -Y where the team names
                    # don't match (i.e. opposite sides of the same game).
                    if over.get("team") == under.get("team"):
                        continue
                    # For spreads, "over.line" is positive (underdog spread),
                    # "under.line" is negative (favorite spread). Middle width
                    # = over.line - abs(under.line). A real middle requires
                    # over.line > abs(under.line).
                    fav_line = abs(under["line"])
                    if over["line"] <= fav_line:
                        continue
                    middle_width = round(over["line"] - fav_line, 1)

                # Cost: sum of vig on both legs (rough estimate — accurate
                # arithmetic depends on stake sizing). If both legs are at
                # implied prob ~50%, vig ≈ (over.prob + under.prob - 100)
                total_implied = over["implied_prob"] + under["implied_prob"]
                cost_pp = round(total_implied - 100.0, 2)
                rows.append({
                    "event": f"{ev.get('home_team', '')} vs {ev.get('away_team', '')}",
                    "home_team": ev.get("home_team", ""),
                    "away_team": ev.get("away_team", ""),
                    "commence_time": ev.get("commence_time", ""),
                    "market_type": market_type,
                    "middle_width": middle_width,
                    "cost_pp": cost_pp,
                    "over_leg": {**over, "implied_prob": round(over["implied_prob"], 2)},
                    "under_leg": {**under, "implied_prob": round(under["implied_prob"], 2)},
                })

    # Sort by widest middle first (more room = higher chance of hitting)
    rows.sort(key=lambda r: (-r["middle_width"], r["cost_pp"]))
    return rows


def parse_outright_events(raw: list[dict]) -> list[dict]:
    """Parse outright/futures odds into team -> implied probability mapping."""
    outrights = []
    for ev in raw:
        for bk in ev.get("bookmakers", []):
            for mkt in bk.get("markets", []):
                if mkt["key"] != "outrights":
                    continue
                for o in mkt["outcomes"]:
                    if o["price"] > 0:
                        implied = (1.0 / o["price"]) * 100
                        outrights.append({
                            "team": o["name"],
                            "bookmaker_key": bk["key"],
                            "bookmaker_title": bk["title"],
                            "decimal_odds": o["price"],
                            "implied_prob": round(implied, 2),
                        })

    # Group by team, keep per-bookmaker detail
    team_probs = {}
    for o in outrights:
        if o["team"] not in team_probs:
            team_probs[o["team"]] = {"bookmakers": {}, "sharp_prob": None}
        team_probs[o["team"]]["bookmakers"][o["bookmaker_key"]] = {
            "title": o["bookmaker_title"],
            "implied_prob": o["implied_prob"],
            "decimal_odds": o["decimal_odds"],
        }
        if o["bookmaker_key"] == "pinnacle":
            team_probs[o["team"]]["sharp_prob"] = o["implied_prob"]

    result = {}
    for team, data in team_probs.items():
        probs = [b["implied_prob"] for b in data["bookmakers"].values()]
        avg = sum(probs) / len(probs)
        sharp = data["sharp_prob"] or avg
        result[team] = {
            "consensus_prob": round(avg, 2),
            "sharp_prob": round(sharp, 2),
            "num_bookmakers": len(probs),
            "bookmakers": data["bookmakers"],  # per-bookmaker detail
        }
    return result


# ---------------------------------------------------------------------------
# Polymarket Gamma API
# ---------------------------------------------------------------------------

SPORT_TAG_KEYWORDS = [
    "sports", "soccer", "football", "nba", "nfl", "nhl", "mlb", "mma",
    "basketball", "baseball", "hockey", "epl", "premier league",
    "la liga", "bundesliga", "serie a", "ligue 1", "champions league",
    "europa league", "ufc",
    # Esports
    "esports", "esport", "lol", "league of legends", "cs2", "csgo",
    "counter-strike", "counter strike", "valorant", "dota", "overwatch",
    "lck", "lpl", "lec", "vct",
]

# Map our sport keys to keywords that should appear in Polymarket tags/titles
SPORT_POLY_FILTERS = {
    "basketball_nba": ["nba", "basketball"],
    "americanfootball_nfl": ["nfl", "football", "super bowl"],
    "icehockey_nhl": ["nhl", "hockey", "stanley cup"],
    "baseball_mlb": ["mlb", "baseball", "world series"],
    "soccer_epl": ["premier league", "epl", "english premier"],
    "soccer_spain_la_liga": ["la liga", "spanish"],
    "soccer_germany_bundesliga": ["bundesliga", "german"],
    "soccer_italy_serie_a": ["serie a", "italian"],
    "soccer_france_ligue_one": ["ligue 1", "french"],
    "soccer_uefa_champs_league": ["champions league", "ucl"],
    "soccer_uefa_europa_league": ["europa league"],
    "soccer_usa_mls": ["mls", "major league soccer"],
    "mma_mixed_martial_arts": ["mma", "ufc", "mixed martial"],
    "boxing_boxing": ["boxing", "fight night", "heavyweight", "middleweight"],
    "tennis_atp": ["atp", "tennis", "grand slam", "wimbledon", "us open tennis", "roland garros", "australian open tennis"],
    "tennis_wta": ["wta", "women's tennis"],
    "motorsport_f1": ["formula 1", "f1", "grand prix", "formula one"],
    "americanfootball_ncaaf": ["ncaaf", "college football", "cfp", "heisman"],
    # Esports
    "esports_lol_lck": ["lck", "league of legends", "lol"],
    "esports_lol_lpl": ["lpl", "league of legends", "lol"],
    "esports_lol_lec": ["lec", "league of legends", "lol"],
    "esports_lol_worlds": ["worlds", "league of legends", "lol"],
    "esports_cs2": ["cs2", "csgo", "counter-strike", "counter strike", "cache"],
    "esports_valorant": ["valorant", "vct", "champions tour"],
    "esports_dota2": ["dota", "the international"],
}


def _make_http_session() -> requests.Session:
    """Create a new requests.Session for thread-safe HTTP calls.

    Each background thread should call this to get its own session rather
    than sharing a global one (requests.Session is not thread-safe).
    """
    return requests.Session()


def fetch_polymarket_sports() -> list[dict]:
    """Fetch sports events from Polymarket Gamma API."""
    session = _make_http_session()
    all_events = []
    for offset in range(0, 500, 100):
        url = f"{GAMMA_API_HOST}/events"
        params = {
            "closed": "false",
            "active": "true",
            "limit": 100,
            "offset": offset,
            "tag": "sports",
        }
        try:
            resp = session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            events = resp.json()
        except Exception:
            break

        if not events:
            break

        for ev in events:
            tags = [t.get("label", "").lower() for t in ev.get("tags", [])]
            title = (ev.get("title") or "").lower()
            # Check if this is a sports event
            is_sport = any(kw in tag for tag in tags for kw in SPORT_TAG_KEYWORDS)
            if not is_sport:
                is_sport = any(kw in title for kw in SPORT_TAG_KEYWORDS)
            if is_sport:
                all_events.append(ev)

    return all_events


def parse_polymarket_events(raw: list[dict]) -> list[dict]:
    """Parse Gamma API events into structured format with prices."""
    parsed = []
    for ev in raw:
        markets = ev.get("markets", [])
        if not markets:
            continue

        for mkt in markets:
            outcomes_raw = mkt.get("outcomes", [])
            prices_raw = mkt.get("outcomePrices", [])
            clob_ids_raw = mkt.get("clobTokenIds", [])
            if not outcomes_raw or not prices_raw:
                continue

            # These can be JSON strings or lists
            if isinstance(outcomes_raw, str):
                try:
                    outcomes_raw = json.loads(outcomes_raw)
                except Exception:
                    continue
            if isinstance(prices_raw, str):
                try:
                    prices_raw = json.loads(prices_raw)
                except Exception:
                    continue
            if isinstance(clob_ids_raw, str):
                try:
                    clob_ids_raw = json.loads(clob_ids_raw)
                except Exception:
                    clob_ids_raw = []

            try:
                prices = [float(p) for p in prices_raw]
            except (ValueError, TypeError):
                continue

            outcome_data = {}
            for i, name in enumerate(outcomes_raw):
                if i < len(prices):
                    outcome_data[name] = {
                        "price": prices[i],
                        "implied_prob": round(prices[i] * 100, 2),
                        "token_id": clob_ids_raw[i] if i < len(clob_ids_raw) else "",
                    }

            # Use groupItemTitle if available (e.g. team name for multi-outcome events)
            group_title = mkt.get("groupItemTitle", "")

            parsed.append({
                "event_id": ev.get("id", ""),
                "event_title": ev.get("title", ""),
                "market_question": mkt.get("question", ""),
                "group_title": group_title,
                "slug": ev.get("slug", ""),
                "condition_id": mkt.get("conditionId", ""),
                "outcomes": outcome_data,
                "volume": float(mkt.get("volumeNum", 0) or mkt.get("volume", 0) or 0),
                "liquidity": float(mkt.get("liquidityNum", 0) or mkt.get("liquidity", 0) or ev.get("liquidity", 0) or 0),
                "liquidity_clob": float(mkt.get("liquidityClob", 0) or ev.get("liquidityClob", 0) or 0),
                "best_bid": float(mkt.get("bestBid", 0) or 0),
                "best_ask": float(mkt.get("bestAsk", 0) or 0),
                "spread": float(mkt.get("spread", 0) or 0),
                "one_day_change": float(mkt.get("oneDayPriceChange", 0) or 0),
                "one_week_change": float(mkt.get("oneWeekPriceChange", 0) or 0),
                "last_trade_price": float(mkt.get("lastTradePrice", 0) or 0),
                "tags": [t.get("label", "") for t in ev.get("tags", [])],
                "start_date": ev.get("startDate") or mkt.get("startDate", "") or "",
                "end_date": ev.get("endDate") or mkt.get("endDate", "") or ev.get("closedTime", "") or "",
            })
    return parsed


# ---------------------------------------------------------------------------
# Kalshi API
# ---------------------------------------------------------------------------

_kalshi_cache: list[dict] = []
_kalshi_cache_time: float = 0


# ── Kalshi rate-limit circuit breaker ────────────────────────────────────────
# Without authenticated keys (KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY) we get
# the IP-based unauth quota, which the previous fetcher blew through ~2400
# times/hour. The breaker tracks consecutive 429s in this poll cycle and
# stops calling Kalshi for the rest of the cycle once we've hit a wall — so
# we degrade gracefully instead of looping retries.
_KALSHI_429_LAST_HIT = 0.0
_KALSHI_BREAKER_OPEN_UNTIL = 0.0
_KALSHI_REQ_DELAY_S = 0.15  # ~6 req/s; well under documented unauth limits
_KALSHI_BREAKER_COOLDOWN_S = 120


def fetch_kalshi_markets(sport_key: str) -> list[dict]:
    """Fetch markets from Kalshi for the given sport across all its series.

    With no auth keys we throttle to ~6 req/s, honor ``Retry-After`` on 429,
    and trip a circuit breaker that suppresses Kalshi traffic for 2 min after
    a rate-limit wall is hit. Once Kalshi auth keys are wired up the breaker
    becomes a no-op (auth quotas are far higher than what we need)."""
    global _KALSHI_429_LAST_HIT, _KALSHI_BREAKER_OPEN_UNTIL

    series_list = KALSHI_SERIES.get(sport_key, [])
    if not series_list:
        return []

    # Skip the fetch entirely while breaker is open.
    if time.time() < _KALSHI_BREAKER_OPEN_UNTIL:
        return []

    session = _make_http_session()
    all_markets: list[dict] = []
    for series in series_list:
        cursor = None
        for _ in range(5):
            params: dict = {"limit": 200, "status": "open", "series_ticker": series}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = session.get(f"{KALSHI_API_HOST}/markets", params=params, timeout=30)
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", "60"))
                    _KALSHI_429_LAST_HIT = time.time()
                    _KALSHI_BREAKER_OPEN_UNTIL = time.time() + max(retry_after, _KALSHI_BREAKER_COOLDOWN_S)
                    print(
                        f"⚠ Kalshi rate-limited (429) on {series}; opening breaker for "
                        f"{int(_KALSHI_BREAKER_OPEN_UNTIL - time.time())}s. "
                        f"Set KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY for higher quotas.",
                        flush=True,
                    )
                    return all_markets  # bail out of *all* series for this sport
                resp.raise_for_status()
                body = resp.json()
            except Exception as e:
                print(f"Kalshi fetch error ({series}): {e}", flush=True)
                break

            markets = body.get("markets", [])
            if not markets:
                break
            all_markets.extend(markets)
            cursor = body.get("cursor")
            if not cursor:
                break
            time.sleep(_KALSHI_REQ_DELAY_S)
        # Stagger between series too, especially when there are many tickers
        time.sleep(_KALSHI_REQ_DELAY_S)

    return all_markets


def _kalshi_series_to_market(event_ticker: str) -> str | None:
    """Map a Kalshi event ticker prefix to a The Odds API market_key.

    Kalshi NBA props: KXNBAPTS → player_points, KXNBA3PT → player_threes,
    KXNBAAST → player_assists, KXNBAREB → player_rebounds. Extensible
    to other sports as Kalshi adds them.
    """
    et = (event_ticker or "").upper()
    if "NBAPTS" in et: return "player_points"
    if "NBA3PT" in et: return "player_threes"
    if "NBAAST" in et: return "player_assists"
    if "NBAREB" in et: return "player_rebounds"
    return None


_KALSHI_LINE_RE = re.compile(r"-T?(\d+(?:\.\d+)?)(?:-\w+)?$")


def _extract_kalshi_prop_line(ticker: str) -> float | None:
    """Pull the threshold (e.g. 26.5 or 27) off the tail of a Kalshi ticker."""
    if not ticker:
        return None
    m = _KALSHI_LINE_RE.search(ticker)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _extract_kalshi_prop_player(market: dict) -> str | None:
    """Best-effort player-name extraction. Tries yes_sub_title, then a
    couple of regex patterns over the market title."""
    sub = (market.get("yes_sub_title") or "").strip()
    if sub and sub.lower() not in ("yes", "no", ""):
        return sub
    title = (market.get("title") or "").strip()
    # Pattern: "Will <Player> score/throw/hit ... ?"
    m = re.match(
        r"(?:will\s+)?([a-z][a-z\.'\-]*(?:\s+[a-z][a-z\.'\-]*)+?)\s+"
        r"(?:score|throw|hit|record|achieve|have|get|pass|rush|catch)",
        title.lower(),
    )
    if m:
        return m.group(1).strip()
    # Pattern: "<Player> <stat>" at start of title
    m = re.match(
        r"^([a-z][a-z\.'\-]*(?:\s+[a-z][a-z\.'\-]*)+?)\s+"
        r"(points|3\-?pt|threes|assists|rebounds|td|tds|yards)",
        title.lower(),
    )
    if m:
        return m.group(1).strip()
    return None


def parse_kalshi_player_props(parsed_kalshi: list[dict]) -> list[dict]:
    """Reshape parse_kalshi_markets output into a flat list of player-prop
    rows ready for cross-venue matching. Each row is one (player, market,
    line) quote at Kalshi.

    Kalshi prop tier convention: a ticker ending in "T27" means
    "score >= 27", which mathematically equals "over 26.5" on a book.
    We expose `line_book_equivalent` = ticker_line - 0.5 so matching
    against book lines is straightforward.
    """
    out: list[dict] = []
    for ev in parsed_kalshi or []:
        if ev.get("market_type") != "props":
            continue
        market_key = _kalshi_series_to_market(ev.get("event_ticker", ""))
        if not market_key:
            continue
        for team_name, data in (ev.get("teams") or {}).items():
            ticker = data.get("ticker", "")
            line_raw = _extract_kalshi_prop_line(ticker)
            if line_raw is None:
                continue
            # Try to extract player from team_name first (Kalshi's yes_sub_title);
            # fall back to title parsing on the parent event.
            player_raw = team_name
            if not player_raw or player_raw.lower() in ("yes", "no"):
                player_raw = _extract_kalshi_prop_player({"title": ev.get("title", "")}) or ""
            player_norm = normalize_player_name(player_raw)
            if not player_norm:
                continue
            out.append({
                "player": player_raw,
                "player_norm": player_norm,
                "market": market_key,
                # Kalshi-native line: "score N or more" threshold
                "line_kalshi": line_raw,
                # Book-equivalent line: book "over N-0.5" == Kalshi "T(N)"
                "line_book_equivalent": round(line_raw - 0.5, 1),
                "yes_prob": data.get("implied_prob"),
                "yes_bid": data.get("yes_bid"),
                "yes_ask": data.get("yes_ask"),
                "volume": data.get("volume", 0),
                "ticker": ticker,
                "event_ticker": ev.get("event_ticker", ""),
                "event_title": ev.get("title", ""),
            })
    return out


def match_player_props_cross_venue(
    book_props: list[dict],
    kalshi_props: list[dict],
    poly_markets: list[dict] | None = None,
) -> list[dict]:
    """Join book player-prop rows to Kalshi (and optionally Polymarket)
    by (player_norm, market, line). Returns enriched rows with
    divergences and signal flags.

    Matching is exact on (player_norm, market, line); books use
    continuous half-integer lines so the equality holds when Kalshi's
    book-equivalent line lines up.
    """
    # Index Kalshi by (player_norm, market, line_book_equivalent) for O(1) lookup
    kalshi_idx: dict[tuple[str, str, float], dict] = {}
    for kp in kalshi_props or []:
        key = (kp["player_norm"], kp["market"], kp["line_book_equivalent"])
        # Prefer higher-volume Kalshi market if duplicates
        existing = kalshi_idx.get(key)
        if existing is None or (kp.get("volume") or 0) > (existing.get("volume") or 0):
            kalshi_idx[key] = kp

    # Same for Polymarket: filter to prop-shaped questions and key by
    # extracted (player, market, line).
    poly_idx: dict[tuple[str, str, float], dict] = {}
    for pm in poly_markets or []:
        info = _extract_poly_prop_info(pm)
        if info is None:
            continue
        key = (info["player_norm"], info["market"], info["line"])
        poly_idx[key] = {**pm, **info}

    out: list[dict] = []
    for bp in book_props:
        key = (bp["player_norm"], bp["market"], bp["line"])
        k = kalshi_idx.get(key)
        p = poly_idx.get(key)

        # Pick best book line for each side (highest implied prob = cheapest)
        best_book_over = None
        best_book_under = None
        for bk_key, bk_data in (bp.get("books") or {}).items():
            if "over_prob" in bk_data:
                if best_book_over is None or bk_data["over_prob"] > best_book_over["prob"]:
                    best_book_over = {"book": bk_key, "title": bk_data.get("title"),
                                       "prob": bk_data["over_prob"],
                                       "odds": bk_data.get("over_odds")}
            if "under_prob" in bk_data:
                if best_book_under is None or bk_data["under_prob"] > best_book_under["prob"]:
                    best_book_under = {"book": bk_key, "title": bk_data.get("title"),
                                        "prob": bk_data["under_prob"],
                                        "odds": bk_data.get("under_odds")}

        # De-vigged consensus = our "fair" probability of OVER
        fair_over = bp.get("consensus_over_devigged") or bp.get("consensus_over_pp")

        divergences: dict = {}
        if k and fair_over is not None and k.get("yes_prob") is not None:
            # Positive = Kalshi YES underprices the over (we should buy YES)
            divergences["kalshi"] = round(fair_over - float(k["yes_prob"]), 2)
        if p and fair_over is not None and p.get("yes_prob") is not None:
            divergences["polymarket"] = round(fair_over - float(p["yes_prob"]), 2)

        max_abs_div = max((abs(v) for v in divergences.values()), default=0.0)

        # Signal: any divergence exceeds threshold AND we have a sharp book quote
        is_signal = max_abs_div >= DIVERGENCE_THRESHOLD and bool(bp.get("books"))

        out.append({
            "player": bp["player"],
            "player_norm": bp["player_norm"],
            "market": bp["market"],
            "line": bp["line"],
            "event": bp.get("event", ""),
            "commence_time": bp.get("commence_time", ""),
            "consensus_over_pp": bp.get("consensus_over_pp"),
            "consensus_over_devigged": bp.get("consensus_over_devigged"),
            "consensus_under_pp": bp.get("consensus_under_pp"),
            "vig_pct": bp.get("vig_pct"),
            "books": bp.get("books"),
            "best_book_over": best_book_over,
            "best_book_under": best_book_under,
            "kalshi": {
                "yes_prob": k.get("yes_prob") if k else None,
                "ticker": k.get("ticker") if k else None,
                "line_kalshi": k.get("line_kalshi") if k else None,
                "volume": k.get("volume") if k else None,
                "trade_url": f"https://kalshi.com/markets/{k['ticker'].lower()}" if k and k.get("ticker") else None,
            } if k else None,
            "polymarket": {
                "yes_prob": p.get("yes_prob") if p else None,
                "slug": p.get("slug") if p else None,
                "trade_url": f"https://polymarket.com/market/{p['slug']}" if p and p.get("slug") else None,
            } if p else None,
            "divergences": divergences,
            "max_divergence_pp": round(max_abs_div, 2),
            "is_signal": is_signal,
        })

    # Sort: signals first, then by absolute divergence
    out.sort(key=lambda r: (-int(r["is_signal"]), -r["max_divergence_pp"]))
    return out


# Patterns that suggest a Polymarket market is a player prop
_POLY_PROP_STAT_PATTERNS = [
    (r"point", "player_points"),
    (r"3-?pt|three-?pointer|threes?", "player_threes"),
    (r"assist", "player_assists"),
    (r"rebound", "player_rebounds"),
    (r"touchdown|td", "player_anytime_td"),
    (r"passing yards?|pass yds?", "player_pass_yds"),
    (r"rushing yards?|rush yds?", "player_rush_yds"),
    (r"strikeout|k's?", "pitcher_strikeouts"),
    (r"home run|hr", "batter_home_runs"),
    (r"goal", "player_goals"),
]

_POLY_PROP_QUESTION_RE = re.compile(
    r"(?:will\s+)?([a-z][a-z\.'\-]*(?:\s+[a-z][a-z\.'\-]*)+?)\s+"
    r"(?:score|throw|hit|record|achieve|have|get|pass for)\s+"
    r"(?:at\s+least\s+|over\s+)?(\d+(?:\.\d+)?)\+?\s*"
    r"(point|3-?pt|three-?pointer|threes?|assist|rebound|touchdown|td|"
    r"passing yards?|rushing yards?|strikeout|home run|hr|goal)",
    re.IGNORECASE,
)


def _extract_poly_prop_info(pm: dict) -> dict | None:
    """Try to parse a Polymarket market dict into a player-prop tuple.
    Returns None if the question doesn't look like a prop."""
    q = (pm.get("market_question") or "").lower()
    if not q:
        return None
    m = _POLY_PROP_QUESTION_RE.search(q)
    if not m:
        return None
    player = m.group(1).strip()
    try:
        threshold = float(m.group(2))
    except ValueError:
        return None
    stat_word = m.group(3)
    market_key = None
    for pat, key in _POLY_PROP_STAT_PATTERNS:
        if re.search(pat, stat_word, re.IGNORECASE):
            market_key = key
            break
    if not market_key:
        return None
    # Polymarket "score N+" means score >= N == book "over N-0.5"
    line = round(threshold - 0.5, 1)
    # Yes price = book over_prob
    yes = (pm.get("outcomes") or {}).get("Yes") or {}
    yes_prob = yes.get("implied_prob")
    return {
        "player_norm": normalize_player_name(player),
        "market": market_key,
        "line": line,
        "yes_prob": yes_prob,
    }


def parse_kalshi_markets(raw: list[dict]) -> list[dict]:
    """Parse Kalshi markets into structured format grouped by event.

    Returns list of dicts each representing an event with team/outcome entries.
    Handles game winner markets, futures/awards, and player props.
    """
    events: dict[str, dict] = {}
    for m in raw:
        et = m.get("event_ticker", "")
        if not et:
            continue
        if et not in events:
            events[et] = {"title": m.get("title", ""), "markets": []}
        events[et]["markets"].append(m)

    parsed = []
    for et, ev_data in events.items():
        teams: dict[str, dict] = {}
        for m in ev_data["markets"]:
            ticker = m.get("ticker", "")
            parts = ticker.rsplit("-", 1)
            team_abbrev = parts[-1] if len(parts) > 1 else ""

            team_name = (m.get("yes_sub_title") or "").strip() or team_abbrev
            if not team_name:
                team_name = m.get("title", ticker)

            try:
                yes_bid = float(m.get("yes_bid_dollars") or "0")
                yes_ask = float(m.get("yes_ask_dollars") or "0")
                last_price = float(m.get("last_price_dollars") or "0")
                volume = float(m.get("volume_fp") or "0")
            except (ValueError, TypeError):
                continue

            if yes_bid > 0 and yes_ask > 0:
                mid_price = (yes_bid + yes_ask) / 2
            elif last_price > 0:
                mid_price = last_price
            else:
                continue

            teams[team_name] = {
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "mid_price": mid_price,
                "last_price": last_price,
                "implied_prob": round(mid_price * 100, 2),
                "volume": volume,
                "ticker": ticker,
                "team_abbrev": team_abbrev,
            }

        if teams:
            # Determine market type from event ticker prefix
            market_type = "game"
            et_upper = et.upper()
            if any(k in et_upper for k in ("MVP", "ROY", "DPOY", "HART", "VEZINA", "CALDER", "NORRIS", "HEISMAN", "PLAYOFF")):
                market_type = "futures"
            elif any(k in et_upper for k in ("PTS", "3PT", "AST", "REB")):
                market_type = "props"
            elif not any(k in et_upper for k in ("GAME", "MATCH", "FIGHT", "RACE")):
                # Series without GAME/MATCH suffix are typically futures (league winners)
                market_type = "futures"

            parsed.append({
                "event_ticker": et,
                "title": ev_data["title"],
                "teams": teams,
                "total_volume": sum(t["volume"] for t in teams.values()),
                "market_type": market_type,
            })

    return parsed


# ---------------------------------------------------------------------------
# Matching + comparison
# ---------------------------------------------------------------------------

# Markets we always reject — these are season-long / futures that can't be compared to match odds
REJECT_KEYWORDS = ["stanley cup", "super bowl", "world series", "mvp",
                    "finish in", "finish last", "top 4", "top 6",
                    "relegated", "promoted", "standings", "ballon d'or",
                    "win the 2025", "win the 2026", "win the 2027",
                    "winner 2025", "winner 2026", "winner 2027"]


def is_comparable_market(question: str) -> bool:
    """Check if a Polymarket question can be meaningfully compared to match-level odds.
    We accept most markets but reject obvious season-long futures."""
    q = question.lower()
    if any(kw in q for kw in REJECT_KEYWORDS):
        return False
    return True


def _parse_iso_utc(s: str) -> datetime | None:
    """Parse an ISO-8601 timestamp into a UTC-aware datetime, or None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# Time window for matching: a Polymarket market resolving more than this long
# before or after the bookmaker event's commence_time is rejected.
MATCH_WINDOW_HOURS_BEFORE = 12   # poly resolved >12h before kickoff is suspicious
MATCH_WINDOW_HOURS_AFTER = 24 * 7  # poly resolved >7d after is a different event

# Diagnostic ring buffer for near-reject matches (min(home,away) in [55, 70)).
# Surfaced via /api/diagnostics/match-rejects so we can tune the threshold.
_NEAR_REJECTS: list[dict] = []
_NEAR_REJECTS_MAX = 200


def _log_near_reject(event: dict, pm: dict, home_score: float, away_score: float, reason: str) -> None:
    """Record a near-reject for diagnostic review. Capped ring buffer."""
    if len(_NEAR_REJECTS) >= _NEAR_REJECTS_MAX:
        _NEAR_REJECTS.pop(0)
    _NEAR_REJECTS.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": f"{event.get('home_team', '')} vs {event.get('away_team', '')}",
        "commence_time": event.get("commence_time", ""),
        "poly_question": pm.get("market_question", ""),
        "poly_event_title": pm.get("event_title", ""),
        "poly_end_date": pm.get("end_date", ""),
        "home_score": round(home_score, 1),
        "away_score": round(away_score, 1),
        "reason": reason,
    })
    M_MATCH_REJECTS.labels(reason=reason).inc()


# Sharp books — these are known to be the most efficient (lowest vig,
# tightest correlation with closing line). Signals require at least one
# sharp book to be present.
SHARP_BOOK_KEYS = {"pinnacle", "betcris", "circa", "bookmakerxch", "betfair_ex_eu", "betfair_ex_uk"}

# Liquidity gate: minimum Polymarket volume and maximum spread (in price
# points, where 1.0 = 100%) for a signal to fire. The product is roughly
# the cost-to-fill: a signal isn't actionable if you'd give back the edge
# crossing the spread.
MIN_POLY_VOLUME = 1000.0      # USD
MAX_POLY_SPREAD = 0.05         # 5pp — below this we treat it as quotable
SHARP_CONSENSUS_TOLERANCE = 2.0  # pp; sharps must agree within this band


def _build_trade_urls(poly_slug: str | None, kalshi_event_ticker: str | None = None,
                      kalshi_market_ticker: str | None = None) -> dict:
    """Build deep-link URLs so the user can place orders on each venue.

    Polymarket pattern: polymarket.com/market/<slug>
    Kalshi pattern:     kalshi.com/markets/<ticker> (specific market) or
                        kalshi.com/events/<event_ticker> (groups all sub-markets)
    """
    return {
        "trade_poly_url": f"https://polymarket.com/market/{poly_slug}" if poly_slug else None,
        "trade_kalshi_url": (
            f"https://kalshi.com/markets/{kalshi_market_ticker.lower()}" if kalshi_market_ticker
            else f"https://kalshi.com/events/{kalshi_event_ticker.lower()}" if kalshi_event_ticker
            else None
        ),
    }


def _signal_quality(event: dict, outcome_name: str, odds_prob: float,
                    poly_prob: float, poly_market: dict) -> dict:
    """Compute signal-quality flags for one outcome of a matched comparison.

    Returns a dict with the de-vigged divergence, vig pct, and four boolean
    gates (sharp_consensus_ok, liquidity_ok, not_stale, plus the combined
    passes_all_gates). The matcher uses passes_all_gates to decide whether
    to flag the signal — raw `divergence` and `is_signal` stay populated
    for the legacy frontend.
    """
    consensus = event.get("consensus_probs") or {}
    total = sum(consensus.values()) if consensus else 100.0
    vig_pct = round(total - 100.0, 2) if consensus else 0.0
    devigged_pct = (odds_prob / total) * 100.0 if total > 0 else odds_prob
    devigged_div = round(devigged_pct - poly_prob, 2)

    # Sharp-book consensus: which sharp books cover this outcome and do
    # their probs agree within tolerance?
    sharp_probs: list[tuple[str, float]] = []
    for bk_key, bk_data in (event.get("bookmakers") or {}).items():
        # Strip the "_h2h" / "_spreads" / "_totals" suffix added in parse_odds_events
        bare = bk_key.rsplit("_", 1)[0] if "_" in bk_key else bk_key
        if bare not in SHARP_BOOK_KEYS:
            continue
        oc = (bk_data.get("outcomes") or {}).get(outcome_name)
        if oc and oc.get("implied_prob") is not None:
            sharp_probs.append((bare, float(oc["implied_prob"])))

    sharp_consensus_ok = bool(sharp_probs)
    if len(sharp_probs) >= 2:
        probs_only = [p for _, p in sharp_probs]
        spread = max(probs_only) - min(probs_only)
        sharp_consensus_ok = spread <= SHARP_CONSENSUS_TOLERANCE

    # Liquidity gate
    pm_volume = float(poly_market.get("volume") or 0.0)
    pm_spread = float(poly_market.get("spread") or 0.0)
    liquidity_ok = (pm_volume >= MIN_POLY_VOLUME) and (pm_spread <= MAX_POLY_SPREAD)

    # Stale-data gate: a market that's never traded or has been completely
    # flat for a week is almost certainly mispriced because nobody's there
    # — not because we found a real edge.
    last_trade = float(poly_market.get("last_trade_price") or 0.0)
    one_day = float(poly_market.get("one_day_change") or 0.0)
    one_week = float(poly_market.get("one_week_change") or 0.0)
    not_stale = (last_trade > 0) and (pm_volume > 0) and not (one_day == 0 and one_week == 0)

    return {
        "divergence_raw": round(odds_prob - poly_prob, 2),
        "divergence_devigged": devigged_div,
        "vig_pct": vig_pct,
        "sharp_books_present": [k for k, _ in sharp_probs],
        "sharp_consensus_ok": sharp_consensus_ok,
        "liquidity_ok": liquidity_ok,
        "not_stale": not_stale,
        "passes_all_gates": sharp_consensus_ok and liquidity_ok and not_stale,
    }


def match_and_compare(odds_events: list[dict], poly_markets: list[dict], kalshi_markets: list[dict] | None = None) -> list[dict]:
    """
    Fuzzy-match odds events to Polymarket markets and compute divergence.
    Only matches actual game-level markets, not season-long futures.
    Returns list of comparison dicts.
    """
    comparisons = []
    used_poly_ids = set()  # prevent same Polymarket market matching multiple events

    for event in odds_events:
        home = normalize_name(event["home_team"])
        away = normalize_name(event["away_team"])
        ev_commence = _parse_iso_utc(event.get("commence_time", ""))

        best_match = None
        best_score = 0

        for pm in poly_markets:
            # Skip already-matched markets
            pm_id = pm.get("event_id", "") + pm.get("market_question", "")
            if pm_id in used_poly_ids:
                continue

            # Skip obvious season-long futures
            if not is_comparable_market(pm["market_question"]):
                continue

            # Skip zero-volume markets (unreliable prices)
            if pm["volume"] <= 0 and pm["liquidity"] <= 0:
                continue

            # Time-window check: reject if the Polymarket market resolves
            # too far from the bookmaker event's commence_time. Skips when
            # either timestamp is missing (Polymarket's end_date is sometimes
            # unset for game-level markets — fall back to fuzzy matching only).
            pm_end = _parse_iso_utc(pm.get("end_date", ""))
            if ev_commence and pm_end:
                delta_h = (pm_end - ev_commence).total_seconds() / 3600.0
                if delta_h < -MATCH_WINDOW_HOURS_BEFORE or delta_h > MATCH_WINDOW_HOURS_AFTER:
                    continue

            q = pm["market_question"].lower()
            title = pm["event_title"].lower()
            text = f"{q} {title}"

            # BOTH team names must score well (not just one)
            home_score = fuzz.partial_ratio(home, text)
            away_score = fuzz.partial_ratio(away, text)

            # Require both teams to be present (min 70 each).
            # Log near-rejects (55-70) so we can tune aliases / threshold.
            if home_score < 70 or away_score < 70:
                if min(home_score, away_score) >= 55:
                    _log_near_reject(event, pm, home_score, away_score, "team_score_below_70")
                continue

            combined = (home_score + away_score) / 2

            if combined > best_score:
                best_score = combined
                best_match = pm

        if best_score < 75 or best_match is None:
            continue

        # Mark this Polymarket market as used
        pm_id = best_match.get("event_id", "") + best_match.get("market_question", "")
        used_poly_ids.add(pm_id)

        # Try to find matching Kalshi market
        kalshi_match = None
        if kalshi_markets:
            best_kalshi_score = 0
            for km in kalshi_markets:
                km_text = km["title"].lower()
                for tname in km["teams"]:
                    km_text += " " + tname.lower()
                h_score = fuzz.partial_ratio(home, km_text)
                a_score = fuzz.partial_ratio(away, km_text)
                if h_score >= 70 and a_score >= 70:
                    combined_k = (h_score + a_score) / 2
                    if combined_k > best_kalshi_score:
                        best_kalshi_score = combined_k
                        kalshi_match = km

        # Now compare outcomes
        outcome_comparisons = []
        for outcome_name, odds_data in event["sharp_outcomes"].items():
            odds_prob = odds_data["implied_prob"]
            consensus_prob = event["consensus_probs"].get(outcome_name, odds_prob)

            # Find matching Polymarket outcome
            poly_prob = None
            poly_outcome_key = None
            poly_token_id = None
            norm_outcome = normalize_name(outcome_name)

            for pk, pv in best_match["outcomes"].items():
                norm_pk = normalize_name(pk)
                score = fuzz.ratio(norm_outcome, norm_pk)
                if score > 75 or (len(norm_outcome) > 3 and norm_outcome in norm_pk) or (len(norm_pk) > 3 and norm_pk in norm_outcome):
                    poly_prob = pv["implied_prob"]
                    poly_outcome_key = pk
                    poly_token_id = pv.get("token_id") or None
                    break

            # Binary Yes/No: check if outcome name is in the question
            if poly_prob is None and len(best_match["outcomes"]) == 2:
                yes_data = best_match["outcomes"].get("Yes")
                if yes_data and outcome_name.lower() in best_match["market_question"].lower():
                    poly_prob = yes_data["implied_prob"]
                    poly_outcome_key = "Yes"
                    poly_token_id = yes_data.get("token_id") or None

            if poly_prob is None:
                continue

            # Skip if Polymarket price is 0 or 100 (illiquid/stale)
            if poly_prob <= 0.5 or poly_prob >= 99.5:
                continue

            # Compute signal-quality flags (vig-adjustment, sharp consensus,
            # liquidity, staleness). We use the DE-VIGGED divergence as the
            # primary signal driver and gate on all four quality flags.
            quality = _signal_quality(event, outcome_name, odds_prob, poly_prob, best_match)
            divergence_raw = quality["divergence_raw"]
            divergence = quality["divergence_devigged"]
            abs_div = abs(divergence)

            # Half-Kelly criterion uses the de-vigged true prob.
            total_prob_raw = sum(event.get("consensus_probs", {}).values()) or 100.0
            p = (odds_prob / total_prob_raw) if total_prob_raw > 0 else (odds_prob / 100)
            q = 1 - p
            if poly_prob > 0:
                b = (100 / poly_prob) - 1  # net decimal odds on Polymarket
                if b > 0:
                    full_kelly = (b * p - q) / b
                    half_kelly = full_kelly / 2
                    kelly_pct = max(0, round(half_kelly * 100, 2))
                else:
                    kelly_pct = 0
            else:
                kelly_pct = 0

            # Find matching Kalshi price
            kalshi_prob = None
            kalshi_ticker = None
            if kalshi_match:
                norm_out = normalize_name(outcome_name)
                for kteam, kdata in kalshi_match["teams"].items():
                    if fuzz.partial_ratio(norm_out, normalize_name(kteam)) > 75:
                        kalshi_prob = kdata["implied_prob"]
                        kalshi_ticker = kdata["ticker"]
                        break

            kalshi_divergence = round(odds_prob - kalshi_prob, 2) if kalshi_prob is not None else None

            # Signal fires when devigged divergence clears threshold AND
            # all four quality gates pass. Raw fields preserved for the UI.
            is_signal = (
                abs_div >= DIVERGENCE_THRESHOLD
                and quality["passes_all_gates"]
            )

            outcome_comparisons.append({
                "outcome": outcome_name,
                "outcome_name": outcome_name,  # alias for frontend
                "poly_outcome": poly_outcome_key,
                "poly_token_id": poly_token_id,
                "sharp_prob": odds_prob,
                "consensus_prob": consensus_prob,
                "poly_prob": poly_prob,
                "poly_price": poly_prob / 100 if poly_prob else 0,  # 0-1 scale for frontend
                "kalshi_prob": kalshi_prob,
                "kalshi_ticker": kalshi_ticker,
                "kalshi_divergence": kalshi_divergence,
                "divergence": divergence,                # de-vigged (used for is_signal)
                "divergence_raw": divergence_raw,        # raw, with vig — for transparency
                "divergence_pct": divergence,            # alias for frontend
                "abs_divergence": round(abs_div, 2),
                "cheap_on": "Polymarket" if divergence > 0 else "Bookmaker",
                "kelly_pct": kelly_pct,
                "kelly_fraction": kelly_pct / 100 if kelly_pct else 0,  # 0-1 scale for frontend
                "is_signal": is_signal,
                # Signal quality breakdown (lets the UI explain why it did/didn't fire)
                "vig_pct": quality["vig_pct"],
                "sharp_books_present": quality["sharp_books_present"],
                "sharp_consensus_ok": quality["sharp_consensus_ok"],
                "liquidity_ok": quality["liquidity_ok"],
                "not_stale": quality["not_stale"],
            })

        if not outcome_comparisons:
            continue

        # Compute time until event
        try:
            commence = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = commence - now
            if delta.total_seconds() < 0:
                time_str = "LIVE / Started"
            elif delta.days > 0:
                time_str = f"{delta.days}d {delta.seconds // 3600}h"
            else:
                hours = delta.seconds // 3600
                mins = (delta.seconds % 3600) // 60
                time_str = f"{hours}h {mins}m"
        except Exception:
            time_str = "Unknown"

        # Build per-bookmaker breakdown for this event's outcomes
        bookmaker_breakdown = {}
        for bk_key, bk_data in event["bookmakers"].items():
            bookmaker_breakdown[bk_key] = {
                "title": bk_data["title"],
                "outcomes": {name: d["implied_prob"] for name, d in bk_data["outcomes"].items()},
            }

        # --- New enriched data points ---
        # Find the best outcome (highest divergence)
        best_oc = max(outcome_comparisons, key=lambda o: o["abs_divergence"])
        best_outcome_name = best_oc["outcome"]

        # Collect bookmaker probs for the best outcome
        bk_probs_for_best = []
        for bk_key, bk_data in event["bookmakers"].items():
            for oname, odata in bk_data["outcomes"].items():
                if normalize_name(oname) == normalize_name(best_outcome_name):
                    bk_probs_for_best.append(odata["implied_prob"])

        if bk_probs_for_best:
            highest_book_prob = round(max(bk_probs_for_best), 2)
            lowest_book_prob = round(min(bk_probs_for_best), 2)
            book_range = round(highest_book_prob - lowest_book_prob, 2)
            median_book_prob = round(statistics.median(bk_probs_for_best), 2)
            book_std_dev = round(statistics.stdev(bk_probs_for_best), 2) if len(bk_probs_for_best) > 1 else 0.0
        else:
            highest_book_prob = best_oc["sharp_prob"]
            lowest_book_prob = best_oc["sharp_prob"]
            book_range = 0.0
            median_book_prob = best_oc["sharp_prob"]
            book_std_dev = 0.0

        # book_agreement: 1-5 (low std = high agreement)
        if book_std_dev <= 1:
            book_agreement = 5
        elif book_std_dev <= 3:
            book_agreement = 4
        elif book_std_dev <= 5:
            book_agreement = 3
        elif book_std_dev <= 8:
            book_agreement = 2
        else:
            book_agreement = 1

        # Implied vig (overround)
        all_consensus = list(event["consensus_probs"].values())
        implied_vig = round(sum(all_consensus) - 100, 2) if all_consensus else 0.0

        # True prob no vig
        total_prob = sum(all_consensus) if all_consensus else 100
        true_prob_no_vig = round(best_oc["sharp_prob"] / total_prob * 100, 2) if total_prob > 0 else best_oc["sharp_prob"]

        # Best/worst decimal odds for top outcome
        best_decimal_odds = round(100 / lowest_book_prob, 2) if lowest_book_prob > 0 else 0
        worst_decimal_odds = round(100 / highest_book_prob, 2) if highest_book_prob > 0 else 0

        # Volume/liquidity ratio
        poly_liq = best_match["liquidity"]
        volume_liquidity_ratio = round(best_match["volume"] / max(poly_liq, 1), 2)

        # Spread percentage
        poly_price = best_oc["poly_prob"] / 100 if best_oc["poly_prob"] > 0 else 1
        spread_pct = round((best_match["spread"] / poly_price) * 100, 2) if poly_price > 0 else 0.0

        # Edge direction
        edge_direction = "BUY" if best_oc["divergence"] > 0 else "SELL"

        # Time to event in hours
        try:
            commence = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
            time_to_event_hours = round((commence - datetime.now(timezone.utc)).total_seconds() / 3600, 1)
            if time_to_event_hours < 0:
                time_to_event_hours = 0
        except Exception:
            time_to_event_hours = None

        # Confidence score (1-5 stars)
        max_div = best_oc["abs_divergence"]
        edge_score = min(max_div / 20, 1.0) * 5  # 20% divergence = max
        agree_score = book_agreement
        vol_score = min(best_match["volume"] / 100000, 1.0) * 5
        spread_score = max(0, 5 - (best_match["spread"] * 50))  # tighter spread = higher
        m_score = min(best_score / 100, 1.0) * 5
        confidence_score = round(
            edge_score * 0.3 + agree_score * 0.25 + vol_score * 0.2 + spread_score * 0.15 + m_score * 0.1,
            1
        )
        confidence_score = max(1.0, min(5.0, confidence_score))

        comparisons.append({
            "home_team": event["home_team"],
            "away_team": event["away_team"],
            "commence_time": event["commence_time"],
            "time_until": time_str,
            "num_bookmakers": event["num_bookmakers"],
            "sharp_book": event["sharp_book"],
            "bookmaker_breakdown": bookmaker_breakdown,
            "poly_question": best_match["market_question"],
            "poly_slug": best_match["slug"],
            "condition_id": best_match.get("condition_id", ""),
            "poly_volume": best_match["volume"],
            "poly_liquidity": best_match["liquidity"],
            "poly_spread": best_match["spread"],
            "poly_one_day_change": best_match["one_day_change"],
            "match_score": round(best_score, 1),
            "outcomes": outcome_comparisons,
            "has_signal": any(o["is_signal"] for o in outcome_comparisons),
            "max_divergence": max(o["abs_divergence"] for o in outcome_comparisons),
            # New data points
            "highest_book_prob": highest_book_prob,
            "lowest_book_prob": lowest_book_prob,
            "book_range": book_range,
            "median_book_prob": median_book_prob,
            "book_std_dev": book_std_dev,
            "book_agreement": book_agreement,
            "implied_vig": implied_vig,
            "true_prob_no_vig": true_prob_no_vig,
            "best_decimal_odds": best_decimal_odds,
            "worst_decimal_odds": worst_decimal_odds,
            "volume_liquidity_ratio": volume_liquidity_ratio,
            "spread_pct": spread_pct,
            "edge_direction": edge_direction,
            "time_to_event_hours": time_to_event_hours,
            "confidence_score": confidence_score,
            # Kalshi data
            "kalshi_event": kalshi_match["event_ticker"] if kalshi_match else None,
            "kalshi_volume": kalshi_match["total_volume"] if kalshi_match else 0,
            **_build_trade_urls(
                best_match["slug"],
                kalshi_match["event_ticker"] if kalshi_match else None,
            ),
        })

    # Sort: signals first, then by max divergence
    comparisons.sort(key=lambda x: (-x["has_signal"], -x["max_divergence"]))
    return comparisons


def compare_outrights(outright_odds: dict, poly_markets: list[dict]) -> list[dict]:
    """
    Compare outright/futures odds with Polymarket futures markets.
    E.g., "Will Liverpool win the EPL?" on Polymarket vs bookmaker outright odds.
    """
    comparisons = []
    used_poly = set()

    for team, book_data in outright_odds.items():
        norm_team = normalize_name(team)
        sharp_prob = book_data["sharp_prob"]
        consensus_prob = book_data["consensus_prob"]

        best_match = None
        best_score = 0

        for pm in poly_markets:
            pm_id = pm.get("event_id", "") + pm.get("market_question", "")
            if pm_id in used_poly:
                continue

            # Skip zero-volume
            if pm["volume"] <= 0 and pm["liquidity"] <= 0:
                continue

            q = pm["market_question"].lower()
            title = pm["event_title"].lower()
            text = f"{q} {title}"

            score = fuzz.partial_ratio(norm_team, text)
            if score > best_score and score >= 75:
                best_score = score
                best_match = pm

        if best_match is None:
            continue

        pm_id = best_match.get("event_id", "") + best_match.get("market_question", "")
        used_poly.add(pm_id)

        # Get "Yes" price from binary market
        yes_data = best_match["outcomes"].get("Yes")
        if not yes_data:
            # Try first outcome
            first_key = next(iter(best_match["outcomes"]), None)
            if first_key:
                yes_data = best_match["outcomes"][first_key]
        if not yes_data:
            continue

        poly_prob = yes_data["implied_prob"]
        if poly_prob <= 0.5 or poly_prob >= 99.5:
            continue

        divergence = sharp_prob - poly_prob
        abs_div = abs(divergence)

        # Half-Kelly
        p = sharp_prob / 100
        q = 1 - p
        b = (100 / poly_prob) - 1
        if b > 0:
            half_kelly = max(0, (b * p - q) / (2 * b))
            kelly_pct = round(half_kelly * 100, 2)
        else:
            kelly_pct = 0

        outcome = {
            "outcome": team,
            "outcome_name": team,
            "poly_outcome": "Yes",
            "sharp_prob": sharp_prob,
            "consensus_prob": consensus_prob,
            "poly_prob": poly_prob,
            "poly_price": poly_prob / 100 if poly_prob else 0,
            "kalshi_prob": None,
            "kalshi_ticker": None,
            "kalshi_divergence": None,
            "divergence": round(divergence, 2),
            "divergence_pct": round(divergence, 2),
            "abs_divergence": round(abs_div, 2),
            "cheap_on": "Polymarket" if divergence > 0 else "Bookmaker",
            "kelly_pct": kelly_pct,
            "kelly_fraction": kelly_pct / 100 if kelly_pct else 0,
            "is_signal": abs_div >= DIVERGENCE_THRESHOLD,
        }

        # Build per-bookmaker breakdown for this team
        bookmaker_breakdown = {}
        for bk_key, bk_data_item in book_data.get("bookmakers", {}).items():
            bookmaker_breakdown[bk_key] = {
                "title": bk_data_item["title"],
                "outcomes": {team: bk_data_item["implied_prob"]},
            }

        # --- New enriched data points for outrights ---
        bk_probs_out = [b["implied_prob"] for b in book_data.get("bookmakers", {}).values()]
        if bk_probs_out:
            highest_book_prob_o = round(max(bk_probs_out), 2)
            lowest_book_prob_o = round(min(bk_probs_out), 2)
            book_range_o = round(highest_book_prob_o - lowest_book_prob_o, 2)
            median_book_prob_o = round(statistics.median(bk_probs_out), 2)
            book_std_dev_o = round(statistics.stdev(bk_probs_out), 2) if len(bk_probs_out) > 1 else 0.0
        else:
            highest_book_prob_o = sharp_prob
            lowest_book_prob_o = sharp_prob
            book_range_o = 0.0
            median_book_prob_o = sharp_prob
            book_std_dev_o = 0.0

        if book_std_dev_o <= 1:
            book_agreement_o = 5
        elif book_std_dev_o <= 3:
            book_agreement_o = 4
        elif book_std_dev_o <= 5:
            book_agreement_o = 3
        elif book_std_dev_o <= 8:
            book_agreement_o = 2
        else:
            book_agreement_o = 1

        implied_vig_o = 0.0  # Not meaningful for outrights with single team
        true_prob_no_vig_o = sharp_prob
        best_decimal_odds_o = round(100 / lowest_book_prob_o, 2) if lowest_book_prob_o > 0 else 0
        worst_decimal_odds_o = round(100 / highest_book_prob_o, 2) if highest_book_prob_o > 0 else 0
        poly_liq_o = best_match["liquidity"]
        volume_liquidity_ratio_o = round(best_match["volume"] / max(poly_liq_o, 1), 2)
        poly_price_o = poly_prob / 100 if poly_prob > 0 else 1
        spread_pct_o = round((best_match["spread"] / poly_price_o) * 100, 2) if poly_price_o > 0 else 0.0
        edge_direction_o = "BUY" if divergence > 0 else "SELL"
        time_to_event_hours_o = None  # Futures don't have specific start times

        edge_score_o = min(abs_div / 20, 1.0) * 5
        vol_score_o = min(best_match["volume"] / 100000, 1.0) * 5
        spread_score_o = max(0, 5 - (best_match["spread"] * 50))
        m_score_o = min(best_score / 100, 1.0) * 5
        confidence_score_o = round(
            edge_score_o * 0.3 + book_agreement_o * 0.25 + vol_score_o * 0.2 + spread_score_o * 0.15 + m_score_o * 0.1,
            1
        )
        confidence_score_o = max(1.0, min(5.0, confidence_score_o))

        comparisons.append({
            "home_team": team,
            "away_team": "",
            "commence_time": best_match.get("end_date", ""),
            "time_until": "Futures",
            "num_bookmakers": book_data["num_bookmakers"],
            "sharp_book": "pinnacle" if book_data["sharp_prob"] != book_data["consensus_prob"] else "consensus",
            "bookmaker_breakdown": bookmaker_breakdown,
            "poly_question": best_match["market_question"],
            "poly_slug": best_match["slug"],
            "condition_id": best_match.get("condition_id", ""),
            "poly_volume": best_match["volume"],
            "poly_liquidity": best_match["liquidity"],
            "poly_spread": best_match["spread"],
            "poly_one_day_change": best_match["one_day_change"],
            "match_score": round(best_score, 1),
            "outcomes": [outcome],
            "has_signal": outcome["is_signal"],
            "max_divergence": round(abs_div, 2),
            "is_futures": True,
            # New data points
            "highest_book_prob": highest_book_prob_o,
            "lowest_book_prob": lowest_book_prob_o,
            "book_range": book_range_o,
            "median_book_prob": median_book_prob_o,
            "book_std_dev": book_std_dev_o,
            "book_agreement": book_agreement_o,
            "implied_vig": implied_vig_o,
            "true_prob_no_vig": true_prob_no_vig_o,
            "best_decimal_odds": best_decimal_odds_o,
            "worst_decimal_odds": worst_decimal_odds_o,
            "volume_liquidity_ratio": volume_liquidity_ratio_o,
            "spread_pct": spread_pct_o,
            "edge_direction": edge_direction_o,
            "time_to_event_hours": time_to_event_hours_o,
            "confidence_score": confidence_score_o,
            "kalshi_event": None,
            "kalshi_volume": 0,
            **_build_trade_urls(best_match["slug"]),
        })

    comparisons.sort(key=lambda x: (-x["has_signal"], -x["max_divergence"]))
    return comparisons


# ---------------------------------------------------------------------------
# Background data updater
# ---------------------------------------------------------------------------

def _save_edge_history(sport: str, comparisons: list[dict]):
    """Save edge signals to sports_edge_history table, deduplicating within 24h."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with _get_db() as conn:
        for comp in comparisons:
            for oc in comp.get("outcomes", []):
                if oc.get("abs_divergence", 0) < 1:
                    continue  # Only track meaningful edges
                home = comp.get("home_team", "")
                outcome = oc.get("outcome", "")
                # Check for duplicate within 24h
                existing = conn.execute(
                    "SELECT id FROM sports_edge_history WHERE sport = ? AND home_team = ? AND outcome = ? AND detected_at >= ? LIMIT 1",
                    (sport, home, outcome, cutoff),
                ).fetchone()
                if existing:
                    continue
                conn.execute(
                    "INSERT INTO sports_edge_history (sport, home_team, away_team, outcome, sharp_prob, poly_prob, divergence, kelly_pct, confidence_score, commence_time, event_id, market_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (sport, home, comp.get("away_team", ""), outcome,
                     oc.get("sharp_prob"), oc.get("poly_prob"), oc.get("divergence"),
                     oc.get("kelly_pct"), comp.get("confidence_score"),
                     comp.get("commence_time", ""), comp.get("event_id", ""),
                     comp.get("market_type", "h2h")),
                )


def _save_market_snapshots(sport: str, comparisons: list[dict]):
    """Save a snapshot of current market data for historical tracking."""
    rows = []
    for comp in comparisons:
        event_name = comp.get("home_team", "") + (" vs " + comp.get("away_team", "") if comp.get("away_team") else "")
        for oc in comp.get("outcomes", []):
            rows.append((
                sport,
                event_name,
                oc.get("outcome", ""),
                oc.get("sharp_prob"),
                oc.get("poly_prob"),
                oc.get("kalshi_prob"),
                oc.get("divergence"),
                comp.get("poly_volume"),
                comp.get("kalshi_volume"),
            ))
    if rows:
        with _get_db() as conn:
            conn.executemany(
                "INSERT INTO sports_market_snapshots (sport, event_name, outcome, book_prob, poly_prob, kalshi_prob, divergence, poly_volume, kalshi_volume) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )


# ---------------------------------------------------------------------------
# Track record — CLV, P&L simulation, calibration
# ---------------------------------------------------------------------------
#
# These three computations turn the dashboard from "trust me bro" into a
# verifiable product. They run against `sports_edge_history` (signals) and
# `sports_market_snapshots` (line movement) — both already populated by the
# main poll loop.
#
# - CLV (closing line value): for each signal, find the latest poly_prob
#   snapshot before commence_time and compute the move in our betting
#   direction. Positive = the market moved toward our prediction = we got
#   the better number when we bet.
# - P&L simulation: replay every resolved signal at threshold T with stake S,
#   compute total profit, win rate, Sharpe, max drawdown.
# - Calibration: bin signals by predicted divergence, compare empirical
#   win rate to expected. A well-calibrated dashboard sits on the diagonal.

def _compute_clv(sport: str | None = None, days: int = 30) -> dict:
    """Compute closing line value across all signals in the window.

    For each `sports_edge_history` row, find the most recent snapshot in
    `sports_market_snapshots` taken before `commence_time` (or before
    detected_at + 24h if commence_time is missing) for the same event +
    outcome. CLV in pp = (poly_prob_close - poly_prob_signal) * direction,
    where direction = +1 if we bet YES on Polymarket (divergence > 0),
    -1 otherwise.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _get_db() as conn:
        if sport:
            rows = conn.execute(
                """SELECT id, sport, home_team, away_team, outcome, sharp_prob, poly_prob,
                          divergence, commence_time, detected_at
                   FROM sports_edge_history
                   WHERE detected_at >= ? AND sport = ?""",
                (cutoff, sport),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, sport, home_team, away_team, outcome, sharp_prob, poly_prob,
                          divergence, commence_time, detected_at
                   FROM sports_edge_history
                   WHERE detected_at >= ?""",
                (cutoff,),
            ).fetchall()

        clv_values: list[float] = []
        per_sport: dict[str, list[float]] = {}
        for r in rows:
            event_name = (r["home_team"] or "") + (" vs " + r["away_team"] if r["away_team"] else "")
            close_cutoff = r["commence_time"] or r["detected_at"]
            if not close_cutoff:
                continue
            snap = conn.execute(
                """SELECT poly_prob FROM sports_market_snapshots
                   WHERE sport = ? AND event_name = ? AND outcome = ?
                         AND snapshot_at <= ? AND snapshot_at >= ?
                   ORDER BY snapshot_at DESC LIMIT 1""",
                (r["sport"], event_name, r["outcome"], close_cutoff, r["detected_at"]),
            ).fetchone()
            if not snap or snap["poly_prob"] is None or r["poly_prob"] is None:
                continue
            direction = 1.0 if (r["divergence"] or 0) > 0 else -1.0
            clv_pp = (float(snap["poly_prob"]) - float(r["poly_prob"])) * direction
            clv_values.append(clv_pp)
            per_sport.setdefault(r["sport"] or "unknown", []).append(clv_pp)

    def _summary(values: list[float]) -> dict:
        if not values:
            return {"n": 0, "mean": 0.0, "median": 0.0, "positive_rate": 0.0}
        sorted_vs = sorted(values)
        median = sorted_vs[len(sorted_vs) // 2]
        return {
            "n": len(values),
            "mean": round(sum(values) / len(values), 3),
            "median": round(median, 3),
            "positive_rate": round(sum(1 for v in values if v > 0) / len(values), 3),
        }

    return {
        "window_days": days,
        "sport": sport,
        "overall": _summary(clv_values),
        "per_sport": {s: _summary(vs) for s, vs in per_sport.items()},
    }


def _compute_pnl_simulation(
    sport: str | None = None,
    days: int = 90,
    threshold_pp: float = 5.0,
    stake: float = 100.0,
) -> dict:
    """Replay all resolved signals and simulate fixed-stake betting.

    Profit on a winning $stake bet at Polymarket = stake * (100/poly_prob - 1)
    because Polymarket prices map directly to implied probability and the
    payout multiplier is 1/price. Losses are -$stake.

    Returns total PnL, win rate, Sharpe (per-bet, sqrt(N) annualization),
    max drawdown, and the per-bet equity curve.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    params: list = [cutoff, threshold_pp]
    sql = """SELECT detected_at, sport, divergence, poly_prob, resolution
             FROM sports_edge_history
             WHERE detected_at >= ?
               AND ABS(divergence) >= ?
               AND resolved = 1
               AND resolution IN ('correct', 'incorrect')"""
    if sport:
        sql += " AND sport = ?"
        params.append(sport)
    sql += " ORDER BY detected_at ASC"

    with _get_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    bets: list[float] = []
    for r in rows:
        poly = float(r["poly_prob"] or 0)
        if poly <= 0 or poly >= 100:
            continue
        if r["resolution"] == "correct":
            profit = stake * (100.0 / poly - 1.0)
        else:
            profit = -stake
        bets.append(profit)

    n = len(bets)
    if n == 0:
        return {
            "window_days": days, "threshold_pp": threshold_pp, "stake": stake,
            "n_bets": 0, "total_pnl": 0.0, "win_rate": 0.0, "roi_pct": 0.0,
            "sharpe": 0.0, "max_drawdown": 0.0, "equity_curve": [],
        }

    total_pnl = sum(bets)
    wins = sum(1 for b in bets if b > 0)
    win_rate = wins / n

    # Equity curve + max drawdown
    equity = []
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for b in bets:
        running += b
        peak = max(peak, running)
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
        equity.append(round(running, 2))

    # Per-bet Sharpe (no risk-free rate, since stakes are tiny vs portfolio).
    if n > 1:
        mean = total_pnl / n
        var = sum((b - mean) ** 2 for b in bets) / (n - 1)
        std = math.sqrt(var) if var > 0 else 0.0
        sharpe = (mean / std) * math.sqrt(n) if std > 0 else 0.0
    else:
        sharpe = 0.0

    return {
        "window_days": days,
        "threshold_pp": threshold_pp,
        "stake": stake,
        "sport": sport,
        "n_bets": n,
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate, 4),
        "roi_pct": round((total_pnl / (n * stake)) * 100, 3) if n > 0 else 0.0,
        "sharpe": round(sharpe, 3),
        "max_drawdown": round(max_dd, 2),
        "equity_curve": equity,
    }


def _compute_calibration(sport: str | None = None, days: int = 180) -> dict:
    """Bin resolved signals by predicted divergence and report empirical win rate.

    Bins: [1,5), [5,10), [10,15), [15,20), [20,inf). For each bin we report
    the count, win rate, and the implied "expected" prob for a perfectly
    calibrated model (= mean sharp_prob in the bin / 100, capped to [0,1]).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    params: list = [cutoff]
    sql = """SELECT divergence, sharp_prob, resolution
             FROM sports_edge_history
             WHERE detected_at >= ?
               AND resolved = 1
               AND resolution IN ('correct', 'incorrect')"""
    if sport:
        sql += " AND sport = ?"
        params.append(sport)
    with _get_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    bins = [(1, 5), (5, 10), (10, 15), (15, 20), (20, 1000)]
    out: list[dict] = []
    for lo, hi in bins:
        bucket = [r for r in rows if lo <= abs(r["divergence"] or 0) < hi]
        n = len(bucket)
        if n == 0:
            out.append({
                "lo": lo, "hi": hi if hi < 1000 else None, "n": 0,
                "win_rate": None, "expected": None,
            })
            continue
        wins = sum(1 for r in bucket if r["resolution"] == "correct")
        # Expected = mean of (sharp_prob / 100) — what our model says the
        # win prob should be, on average, for signals in this bucket.
        sharp_probs = [(r["sharp_prob"] or 0) / 100.0 for r in bucket]
        expected = sum(sharp_probs) / n if sharp_probs else 0.0
        out.append({
            "lo": lo, "hi": hi if hi < 1000 else None, "n": n,
            "win_rate": round(wins / n, 4),
            "expected": round(max(0.0, min(1.0, expected)), 4),
        })
    return {"window_days": days, "sport": sport, "bins": out}


# ---------------------------------------------------------------------------
# Backtest replay — apply an arbitrary alert rule to historical signals
# ---------------------------------------------------------------------------
#
# _compute_pnl_simulation only supports a flat divergence threshold. The
# backtest replay endpoint accepts the same rule shape used by
# /api/alert-rules (sports allowlist, market_type allowlist, min_volume,
# max_time_to_event, quality-gate flags) and replays it across resolved
# edge_history. Pros use this to test new rules before turning them on.

def _signal_from_edge_row(row: dict) -> tuple[str, dict]:
    """Reshape a sports_edge_history row into the comparison-shaped dict
    that _signal_matches_rule expects.

    Some fields aren't stored on edge_history (poly_volume, time-to-event,
    per-gate flags), so we synthesize neutral values: poly_volume=0 means
    rules with a min_volume filter will reject the row (caller's choice),
    and the quality gates default to True since rows in edge_history
    already passed the live gates at signal time.
    """
    sport = row.get("sport") or ""
    outcome = {
        "outcome_name": row.get("outcome", ""),
        "divergence_pct": row.get("divergence", 0),
        "is_signal": True,
        "sharp_consensus_ok": True,
        "not_stale": True,
        "liquidity_ok": True,
    }
    # Compute time-to-event from commence_time at the moment the signal
    # fired (detected_at) — closer to what the live rule sees.
    tth = None
    commence = _parse_iso_utc(row.get("commence_time") or "")
    detected = _parse_iso_utc(row.get("detected_at") or "")
    if commence and detected:
        delta = (commence - detected).total_seconds() / 3600.0
        tth = max(0.0, delta)
    return sport, {
        "home_team": row.get("home_team", ""),
        "away_team": row.get("away_team", ""),
        "market_type": row.get("market_type", "h2h"),
        "max_divergence": abs(float(row.get("divergence") or 0)),
        "poly_volume": 0,
        "time_to_event_hours": tth,
        "outcomes": [outcome],
    }


def _simulate_alert_rule(rule: dict, days: int, stake: float) -> dict:
    """Replay resolved signals against a rule. Returns aggregate stats +
    the per-bet equity curve + the first 200 matched signals.

    The rule shape matches what /api/alert-rules accepts: sports (JSON
    list), market_types (JSON list), min_divergence_pp, min_volume,
    max_time_to_event_hours, require_sharp_consensus, require_not_stale,
    require_liquidity_ok.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM sports_edge_history
               WHERE detected_at >= ?
                 AND resolved = 1
                 AND resolution IN ('correct', 'incorrect')
               ORDER BY detected_at ASC""",
            (cutoff,),
        ).fetchall()

    bets: list[float] = []
    equity: list[float] = []
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    matches: list[dict] = []

    for raw in rows:
        row = dict(raw)
        sport, signal = _signal_from_edge_row(row)
        if not _signal_matches_rule(signal, sport, rule):
            continue
        poly = float(row.get("poly_prob") or 0)
        if poly <= 0 or poly >= 100:
            continue
        if row.get("resolution") == "correct":
            profit = stake * (100.0 / poly - 1.0)
        else:
            profit = -stake
        bets.append(profit)
        running += profit
        peak = max(peak, running)
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
        equity.append(round(running, 2))
        if len(matches) < 200:
            matches.append({
                "detected_at": row.get("detected_at"),
                "sport": sport,
                "event": _event_name(row.get("home_team"), row.get("away_team")),
                "outcome": row.get("outcome"),
                "divergence": row.get("divergence"),
                "poly_prob": poly,
                "resolution": row.get("resolution"),
                "pnl": round(profit, 2),
            })

    n = len(bets)
    if n == 0:
        return {
            "days": days, "stake": stake,
            "n_bets": 0, "total_pnl": 0.0,
            "win_rate": 0.0, "roi_pct": 0.0,
            "sharpe": 0.0, "max_drawdown": 0.0,
            "equity_curve": [], "matches": [],
        }

    total_pnl = sum(bets)
    wins = sum(1 for b in bets if b > 0)
    win_rate = wins / n
    roi = (total_pnl / (n * stake)) * 100

    if n > 1:
        mean = total_pnl / n
        var = sum((b - mean) ** 2 for b in bets) / (n - 1)
        std = math.sqrt(var) if var > 0 else 0.0
        sharpe = (mean / std) * math.sqrt(n) if std > 0 else 0.0
    else:
        sharpe = 0.0

    return {
        "days": days,
        "stake": stake,
        "n_bets": n,
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate, 4),
        "roi_pct": round(roi, 3),
        "sharpe": round(sharpe, 3),
        "max_drawdown": round(max_dd, 2),
        "equity_curve": equity,
        "matches": matches,
    }


# ---------------------------------------------------------------------------
# Steam moves — detect rapid sharp-book line moves (T4.1)
# ---------------------------------------------------------------------------
#
# A "steam move" is a fast, sharp-money-driven line move at a major book.
# We detect it by scanning consecutive snapshots in sports_market_snapshots
# for the same (sport, event, outcome) and flagging any pair where:
#   |book_prob_late - book_prob_early| >= STEAM_MIN_DELTA_PP
#   and (snapshot_at_late - snapshot_at_early) <= STEAM_WINDOW_MINUTES
#
# Pros trade off steam moves because they signal that sharp action just
# hit a major book — the rest of the market is about to follow.

STEAM_MIN_DELTA_PP = 2.0     # minimum sharp-book move to qualify
STEAM_WINDOW_MINUTES = 30    # over no more than this many minutes


def _detect_steam_moves(sport: str | None, hours: int = 24,
                          min_delta_pp: float | None = None,
                          window_min: int | None = None) -> list[dict]:
    """Scan recent market snapshots for fast sharp-book moves.

    For each (sport, event, outcome), walk the snapshot stream and emit
    a steam-move row whenever two snapshots within `window_min` minutes
    show a |book_prob| swing >= `min_delta_pp`. Each event/outcome can
    emit multiple moves if the line keeps stair-stepping.
    """
    min_d = float(min_delta_pp if min_delta_pp is not None else STEAM_MIN_DELTA_PP)
    win = int(window_min if window_min is not None else STEAM_WINDOW_MINUTES)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    where = ["snapshot_at >= ?", "book_prob IS NOT NULL"]
    params: list = [cutoff]
    if sport:
        where.append("sport = ?")
        params.append(sport)

    sql = ("SELECT sport, event_name, outcome, book_prob, poly_prob, "
           "       kalshi_prob, snapshot_at "
           "FROM sports_market_snapshots "
           "WHERE " + " AND ".join(where) + " "
           "ORDER BY sport, event_name, outcome, snapshot_at ASC")
    with _get_db() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    moves: list[dict] = []
    # Group consecutive rows by (sport, event, outcome) — they're already
    # sorted, so we just walk and reset whenever the key changes.
    current_key = None
    history: list[dict] = []
    for r in rows:
        key = (r["sport"], r["event_name"], r["outcome"])
        if key != current_key:
            current_key = key
            history = []
        history.append(r)

        # Walk back through this key's history looking for the most-recent
        # snapshot still inside the window. Compare against r to detect a
        # move. We don't dedupe overlapping moves per key — successive
        # snapshots can each emit a move, which is the desired behavior
        # for a line that keeps stair-stepping.
        late_dt = _parse_iso_utc(r["snapshot_at"])
        if not late_dt:
            continue
        for i in range(len(history) - 2, -1, -1):
            early = history[i]
            early_dt = _parse_iso_utc(early["snapshot_at"])
            if not early_dt:
                continue
            elapsed_min = (late_dt - early_dt).total_seconds() / 60.0
            if elapsed_min > win:
                break  # rest of history is older than the window
            try:
                early_prob = float(early["book_prob"])
                late_prob = float(r["book_prob"])
            except (TypeError, ValueError):
                continue
            delta = late_prob - early_prob
            if abs(delta) >= min_d:
                # Don't double-emit the same (window, direction). The
                # caller usually cares about the strongest move per key,
                # so we only emit if this is the largest move involving
                # `r` we've seen so far for this pair span.
                moves.append({
                    "sport": r["sport"],
                    "event": r["event_name"],
                    "outcome": r["outcome"],
                    "delta_pp": round(delta, 2),
                    "from_prob": round(early_prob, 2),
                    "to_prob": round(late_prob, 2),
                    "elapsed_min": round(elapsed_min, 1),
                    "from_ts": early["snapshot_at"],
                    "to_ts": r["snapshot_at"],
                    "poly_prob": r.get("poly_prob"),
                    "kalshi_prob": r.get("kalshi_prob"),
                })
                break  # one move per `late` row is enough

    # Dedupe to most-significant move per (key, late_ts) — within a single
    # snapshot we might match multiple earlier snapshots; the inner break
    # above already enforces "first match wins" but we also collapse the
    # outer list to one row per (key, to_ts) to be safe.
    seen = set()
    unique: list[dict] = []
    for m in moves:
        ident = (m["sport"], m["event"], m["outcome"], m["to_ts"])
        if ident in seen:
            continue
        seen.add(ident)
        unique.append(m)

    # Sort by absolute delta desc — biggest moves first.
    unique.sort(key=lambda m: -abs(m["delta_pp"]))
    return unique


# ---------------------------------------------------------------------------
# Closing line consensus — sharp book closing prob per (event, outcome) (T4.2)
# ---------------------------------------------------------------------------
#
# The closing line at a sharp book is the canonical "true" probability for
# retrospective CLV analysis. We compute it from sports_market_snapshots
# as "the latest book_prob snapshot before (or at) the event's
# commence_time" — same logic _compute_clv uses, but exposed as a
# first-class endpoint so pros can pull closing lines for arbitrary
# events without inferring them from the CLV summary.

def _compute_closing_lines(sport: str | None, days: int = 7) -> list[dict]:
    """Return closing-line rows for every (sport, event, outcome) with at
    least one snapshot inside the window.

    Closing line = latest book_prob whose snapshot_at is <= the linked
    event's commence_time (joined from sports_scores). If commence_time
    is unknown for an event, we fall back to the latest snapshot in the
    window (best-effort).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    where = ["s.snapshot_at >= ?"]
    params: list = [cutoff]
    if sport:
        where.append("s.sport = ?")
        params.append(sport)

    sql = (
        "SELECT s.sport, s.event_name, s.outcome, "
        "       s.book_prob, s.poly_prob, s.kalshi_prob, s.snapshot_at, "
        "       sc.commence_time, sc.home_team, sc.away_team "
        "FROM sports_market_snapshots s "
        "LEFT JOIN sports_scores sc "
        "  ON sc.sport = s.sport "
        " AND (s.event_name = sc.home_team || ' vs ' || sc.away_team) "
        "WHERE " + " AND ".join(where) + " "
        "  AND s.book_prob IS NOT NULL "
        "ORDER BY s.sport, s.event_name, s.outcome, s.snapshot_at ASC"
    )
    with _get_db() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    # Group by (sport, event, outcome) and pick the latest snapshot <= commence_time
    closings: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        key = (r["sport"], r["event_name"], r["outcome"])
        commence_dt = _parse_iso_utc(r.get("commence_time") or "")
        snap_dt = _parse_iso_utc(r["snapshot_at"])
        if not snap_dt:
            continue
        if commence_dt and snap_dt > commence_dt:
            # snapshot was after kickoff — skip
            continue
        existing = closings.get(key)
        if existing is None or snap_dt > existing["_snap_dt"]:
            closings[key] = {
                "sport": r["sport"],
                "event": r["event_name"],
                "outcome": r["outcome"],
                "closing_book_prob": round(float(r["book_prob"]), 2),
                "closing_poly_prob": (round(float(r["poly_prob"]), 2)
                                       if r.get("poly_prob") is not None else None),
                "closing_kalshi_prob": (round(float(r["kalshi_prob"]), 2)
                                         if r.get("kalshi_prob") is not None else None),
                "closing_ts": r["snapshot_at"],
                "commence_time": r.get("commence_time") or None,
                "_snap_dt": snap_dt,
            }

    out = []
    for v in closings.values():
        v.pop("_snap_dt", None)
        out.append(v)
    # Most-recently closed first
    out.sort(key=lambda x: x.get("closing_ts") or "", reverse=True)
    return out


# ---------------------------------------------------------------------------
# Auto-resolution: fetch scores and resolve edge history
# ---------------------------------------------------------------------------

def fetch_scores(sport_key: str) -> list[dict]:
    """Fetch completed game scores from The Odds API."""
    if not ODDS_API_KEY or sport_key in ESPORTS_KEYS or sport_key in KALSHI_ONLY_SPORTS:
        return []
    try:
        resp = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores",
            params={"apiKey": ODDS_API_KEY, "daysFrom": 3},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning("fetch_scores(%s) error: %s", sport_key, e)
        return []


def _store_scores(sport_key: str, scores: list[dict]):
    """Store/update completed game scores."""
    with _get_db() as conn:
        for sc in scores:
            if not sc.get("completed"):
                continue
            home = sc.get("home_team", "")
            away = sc.get("away_team", "")
            event_id = sc.get("id", "")
            home_score = away_score = 0
            for s in sc.get("scores", []) or []:
                if s.get("name") == home:
                    home_score = int(s.get("score", 0) or 0)
                elif s.get("name") == away:
                    away_score = int(s.get("score", 0) or 0)
            winner = home if home_score > away_score else (away if away_score > home_score else "draw")
            conn.execute(
                """INSERT INTO sports_scores (sport, event_id, home_team, away_team, home_score, away_score, completed, winner, commence_time)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(sport, event_id) DO UPDATE SET
                       home_score = excluded.home_score, away_score = excluded.away_score,
                       completed = 1, winner = excluded.winner""",
                (sport_key, event_id, home, away, home_score, away_score, winner,
                 sc.get("commence_time", "")),
            )


def _auto_resolve_edges():
    """Resolve unresolved edge_history entries against completed scores."""
    resolved_count = 0
    with _get_db() as conn:
        # Get all completed scores from last 7 days
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        scores = conn.execute(
            "SELECT * FROM sports_scores WHERE completed = 1 AND fetched_at >= ?", (cutoff,)
        ).fetchall()
        score_map = {}
        for sc in scores:
            sc = dict(sc)
            key = (normalize_name(sc["home_team"]), normalize_name(sc["away_team"]))
            score_map[key] = sc
            # Also index by reversed order
            rev_key = (normalize_name(sc["away_team"]), normalize_name(sc["home_team"]))
            score_map[rev_key] = sc

        # Get unresolved edges from last 7 days
        unresolved = conn.execute(
            "SELECT * FROM sports_edge_history WHERE resolved = 0 AND detected_at >= ?", (cutoff,)
        ).fetchall()

        for edge in unresolved:
            edge = dict(edge)
            home_n = normalize_name(edge["home_team"])
            away_n = normalize_name(edge["away_team"])
            score = score_map.get((home_n, away_n))
            if not score:
                # Try fuzzy match. Use partial_ratio so abbreviations like
                # "LA Rams" still match "Los Angeles Rams" (ratio is overly
                # length-sensitive and would mark these as a miss).
                for (sh, sa), sc_data in score_map.items():
                    if (fuzz.partial_ratio(home_n, sh) > 85
                            and fuzz.partial_ratio(away_n, sa) > 85):
                        score = sc_data
                        break
            if not score:
                continue

            # Determine if edge was correct
            outcome_name = normalize_name(edge["outcome"])
            winner_name = normalize_name(score["winner"])
            divergence = edge["divergence"] or 0

            # Draw/tie synonyms — treat "draw", "tie", and "x" as equivalent
            _draw_terms = {"draw", "tie", "x"}
            outcome_is_draw = outcome_name in _draw_terms
            winner_is_draw = winner_name in _draw_terms

            # Match outcome name against the actual winner. partial_ratio so
            # "LA Rams" vs "Los Angeles Rams" still matches; falling back to
            # `ratio` would falsely mark this as "outcome did not win", which
            # then flips to a phantom `is_correct=True` on negative divergence.
            if outcome_is_draw and winner_is_draw:
                outcome_won = True
            elif outcome_is_draw or winner_is_draw:
                outcome_won = False
            else:
                outcome_won = fuzz.partial_ratio(outcome_name, winner_name) > 80

            # Skip edges we cannot positively classify (no recorded winner) so
            # we don't mislabel them via the negative-divergence flip below.
            if not winner_name:
                continue

            if divergence > 0:
                # We recommended this outcome — check if it won
                is_correct = outcome_won
            else:
                # Negative divergence = Polymarket overpriced this outcome.
                # "Correct" if this outcome did NOT win.
                is_correct = not outcome_won

            resolution = "correct" if is_correct else "incorrect"
            conn.execute(
                "UPDATE sports_edge_history SET resolved = 1, resolution = ? WHERE id = ?",
                (resolution, edge["id"]),
            )
            resolved_count += 1

    if resolved_count > 0:
        print(f"Auto-resolved {resolved_count} edges", flush=True)
    return resolved_count


# ---------------------------------------------------------------------------
# Historical team data (ESPN) - powers head-to-head and recent form
# ---------------------------------------------------------------------------

# Maps our internal sport_key -> ESPN (sport, league) tuple. Sports without an
# ESPN endpoint (esports, individual sports w/o team-vs-team H2H) are omitted.
ESPN_LEAGUE_MAP: dict[str, tuple[str, str]] = {
    "basketball_nba": ("basketball", "nba"),
    "americanfootball_nfl": ("football", "nfl"),
    "americanfootball_ncaaf": ("football", "college-football"),
    "icehockey_nhl": ("hockey", "nhl"),
    "baseball_mlb": ("baseball", "mlb"),
    "soccer_epl": ("soccer", "eng.1"),
    "soccer_spain_la_liga": ("soccer", "esp.1"),
    "soccer_germany_bundesliga": ("soccer", "ger.1"),
    "soccer_italy_serie_a": ("soccer", "ita.1"),
    "soccer_france_ligue_one": ("soccer", "fra.1"),
    "soccer_uefa_champs_league": ("soccer", "uefa.champions"),
    "soccer_uefa_europa_league": ("soccer", "uefa.europa"),
    "soccer_usa_mls": ("soccer", "usa.1"),
    "mma_mixed_martial_arts": ("mma", "ufc"),
    "boxing_boxing": ("boxing", "boxing"),
}


def _espn_scoreboard_url(sport_key: str, date_str: str) -> str | None:
    league = ESPN_LEAGUE_MAP.get(sport_key)
    if not league:
        return None
    sport, lg = league
    return f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{lg}/scoreboard?dates={date_str}&limit=200"


def _parse_espn_event(ev: dict) -> dict | None:
    """Convert one ESPN scoreboard event into our internal row format."""
    try:
        comps = ev.get("competitions") or []
        if not comps:
            return None
        comp = comps[0]
        status = (comp.get("status") or {}).get("type", {})
        # Only count completed games
        if not status.get("completed"):
            return None
        teams = comp.get("competitors") or []
        if len(teams) < 2:
            return None
        home = away = None
        for t in teams:
            if t.get("homeAway") == "home":
                home = t
            elif t.get("homeAway") == "away":
                away = t
        # Boxing/MMA scoreboards don't tag homeAway — fall back to first/second
        if home is None or away is None:
            home, away = teams[0], teams[1]
        # ESPN responses sometimes set "athlete" to None explicitly (boxing
        # cards without a roster entry). `dict.get("k", {})` returns None in
        # that case, so chained `.get("displayName")` would crash with
        # AttributeError. Use `(... or {})` to coerce None to an empty dict.
        h_name = (
            (home.get("team") or {}).get("displayName")
            or (home.get("athlete") or {}).get("displayName")
            or ""
        )
        a_name = (
            (away.get("team") or {}).get("displayName")
            or (away.get("athlete") or {}).get("displayName")
            or ""
        )
        if not h_name or not a_name:
            return None
        try:
            h_score = int(home.get("score") or 0)
            a_score = int(away.get("score") or 0)
        except (ValueError, TypeError):
            h_score = a_score = 0
        # Determine winner — ESPN sets a "winner" boolean on the competitor
        winner = ""
        if home.get("winner") is True:
            winner = h_name
        elif away.get("winner") is True:
            winner = a_name
        elif h_score > a_score:
            winner = h_name
        elif a_score > h_score:
            winner = a_name
        else:
            winner = "draw"
        date_iso = ev.get("date") or comp.get("date") or ""
        date_only = date_iso[:10] if date_iso else ""
        season = ""
        try:
            season = str((ev.get("season") or {}).get("year") or "")
        except Exception:
            pass
        return {
            "event_date": date_only,
            "home_team": h_name,
            "away_team": a_name,
            "home_team_norm": normalize_name(h_name),
            "away_team_norm": normalize_name(a_name),
            "home_score": h_score,
            "away_score": a_score,
            "winner": winner,
            "season": season,
        }
    except Exception:
        return None


def fetch_espn_history(sport_key: str, days_back: int = 180) -> list[dict]:
    """Pull completed games from ESPN's public scoreboard API for the given sport.

    Iterates day-by-day from today back `days_back` days. Returns a list of
    parsed event dicts ready for _store_team_history. Stops early if many
    consecutive empty days are encountered (off-season).
    """
    if sport_key not in ESPN_LEAGUE_MAP:
        return []
    session = _make_http_session()
    rows: list[dict] = []
    today = datetime.now(timezone.utc).date()
    consecutive_empty = 0
    max_empty_streak = 30  # bail out of long off-season gaps
    for offset in range(days_back):
        d = today - timedelta(days=offset)
        date_str = d.strftime("%Y%m%d")
        url = _espn_scoreboard_url(sport_key, date_str)
        if not url:
            break
        try:
            resp = session.get(url, timeout=15)
            if resp.status_code != 200:
                consecutive_empty += 1
                if consecutive_empty >= max_empty_streak:
                    break
                continue
            data = resp.json()
        except Exception as e:
            log.debug("ESPN fetch %s %s: %s", sport_key, date_str, e)
            consecutive_empty += 1
            if consecutive_empty >= max_empty_streak:
                break
            continue
        events = data.get("events") or []
        day_added = 0
        for ev in events:
            parsed = _parse_espn_event(ev)
            if parsed:
                parsed["sport"] = sport_key
                rows.append(parsed)
                day_added += 1
        if day_added == 0:
            consecutive_empty += 1
            if consecutive_empty >= max_empty_streak:
                break
        else:
            consecutive_empty = 0
        # Be a polite citizen of ESPN's free API
        import time as _t
        _t.sleep(0.05)
    return rows


def _store_team_history(rows: list[dict]) -> int:
    """Bulk insert team history rows. Returns number actually inserted."""
    if not rows:
        return 0
    inserted = 0
    with _get_db() as conn:
        for r in rows:
            try:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO sports_team_history
                       (sport, event_date, home_team, away_team, home_team_norm, away_team_norm,
                        home_score, away_score, winner, season, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'espn')""",
                    (r["sport"], r["event_date"], r["home_team"], r["away_team"],
                     r["home_team_norm"], r["away_team_norm"],
                     r["home_score"], r["away_score"], r["winner"], r.get("season", "")),
                )
                if cur.rowcount > 0:
                    inserted += 1
            except Exception as e:
                log.debug("history insert error: %s", e)
        # Update meta row for this sport
        if rows:
            sport = rows[0]["sport"]
            total = conn.execute(
                "SELECT COUNT(*) FROM sports_team_history WHERE sport = ?", (sport,)
            ).fetchone()[0]
            last_date = conn.execute(
                "SELECT MAX(event_date) FROM sports_team_history WHERE sport = ?", (sport,)
            ).fetchone()[0] or ""
            conn.execute(
                """INSERT INTO sports_history_meta (sport, last_fetch_at, last_date_covered, rows_total)
                   VALUES (?, datetime('now'), ?, ?)
                   ON CONFLICT(sport) DO UPDATE SET
                       last_fetch_at = excluded.last_fetch_at,
                       last_date_covered = excluded.last_date_covered,
                       rows_total = excluded.rows_total""",
                (sport, last_date, total),
            )
    return inserted


def _refresh_history_for_sport(sport_key: str, days_back: int = 30) -> int:
    """Pull recent games for a sport and persist them. Used by background refresh."""
    if sport_key not in ESPN_LEAGUE_MAP:
        return 0
    rows = fetch_espn_history(sport_key, days_back=days_back)
    return _store_team_history(rows)


def _backfill_all_team_history(days_back: int = 365):
    """One-shot backfill for every supported sport. Run in background on startup."""
    total = 0
    for sport_key in ESPN_LEAGUE_MAP:
        try:
            n = _refresh_history_for_sport(sport_key, days_back=days_back)
            total += n
            print(f"  team_history: {sport_key} +{n} games", flush=True)
        except Exception as e:
            print(f"  team_history: {sport_key} error: {e}", flush=True)
    print(f"team_history backfill complete: {total} new rows", flush=True)
    return total


def _compute_h2h(sport_key: str, team_a: str, team_b: str, lookback: int = 10) -> dict | None:
    """Look up the head-to-head history between two teams. Returns a summary
    dict the frontend can render directly. Returns None if no games found.
    """
    if not team_a or not team_b:
        return None
    a_norm = normalize_name(team_a)
    b_norm = normalize_name(team_b)
    if not a_norm or not b_norm or a_norm == b_norm:
        return None
    try:
        with _get_db() as conn:
            rows = conn.execute(
                """SELECT event_date, home_team, away_team, home_team_norm, away_team_norm,
                          home_score, away_score, winner
                   FROM sports_team_history
                   WHERE sport = ?
                     AND ((home_team_norm = ? AND away_team_norm = ?)
                          OR (home_team_norm = ? AND away_team_norm = ?))
                   ORDER BY event_date DESC
                   LIMIT ?""",
                (sport_key, a_norm, b_norm, b_norm, a_norm, lookback),
            ).fetchall()
    except Exception as e:
        log.debug("h2h query error: %s", e)
        return None
    if not rows:
        return None
    a_wins = b_wins = draws = 0
    recent: list[dict] = []
    for row in rows:
        r = dict(row)
        winner_norm = normalize_name(r["winner"])
        if winner_norm == a_norm:
            a_wins += 1
            who = "a"
        elif winner_norm == b_norm:
            b_wins += 1
            who = "b"
        else:
            draws += 1
            who = "draw"
        recent.append({
            "date": r["event_date"],
            "home": r["home_team"],
            "away": r["away_team"],
            "home_score": r["home_score"],
            "away_score": r["away_score"],
            "winner": r["winner"],
            "who": who,
        })
    last = recent[0]
    return {
        "team_a": team_a,
        "team_b": team_b,
        "total_games": len(rows),
        "team_a_wins": a_wins,
        "team_b_wins": b_wins,
        "draws": draws,
        "last_meeting": {
            "date": last["date"],
            "score": f"{last['home']} {last['home_score']}-{last['away_score']} {last['away']}",
            "winner": last["winner"],
        },
        "recent": recent,
    }


def _compute_team_form(sport_key: str, team: str, last_n: int = 5) -> dict | None:
    """Recent W/L/D form for a team across all opponents."""
    if not team:
        return None
    t_norm = normalize_name(team)
    if not t_norm:
        return None
    try:
        with _get_db() as conn:
            rows = conn.execute(
                """SELECT event_date, home_team, away_team, home_team_norm, away_team_norm,
                          home_score, away_score, winner
                   FROM sports_team_history
                   WHERE sport = ?
                     AND (home_team_norm = ? OR away_team_norm = ?)
                   ORDER BY event_date DESC
                   LIMIT ?""",
                (sport_key, t_norm, t_norm, last_n),
            ).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    results: list[str] = []
    wins = losses = draws = 0
    for row in rows:
        r = dict(row)
        winner_norm = normalize_name(r["winner"])
        if winner_norm in {"draw", "tie", ""}:
            results.append("D")
            draws += 1
        elif winner_norm == t_norm:
            results.append("W")
            wins += 1
        else:
            results.append("L")
            losses += 1
    return {
        "team": team,
        "results": results,
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "last_n": len(results),
    }


def _attach_h2h_to_comparisons(sport_key: str, comparisons: list[dict]) -> None:
    """Mutates each comparison in place to add h2h, home_form, away_form,
    plus team_info, players, and chemistry score for both sides."""
    if sport_key not in ESPN_LEAGUE_MAP:
        return
    for comp in comparisons:
        # Skip futures (no opponent)
        if comp.get("is_futures"):
            continue
        home = comp.get("home_team", "")
        away = comp.get("away_team", "")
        if not home or not away:
            continue
        h2h = _compute_h2h(sport_key, home, away)
        if h2h:
            comp["h2h"] = h2h
        home_form = _compute_team_form(sport_key, home)
        if home_form:
            comp["home_form"] = home_form
        away_form = _compute_team_form(sport_key, away)
        if away_form:
            comp["away_form"] = away_form
        # Team info: record, ranking, points-for/against
        home_info = _get_team_info(sport_key, home)
        if home_info:
            comp["home_info"] = home_info
        away_info = _get_team_info(sport_key, away)
        if away_info:
            comp["away_info"] = away_info
        # Top players
        home_players = _get_top_players(sport_key, home, limit=3)
        if home_players:
            comp["home_players"] = home_players
        away_players = _get_top_players(sport_key, away, limit=3)
        if away_players:
            comp["away_players"] = away_players
        # Chemistry score (form consistency × close-game record × roster stability)
        comp["home_chemistry"] = _compute_chemistry(sport_key, home, home_form, home_info)
        comp["away_chemistry"] = _compute_chemistry(sport_key, away, away_form, away_info)


# ---------------------------------------------------------------------------
# ESPN team standings + rosters (powers team_info + player_info)
# ---------------------------------------------------------------------------

def _espn_teams_url(sport_key: str) -> str | None:
    league = ESPN_LEAGUE_MAP.get(sport_key)
    if not league:
        return None
    sport, lg = league
    return f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{lg}/teams?limit=100"


def _espn_team_detail_url(sport_key: str, team_id: str) -> str | None:
    league = ESPN_LEAGUE_MAP.get(sport_key)
    if not league:
        return None
    sport, lg = league
    return f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{lg}/teams/{team_id}"


def _espn_team_roster_url(sport_key: str, team_id: str) -> str | None:
    league = ESPN_LEAGUE_MAP.get(sport_key)
    if not league:
        return None
    sport, lg = league
    return f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{lg}/teams/{team_id}/roster"


def _espn_athlete_stats_url(sport_key: str, athlete_id: str) -> str | None:
    league = ESPN_LEAGUE_MAP.get(sport_key)
    if not league:
        return None
    sport, lg = league
    # ESPN has a per-athlete stats endpoint that returns season averages
    return f"https://site.api.espn.com/apis/common/v3/sports/{sport}/{lg}/athletes/{athlete_id}/statistics"


def fetch_espn_teams(sport_key: str) -> list[dict]:
    """Fetch all teams for a sport with their season records.

    Returns a list of dicts with keys: espn_id, name, abbreviation, wins,
    losses, draws, win_pct, points_for, points_against, rank, conference_rank,
    streak, last_10, logo_url.
    """
    if sport_key not in ESPN_LEAGUE_MAP:
        return []
    session = _make_http_session()
    url = _espn_teams_url(sport_key)
    if not url:
        return []
    try:
        resp = session.get(url, timeout=20)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception as e:
        log.debug("espn teams fetch %s: %s", sport_key, e)
        return []

    teams_out: list[dict] = []
    sports_arr = data.get("sports") or []
    for sp in sports_arr:
        for lg in sp.get("leagues") or []:
            for tw in lg.get("teams") or []:
                team = tw.get("team") or {}
                name = team.get("displayName") or team.get("name") or ""
                if not name:
                    continue
                espn_id = str(team.get("id") or "")
                abbr = team.get("abbreviation") or ""
                logos = team.get("logos") or []
                logo_url = (logos[0].get("href") if logos else "") or ""
                teams_out.append({
                    "sport": sport_key,
                    "espn_id": espn_id,
                    "name": name,
                    "team_norm": normalize_name(name),
                    "abbreviation": abbr,
                    "logo_url": logo_url,
                    "wins": 0, "losses": 0, "draws": 0, "win_pct": 0.0,
                    "points_for": 0.0, "points_against": 0.0,
                    "rank": 0, "conference_rank": 0, "streak": "", "last_10": "",
                    "close_game_wins": 0, "close_game_losses": 0,
                })
    # Each team needs a per-team detail call to grab record + stats
    for t in teams_out:
        try:
            detail_url = _espn_team_detail_url(sport_key, t["espn_id"])
            if not detail_url:
                continue
            r = session.get(detail_url, timeout=15)
            if r.status_code != 200:
                continue
            d = r.json()
            tm = (d.get("team") or {})
            # Record info
            record_groups = (tm.get("record") or {}).get("items") or []
            for rg in record_groups:
                if rg.get("type") == "total" or not rg.get("type"):
                    stats_arr = rg.get("stats") or []
                    for st in stats_arr:
                        nm = st.get("name") or ""
                        val = st.get("value")
                        if nm == "wins" and val is not None:
                            t["wins"] = int(val)
                        elif nm == "losses" and val is not None:
                            t["losses"] = int(val)
                        elif nm == "ties" and val is not None:
                            t["draws"] = int(val)
                        elif nm == "winPercent" and val is not None:
                            t["win_pct"] = float(val)
                        elif nm == "pointsFor" and val is not None:
                            t["points_for"] = float(val)
                        elif nm == "pointsAgainst" and val is not None:
                            t["points_against"] = float(val)
                        elif nm == "streak" and val is not None:
                            t["streak"] = str(int(val))
                    summary = rg.get("summary") or ""
                    if summary and not t["last_10"]:
                        t["last_10"] = summary
            # Conference standing
            standing = tm.get("standingSummary") or ""
            if standing:
                # e.g. "3rd in Eastern Conference"
                import re as _re
                m = _re.search(r"(\d+)", standing)
                if m:
                    t["conference_rank"] = int(m.group(1))
        except Exception as e:
            log.debug("espn team detail %s/%s: %s", sport_key, t.get("espn_id"), e)
        import time as _t
        _t.sleep(0.04)
    return teams_out


def _store_team_info(rows: list[dict]) -> int:
    """Bulk upsert team info rows. Returns number written."""
    if not rows:
        return 0
    written = 0
    with _get_db() as conn:
        for r in rows:
            try:
                conn.execute(
                    """INSERT INTO sports_team_info
                       (sport, team_name, team_norm, espn_id, abbreviation, wins, losses, draws,
                        win_pct, points_for, points_against, rank, conference_rank, streak,
                        close_game_wins, close_game_losses, last_10, logo_url, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                       ON CONFLICT(sport, team_norm) DO UPDATE SET
                         espn_id = excluded.espn_id,
                         abbreviation = excluded.abbreviation,
                         wins = excluded.wins,
                         losses = excluded.losses,
                         draws = excluded.draws,
                         win_pct = excluded.win_pct,
                         points_for = excluded.points_for,
                         points_against = excluded.points_against,
                         rank = excluded.rank,
                         conference_rank = excluded.conference_rank,
                         streak = excluded.streak,
                         last_10 = excluded.last_10,
                         logo_url = excluded.logo_url,
                         fetched_at = datetime('now')""",
                    (r["sport"], r["name"], r["team_norm"], r.get("espn_id", ""),
                     r.get("abbreviation", ""), r.get("wins", 0), r.get("losses", 0),
                     r.get("draws", 0), r.get("win_pct", 0.0),
                     r.get("points_for", 0.0), r.get("points_against", 0.0),
                     r.get("rank", 0), r.get("conference_rank", 0),
                     r.get("streak", ""),
                     r.get("close_game_wins", 0), r.get("close_game_losses", 0),
                     r.get("last_10", ""), r.get("logo_url", "")),
                )
                written += 1
            except Exception as e:
                log.debug("team_info upsert error: %s", e)
    return written


# Per-sport stat key mapping for player stats. ESPN uses different names per
# sport so we centralize them here. Used by _summarize_player_stats.
_PLAYER_STAT_KEYS: dict[str, list[tuple[str, str, str]]] = {
    # (display label, ESPN stat name candidates separated by |, format)
    "basketball_nba": [
        ("PPG", "avgPoints|points", "{:.1f}"),
        ("RPG", "avgRebounds|rebounds", "{:.1f}"),
        ("APG", "avgAssists|assists", "{:.1f}"),
        ("FG%", "fieldGoalPct", "{:.1f}%"),
        ("3P%", "threePointPct", "{:.1f}%"),
    ],
    "americanfootball_nfl": [
        ("YDS", "passingYards|rushingYards|receivingYards", "{:.0f}"),
        ("TD", "passingTouchdowns|rushingTouchdowns|receivingTouchdowns", "{:.0f}"),
        ("INT", "interceptions", "{:.0f}"),
        ("RTG", "QBRating|passerRating", "{:.1f}"),
    ],
    "americanfootball_ncaaf": [
        ("YDS", "passingYards|rushingYards|receivingYards", "{:.0f}"),
        ("TD", "passingTouchdowns|rushingTouchdowns|receivingTouchdowns", "{:.0f}"),
        ("INT", "interceptions", "{:.0f}"),
    ],
    "icehockey_nhl": [
        ("G", "goals", "{:.0f}"),
        ("A", "assists", "{:.0f}"),
        ("PTS", "points", "{:.0f}"),
        ("+/-", "plusMinus", "{:+.0f}"),
    ],
    "baseball_mlb": [
        ("AVG", "avg|battingAverage", "{:.3f}"),
        ("HR", "homeRuns", "{:.0f}"),
        ("RBI", "RBIs|runsBattedIn", "{:.0f}"),
        ("OPS", "OPS|onBasePlusSlugging", "{:.3f}"),
    ],
    "soccer_epl": [
        ("G", "totalGoals|goals", "{:.0f}"),
        ("A", "goalAssists|assists", "{:.0f}"),
        ("APP", "appearances", "{:.0f}"),
    ],
    "mma_mixed_martial_arts": [
        ("W", "wins", "{:.0f}"),
        ("L", "losses", "{:.0f}"),
        ("KO", "knockouts", "{:.0f}"),
    ],
}
# Reuse soccer mapping for all soccer leagues
for _k in ("soccer_spain_la_liga", "soccer_germany_bundesliga", "soccer_italy_serie_a",
           "soccer_france_ligue_one", "soccer_uefa_champs_league", "soccer_uefa_europa_league",
           "soccer_usa_mls"):
    _PLAYER_STAT_KEYS[_k] = _PLAYER_STAT_KEYS["soccer_epl"]


def _summarize_player_stats(sport_key: str, raw_stats: dict) -> dict:
    """Convert ESPN raw stats payload into a sport-specific summary dict."""
    out: dict = {}
    if not raw_stats:
        return out
    keys = _PLAYER_STAT_KEYS.get(sport_key, [])
    # ESPN's stats payloads vary, but most expose a "splits.categories" or
    # a flat list of stat dicts with name + value/displayValue.
    flat: dict[str, float] = {}
    try:
        # Drill into common shapes
        cats = raw_stats.get("splits", {}).get("categories") or raw_stats.get("categories") or []
        for cat in cats:
            for s in cat.get("stats") or []:
                nm = s.get("name") or ""
                val = s.get("value")
                if val is None:
                    try:
                        val = float(s.get("displayValue", "0").replace(",", "").replace("%", ""))
                    except (ValueError, AttributeError):
                        val = 0
                if nm:
                    try:
                        flat[nm] = float(val)
                    except (TypeError, ValueError):
                        pass
        # Also try the top-level "stats" if present
        for s in raw_stats.get("stats") or []:
            nm = s.get("name") or ""
            val = s.get("value")
            if nm and val is not None and nm not in flat:
                try:
                    flat[nm] = float(val)
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    for label, name_str, fmt in keys:
        for nm in name_str.split("|"):
            if nm in flat and flat[nm] != 0:
                try:
                    out[label] = fmt.format(flat[nm])
                except (ValueError, TypeError):
                    out[label] = str(flat[nm])
                break
    return out


def _derive_player_strengths(sport_key: str, raw_stats: dict, position: str = "") -> tuple[list[str], list[str]]:
    """Derive 1-2 strengths and 0-1 weaknesses from raw stats. Heuristic only."""
    strengths: list[str] = []
    weaknesses: list[str] = []
    if not raw_stats:
        return strengths, weaknesses
    flat: dict[str, float] = {}
    try:
        cats = raw_stats.get("splits", {}).get("categories") or raw_stats.get("categories") or []
        for cat in cats:
            for s in cat.get("stats") or []:
                nm = s.get("name") or ""
                val = s.get("value")
                if val is None:
                    try:
                        val = float(str(s.get("displayValue", "0")).replace(",", "").replace("%", ""))
                    except (ValueError, AttributeError):
                        val = 0
                if nm:
                    try:
                        flat[nm] = float(val)
                    except (TypeError, ValueError):
                        pass
    except Exception:
        return strengths, weaknesses

    if sport_key == "basketball_nba":
        ppg = flat.get("avgPoints", flat.get("points", 0))
        rpg = flat.get("avgRebounds", flat.get("rebounds", 0))
        apg = flat.get("avgAssists", flat.get("assists", 0))
        fg_pct = flat.get("fieldGoalPct", 0)
        tp_pct = flat.get("threePointPct", 0)
        ft_pct = flat.get("freeThrowPct", 0)
        if ppg >= 22: strengths.append("Elite Scorer")
        elif ppg >= 16: strengths.append("Volume Scorer")
        if apg >= 7: strengths.append("Playmaker")
        if rpg >= 9: strengths.append("Rebounder")
        if tp_pct >= 38 and ppg >= 10: strengths.append("Sharpshooter")
        if fg_pct >= 55 and ppg >= 10: strengths.append("Efficient")
        if 0 < tp_pct < 30: weaknesses.append("Poor 3pt")
        if 0 < ft_pct < 65: weaknesses.append("Bad FT")
    elif sport_key in ("americanfootball_nfl", "americanfootball_ncaaf"):
        py = flat.get("passingYards", 0)
        ptd = flat.get("passingTouchdowns", 0)
        ints = flat.get("interceptions", 0)
        ry = flat.get("rushingYards", 0)
        rtd = flat.get("rushingTouchdowns", 0)
        recy = flat.get("receivingYards", 0)
        rectd = flat.get("receivingTouchdowns", 0)
        if py >= 3500: strengths.append("Elite Passer")
        elif py >= 2500: strengths.append("Starting QB")
        if ry >= 1000: strengths.append("Workhorse RB")
        elif ry >= 600: strengths.append("Rushing Threat")
        if recy >= 1000: strengths.append("WR1")
        elif recy >= 700: strengths.append("Reliable Target")
        if ptd >= 25: strengths.append("TD Machine")
        if rtd >= 8 or rectd >= 8: strengths.append("Red-Zone Threat")
        if ints >= 12: weaknesses.append("Turnover Prone")
    elif sport_key == "icehockey_nhl":
        goals = flat.get("goals", 0)
        assists = flat.get("assists", 0)
        pm = flat.get("plusMinus", 0)
        if goals >= 30: strengths.append("Sniper")
        elif goals >= 20: strengths.append("Scorer")
        if assists >= 50: strengths.append("Playmaker")
        if pm >= 15: strengths.append("Two-Way Star")
        if pm <= -10: weaknesses.append("Defensive Liability")
    elif sport_key == "baseball_mlb":
        avg = flat.get("avg", flat.get("battingAverage", 0))
        hr = flat.get("homeRuns", 0)
        rbi = flat.get("RBIs", flat.get("runsBattedIn", 0))
        ops = flat.get("OPS", flat.get("onBasePlusSlugging", 0))
        era = flat.get("ERA", 0)
        if avg >= 0.300: strengths.append("Hits for Avg")
        if hr >= 30: strengths.append("Power Hitter")
        elif hr >= 20: strengths.append("Slugger")
        if rbi >= 90: strengths.append("RBI Producer")
        if ops >= 0.900: strengths.append("Elite OPS")
        if 0 < era < 3: strengths.append("Ace")
        elif era > 4.5: weaknesses.append("High ERA")
    elif sport_key.startswith("soccer_"):
        goals = flat.get("totalGoals", flat.get("goals", 0))
        assists = flat.get("goalAssists", flat.get("assists", 0))
        if goals >= 15: strengths.append("Top Scorer")
        elif goals >= 8: strengths.append("Goal Threat")
        if assists >= 8: strengths.append("Creator")
        # Position-based fallbacks
        pos = (position or "").lower()
        if "keeper" in pos or pos in ("g", "gk"):
            strengths.append("Shot Stopper")
        elif "defender" in pos or pos in ("d", "df", "cb"):
            strengths.append("Defender")
    elif sport_key == "mma_mixed_martial_arts":
        wins = flat.get("wins", 0)
        losses = flat.get("losses", 0)
        kos = flat.get("knockouts", 0)
        if wins >= 15: strengths.append("Veteran")
        if kos >= 5: strengths.append("KO Artist")
        if losses >= 5: weaknesses.append("Losing Streak Risk")
    return strengths[:2], weaknesses[:1]


def fetch_espn_roster(sport_key: str, team_espn_id: str, team_norm: str, max_players: int = 25) -> list[dict]:
    """Fetch roster + per-player stats for a team. Returns parsed player rows."""
    if sport_key not in ESPN_LEAGUE_MAP or not team_espn_id:
        return []
    session = _make_http_session()
    roster_url = _espn_team_roster_url(sport_key, team_espn_id)
    if not roster_url:
        return []
    try:
        resp = session.get(roster_url, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception as e:
        log.debug("espn roster fetch %s/%s: %s", sport_key, team_espn_id, e)
        return []

    # ESPN structures rosters as either "athletes": [{position, items: [...]}, ...]
    # or a flat "athletes" list of athlete dicts.
    raw_athletes: list[dict] = []
    athletes_field = data.get("athletes") or []
    if athletes_field and isinstance(athletes_field[0], dict):
        if "items" in athletes_field[0]:
            for grp in athletes_field:
                for a in grp.get("items") or []:
                    raw_athletes.append(a)
        else:
            raw_athletes = athletes_field

    rows: list[dict] = []
    for a in raw_athletes[:max_players]:
        try:
            name = a.get("displayName") or a.get("fullName") or a.get("name") or ""
            if not name:
                continue
            ath_id = str(a.get("id") or "")
            position = ((a.get("position") or {}).get("abbreviation") or
                        (a.get("position") or {}).get("displayName") or "")
            jersey = str(a.get("jersey") or "")
            # Per-athlete stats lookup (best-effort, tolerate failure)
            stats_payload: dict = {}
            try:
                stats_url = _espn_athlete_stats_url(sport_key, ath_id)
                if stats_url:
                    sr = session.get(stats_url, timeout=10)
                    if sr.status_code == 200:
                        stats_payload = sr.json() or {}
            except Exception:
                pass
            stats_summary = _summarize_player_stats(sport_key, stats_payload)
            strengths, weaknesses = _derive_player_strengths(sport_key, stats_payload, position)
            # Impact score: weighted sum of "key" stats so we can pick top players
            impact = _player_impact_score(sport_key, stats_payload)
            rows.append({
                "sport": sport_key,
                "team_norm": team_norm,
                "player_name": name,
                "espn_id": ath_id,
                "position": position,
                "jersey": jersey,
                "stats_summary": stats_summary,
                "strengths": strengths,
                "weaknesses": weaknesses,
                "impact_score": impact,
            })
        except Exception as e:
            log.debug("athlete parse error: %s", e)
        import time as _t
        _t.sleep(0.03)
    return rows


def _player_impact_score(sport_key: str, raw_stats: dict) -> float:
    """Numeric impact score for ranking players within a team. Higher = more important."""
    if not raw_stats:
        return 0.0
    flat: dict[str, float] = {}
    try:
        cats = raw_stats.get("splits", {}).get("categories") or raw_stats.get("categories") or []
        for cat in cats:
            for s in cat.get("stats") or []:
                nm = s.get("name") or ""
                val = s.get("value", 0)
                if nm:
                    try:
                        flat[nm] = float(val or 0)
                    except (TypeError, ValueError):
                        pass
    except Exception:
        return 0.0
    if sport_key == "basketball_nba":
        return (flat.get("avgPoints", 0) * 1.0 +
                flat.get("avgRebounds", 0) * 0.7 +
                flat.get("avgAssists", 0) * 1.0)
    if sport_key in ("americanfootball_nfl", "americanfootball_ncaaf"):
        return (flat.get("passingYards", 0) * 0.001 +
                flat.get("passingTouchdowns", 0) * 1.0 +
                flat.get("rushingYards", 0) * 0.005 +
                flat.get("rushingTouchdowns", 0) * 1.0 +
                flat.get("receivingYards", 0) * 0.005 +
                flat.get("receivingTouchdowns", 0) * 1.0)
    if sport_key == "icehockey_nhl":
        return flat.get("goals", 0) * 1.0 + flat.get("assists", 0) * 0.7
    if sport_key == "baseball_mlb":
        return (flat.get("homeRuns", 0) * 1.5 +
                flat.get("RBIs", flat.get("runsBattedIn", 0)) * 0.5 +
                flat.get("OPS", 0) * 5)
    if sport_key.startswith("soccer_"):
        return (flat.get("totalGoals", flat.get("goals", 0)) * 2.0 +
                flat.get("goalAssists", flat.get("assists", 0)) * 1.5)
    return 0.0


def _store_player_info(rows: list[dict]) -> int:
    """Bulk upsert player rows. Returns number written."""
    if not rows:
        return 0
    written = 0
    with _get_db() as conn:
        for r in rows:
            try:
                conn.execute(
                    """INSERT INTO sports_player_info
                       (sport, team_norm, player_name, espn_id, position, jersey,
                        stats_json, strengths, weaknesses, impact_score, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                       ON CONFLICT(sport, team_norm, player_name) DO UPDATE SET
                         espn_id = excluded.espn_id,
                         position = excluded.position,
                         jersey = excluded.jersey,
                         stats_json = excluded.stats_json,
                         strengths = excluded.strengths,
                         weaknesses = excluded.weaknesses,
                         impact_score = excluded.impact_score,
                         fetched_at = datetime('now')""",
                    (r["sport"], r["team_norm"], r["player_name"],
                     r.get("espn_id", ""), r.get("position", ""), r.get("jersey", ""),
                     json.dumps(r.get("stats_summary") or {}),
                     json.dumps(r.get("strengths") or []),
                     json.dumps(r.get("weaknesses") or []),
                     float(r.get("impact_score", 0))),
                )
                written += 1
            except Exception as e:
                log.debug("player_info upsert error: %s", e)
    return written


def fetch_espn_team_leaders(sport_key: str, days_back: int = 21) -> dict[str, list[dict]]:
    """Pull recent scoreboard pages for a sport and extract per-team statistical
    leaders. ESPN scoreboard responses include `competitors[].leaders` with the
    top player per stat category (PPG/RPG/APG, passingYards/touchdowns, etc.)
    plus their displayValue. We aggregate across all events in the window so
    every team gets a list of leader-players with real, current-season values.

    Returns a dict keyed by team_norm → list of player rows ready for
    _store_player_info.
    """
    if sport_key not in ESPN_LEAGUE_MAP:
        return {}
    league = ESPN_LEAGUE_MAP[sport_key]
    sport, lg = league
    session = _make_http_session()
    leaders_by_team: dict[str, dict[str, dict]] = {}  # team_norm -> {player_name: row}

    # Walk back through the past N days. Each day's scoreboard returns up to
    # ~20 events; that gives us most teams in a typical league season.
    today = datetime.now(timezone.utc).date()
    for offset in range(0, days_back):
        d = today - timedelta(days=offset)
        date_str = d.strftime("%Y%m%d")
        url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{lg}/scoreboard?dates={date_str}"
        try:
            resp = session.get(url, timeout=15)
            if resp.status_code != 200:
                continue
            data = resp.json()
        except Exception as e:
            log.debug("espn scoreboard leaders %s %s: %s", sport_key, date_str, e)
            continue
        events = data.get("events") or []
        for ev in events:
            for comp in ev.get("competitions") or []:
                for c in comp.get("competitors") or []:
                    team = c.get("team") or {}
                    name = team.get("displayName") or team.get("name") or ""
                    if not name:
                        continue
                    team_norm = normalize_name(name)
                    bucket = leaders_by_team.setdefault(team_norm, {})
                    leaders_arr = c.get("leaders") or []
                    for ldr_cat in leaders_arr:
                        cat_name = ldr_cat.get("name") or ""
                        cat_short = ldr_cat.get("shortDisplayName") or ldr_cat.get("abbreviation") or cat_name
                        for ldr in ldr_cat.get("leaders") or []:
                            ath = ldr.get("athlete") or {}
                            pname = ath.get("displayName") or ath.get("fullName") or ""
                            if not pname:
                                continue
                            disp = ldr.get("displayValue") or ""
                            try:
                                value = float(ldr.get("value") or 0)
                            except (TypeError, ValueError):
                                value = 0.0
                            row = bucket.setdefault(pname, {
                                "player_name": pname,
                                "espn_id": str(ath.get("id") or ""),
                                "position": ((ath.get("position") or {}).get("abbreviation") or ""),
                                "jersey": str(ath.get("jersey") or ""),
                                "stats": {},
                                "raw_values": {},
                            })
                            # Stat label e.g. "PPG", "REB", "PTS" → display value
                            row["stats"][cat_short] = disp
                            row["raw_values"][cat_name] = value
        import time as _t
        _t.sleep(0.05)

    # Convert to player rows ready for _store_player_info
    out: dict[str, list[dict]] = {}
    for team_norm, players in leaders_by_team.items():
        rows: list[dict] = []
        for p in players.values():
            raw = p["raw_values"]
            strengths, weaknesses = _derive_player_strengths_from_raw(sport_key, raw, p["position"])
            impact = _player_impact_score_from_raw(sport_key, raw)
            rows.append({
                "sport": sport_key,
                "team_norm": team_norm,
                "player_name": p["player_name"],
                "espn_id": p["espn_id"],
                "position": p["position"],
                "jersey": p["jersey"],
                "stats_summary": p["stats"],
                "strengths": strengths,
                "weaknesses": weaknesses,
                "impact_score": impact,
            })
        # Keep top 6 per team by impact
        rows.sort(key=lambda r: r["impact_score"], reverse=True)
        out[team_norm] = rows[:6]
    return out


def _derive_player_strengths_from_raw(sport_key: str, raw: dict, position: str) -> tuple[list[str], list[str]]:
    """Strengths/weaknesses from the raw value dict produced by leaders extraction.
    The keys are ESPN stat names like 'pointsPerGame', 'reboundsPerGame', etc.
    """
    strengths: list[str] = []
    weaknesses: list[str] = []
    if not raw:
        return strengths, weaknesses
    if sport_key == "basketball_nba":
        ppg = raw.get("pointsPerGame", 0)
        rpg = raw.get("reboundsPerGame", 0)
        apg = raw.get("assistsPerGame", 0)
        if ppg >= 25: strengths.append("Elite Scorer")
        elif ppg >= 18: strengths.append("Scorer")
        if rpg >= 10: strengths.append("Glass Eater")
        elif rpg >= 7: strengths.append("Strong Rebounder")
        if apg >= 8: strengths.append("Floor General")
        elif apg >= 5: strengths.append("Playmaker")
        if ppg < 8 and rpg < 4 and apg < 3 and (ppg or rpg or apg):
            weaknesses.append("Limited Output")
    elif sport_key in ("americanfootball_nfl", "americanfootball_ncaaf"):
        py = raw.get("passingYards", 0)
        pt = raw.get("passingTouchdowns", 0)
        ry = raw.get("rushingYards", 0)
        rt = raw.get("rushingTouchdowns", 0)
        rcy = raw.get("receivingYards", 0)
        rct = raw.get("receivingTouchdowns", 0)
        ints = raw.get("interceptions", 0)
        rtg = raw.get("QBRating", 0) or raw.get("passerRating", 0)
        if py >= 4000: strengths.append("Elite Passer")
        elif py >= 2500: strengths.append("Pocket Arm")
        if pt >= 25: strengths.append("TD Machine")
        if ints >= 12: weaknesses.append("Turnover Prone")
        if ry >= 1000: strengths.append("Bell Cow Back")
        elif ry >= 600: strengths.append("Workhorse")
        if rt >= 8: strengths.append("Goal Line Threat")
        if rcy >= 1000: strengths.append("WR1")
        elif rcy >= 600: strengths.append("Dependable Target")
        if rct >= 8: strengths.append("End Zone Threat")
        if rtg and rtg >= 100: strengths.append("Efficient")
    elif sport_key == "icehockey_nhl":
        g = raw.get("goals", 0)
        a = raw.get("assists", 0)
        pts = raw.get("points", 0)
        pm = raw.get("plusMinus", 0)
        if g >= 30: strengths.append("Sniper")
        elif g >= 20: strengths.append("Goal Scorer")
        if a >= 40: strengths.append("Elite Setup")
        elif a >= 25: strengths.append("Playmaker")
        if pts >= 70: strengths.append("Offensive Star")
        if pm <= -10: weaknesses.append("Liability")
        elif pm >= 15: strengths.append("Defensive Plus")
    elif sport_key == "baseball_mlb":
        avg = raw.get("avg", 0) or raw.get("battingAverage", 0)
        hr = raw.get("homeRuns", 0)
        rbi = raw.get("RBIs", 0) or raw.get("runsBattedIn", 0)
        ops = raw.get("OPS", 0) or raw.get("onBasePlusSlugging", 0)
        era = raw.get("ERA", 0) or raw.get("earnedRunAverage", 0)
        wins = raw.get("wins", 0)
        if avg >= 0.300: strengths.append("Hits for Avg")
        if hr >= 30: strengths.append("Power Hitter")
        elif hr >= 20: strengths.append("Long Ball Threat")
        if rbi >= 90: strengths.append("RBI Machine")
        if ops >= 0.900: strengths.append("Elite Hitter")
        if era and era < 3.0: strengths.append("Ace")
        elif era and era > 4.5: weaknesses.append("High ERA")
        if wins >= 15: strengths.append("Workhorse")
    elif sport_key.startswith("soccer_"):
        gls = raw.get("totalGoals", 0) or raw.get("goals", 0)
        ast = raw.get("goalAssists", 0) or raw.get("assists", 0)
        if gls >= 20: strengths.append("Top Scorer")
        elif gls >= 10: strengths.append("Goal Threat")
        if ast >= 10: strengths.append("Creator")
        elif ast >= 5: strengths.append("Setup Artist")
        if (position or "").upper() in ("GK", "G"): strengths.append("Shot Stopper")
    return strengths[:3], weaknesses[:2]


def _player_impact_score_from_raw(sport_key: str, raw: dict) -> float:
    if not raw:
        return 0.0
    if sport_key == "basketball_nba":
        return (raw.get("pointsPerGame", 0) * 1.0 +
                raw.get("reboundsPerGame", 0) * 0.7 +
                raw.get("assistsPerGame", 0) * 1.0)
    if sport_key in ("americanfootball_nfl", "americanfootball_ncaaf"):
        return (raw.get("passingYards", 0) * 0.001 +
                raw.get("passingTouchdowns", 0) * 1.0 +
                raw.get("rushingYards", 0) * 0.005 +
                raw.get("rushingTouchdowns", 0) * 1.0 +
                raw.get("receivingYards", 0) * 0.005 +
                raw.get("receivingTouchdowns", 0) * 1.0)
    if sport_key == "icehockey_nhl":
        return raw.get("goals", 0) * 1.0 + raw.get("assists", 0) * 0.7 + raw.get("points", 0) * 0.5
    if sport_key == "baseball_mlb":
        return (raw.get("homeRuns", 0) * 1.5 +
                (raw.get("RBIs", 0) or raw.get("runsBattedIn", 0)) * 0.3 +
                (raw.get("OPS", 0) or raw.get("onBasePlusSlugging", 0)) * 5)
    if sport_key.startswith("soccer_"):
        return ((raw.get("totalGoals", 0) or raw.get("goals", 0)) * 2.0 +
                (raw.get("goalAssists", 0) or raw.get("assists", 0)) * 1.5)
    return 0.0


def _refresh_team_info_for_sport(sport_key: str) -> int:
    """Pull fresh team standings + records for a sport, then top players via
    scoreboard leaders extraction (much faster + more reliable than
    per-athlete stats endpoints, which return only metadata).
    """
    if sport_key not in ESPN_LEAGUE_MAP:
        return 0
    teams = fetch_espn_teams(sport_key)
    if not teams:
        return 0
    n = _store_team_info(teams)
    # Pull top players via scoreboard leaders aggregation
    try:
        leaders = fetch_espn_team_leaders(sport_key, days_back=21)
        all_player_rows: list[dict] = []
        for team_norm, rows in leaders.items():
            all_player_rows.extend(rows)
        if all_player_rows:
            written = _store_player_info(all_player_rows)
            log.debug("team_info %s: %d teams, %d players", sport_key, n, written)
    except Exception as e:
        log.debug("leaders refresh %s: %s", sport_key, e)
    return n


def _backfill_all_team_info():
    """Backfill team_info + player_info for every supported sport."""
    total = 0
    for sport_key in ESPN_LEAGUE_MAP:
        try:
            n = _refresh_team_info_for_sport(sport_key)
            total += n
            print(f"  team_info: {sport_key} +{n} teams", flush=True)
        except Exception as e:
            print(f"  team_info: {sport_key} error: {e}", flush=True)
    print(f"team_info backfill complete: {total} team rows", flush=True)
    return total


# ---------------------------------------------------------------------------
# Top Polymarket traders — fetcher + cache layer
# ---------------------------------------------------------------------------
# Pulls the top N wallets by lifetime volume from the Polymarket leaderboard,
# then walks each wallet's recent trades from the data API and aggregates
# net positions per (conditionId, outcome). The aggregated rows are upserted
# into top_trader_positions, indexed by condition_id so that comparisons
# can be joined in O(1) when we build the dashboard payload.

def fetch_top_polymarket_traders(limit: int = TOP_TRADERS_LIMIT) -> list[dict]:
    """Fetch the top N traders by lifetime volume from the Polymarket
    leaderboard API. Returns a list of {wallet, pseudonym, name, profile_image,
    lifetime_volume, rank}.
    """
    session = _make_http_session()
    url = f"{POLY_LB_API_HOST}/volume"
    params = {"window": "all", "limit": limit}
    try:
        resp = session.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.debug("top traders fetch error: %s", e)
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for i, row in enumerate(data[:limit]):
        wallet = (row.get("proxyWallet") or row.get("wallet") or "").lower()
        if not wallet:
            continue
        out.append({
            "wallet": wallet,
            "pseudonym": row.get("pseudonym") or "",
            "name": row.get("name") or "",
            "profile_image": row.get("profileImage") or "",
            "lifetime_volume": float(row.get("amount") or 0),
            "rank": i + 1,
        })
    return out


def fetch_trader_trades(wallet: str, limit: int = TOP_TRADER_TRADES_LIMIT) -> list[dict]:
    """Fetch recent trades for a single wallet from the Polymarket data API.
    Returns the raw trade list (each trade has conditionId, outcome, side,
    size, price, title, slug, timestamp, etc.).
    """
    if not wallet:
        return []
    session = _make_http_session()
    url = f"{POLY_DATA_API_HOST}/trades"
    params = {"user": wallet, "limit": limit}
    try:
        resp = session.get(url, params=params, timeout=25)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.debug("trader trades fetch %s error: %s", wallet[:8], e)
        return []
    if not isinstance(data, list):
        return []
    return data


def _aggregate_positions_by_market(trades: list[dict]) -> list[dict]:
    """Group trades by (conditionId, outcome) and compute net position.

    For each (market, outcome) pair, sums BUY size minus SELL size, computes
    weighted-average buy price, captures the latest trade side + timestamp,
    and counts trades. Returns a list of position dicts ready to upsert into
    top_trader_positions.
    """
    if not trades:
        return []
    grouped: dict[tuple[str, str], dict] = {}
    for t in trades:
        cid = t.get("conditionId") or ""
        if not cid:
            continue
        outcome = t.get("outcome") or ""
        key = (cid, outcome)
        try:
            size = float(t.get("size") or 0)
            price = float(t.get("price") or 0)
        except (ValueError, TypeError):
            continue
        if size <= 0:
            continue
        side = (t.get("side") or "").upper()
        signed_size = size if side == "BUY" else -size
        try:
            ts = int(t.get("timestamp") or 0)
        except (ValueError, TypeError):
            ts = 0
        bucket = grouped.setdefault(key, {
            "condition_id": cid,
            "outcome": outcome,
            "outcome_index": int(t.get("outcomeIndex") or 0),
            "slug": t.get("slug") or "",
            "title": t.get("title") or "",
            "net_size": 0.0,
            "net_usd": 0.0,
            "buy_size_total": 0.0,
            "buy_usd_total": 0.0,
            "last_side": "",
            "last_traded_ts": 0,
            "trade_count": 0,
        })
        bucket["net_size"] += signed_size
        bucket["net_usd"] += signed_size * price
        if side == "BUY":
            bucket["buy_size_total"] += size
            bucket["buy_usd_total"] += size * price
        bucket["trade_count"] += 1
        if ts > bucket["last_traded_ts"]:
            bucket["last_traded_ts"] = ts
            bucket["last_side"] = side
    out: list[dict] = []
    for bucket in grouped.values():
        buy_size = bucket.pop("buy_size_total")
        buy_usd = bucket.pop("buy_usd_total")
        avg_price = (buy_usd / buy_size) if buy_size > 0 else 0.0
        bucket["avg_price"] = round(avg_price, 4)
        bucket["net_size"] = round(bucket["net_size"], 4)
        bucket["net_usd"] = round(bucket["net_usd"], 2)
        out.append(bucket)
    return out


def _store_top_trader_positions(trader: dict, positions: list[dict]) -> int:
    """Upsert a list of aggregated positions for one trader into
    top_trader_positions. Existing rows for the same wallet that are no
    longer present in the new payload are deleted, so closed/expired
    positions don't linger in the cache.
    """
    if not trader:
        return 0
    wallet = trader["wallet"]
    written = 0
    with _get_db() as conn:
        new_keys = {(p["condition_id"], p.get("outcome", "")) for p in positions}
        existing = conn.execute(
            "SELECT condition_id, outcome FROM top_trader_positions WHERE wallet = ?",
            (wallet,),
        ).fetchall()
        for row in existing:
            key = (row["condition_id"], row["outcome"] or "")
            if key not in new_keys:
                conn.execute(
                    "DELETE FROM top_trader_positions WHERE wallet = ? AND condition_id = ? AND outcome = ?",
                    (wallet, key[0], key[1]),
                )
        for pos in positions:
            try:
                conn.execute(
                    """INSERT INTO top_trader_positions
                       (wallet, pseudonym, name, profile_image, rank, lifetime_volume,
                        condition_id, slug, title, outcome, outcome_index,
                        net_size, net_usd, avg_price, last_side, last_traded_ts,
                        trade_count, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                       ON CONFLICT(wallet, condition_id, outcome) DO UPDATE SET
                         pseudonym = excluded.pseudonym,
                         name = excluded.name,
                         profile_image = excluded.profile_image,
                         rank = excluded.rank,
                         lifetime_volume = excluded.lifetime_volume,
                         slug = excluded.slug,
                         title = excluded.title,
                         outcome_index = excluded.outcome_index,
                         net_size = excluded.net_size,
                         net_usd = excluded.net_usd,
                         avg_price = excluded.avg_price,
                         last_side = excluded.last_side,
                         last_traded_ts = excluded.last_traded_ts,
                         trade_count = excluded.trade_count,
                         fetched_at = datetime('now')""",
                    (
                        wallet,
                        trader.get("pseudonym", ""),
                        trader.get("name", ""),
                        trader.get("profile_image", ""),
                        int(trader.get("rank") or 0),
                        float(trader.get("lifetime_volume") or 0),
                        pos["condition_id"],
                        pos.get("slug", ""),
                        pos.get("title", ""),
                        pos.get("outcome", ""),
                        int(pos.get("outcome_index") or 0),
                        float(pos.get("net_size") or 0),
                        float(pos.get("net_usd") or 0),
                        float(pos.get("avg_price") or 0),
                        pos.get("last_side", ""),
                        int(pos.get("last_traded_ts") or 0),
                        int(pos.get("trade_count") or 0),
                    ),
                )
                written += 1
            except Exception as e:
                log.debug("ttp upsert error: %s", e)
    return written


def _refresh_top_trader_positions() -> int:
    """Refresh the cached top-trader positions table.

    Fetches the top N traders by volume, walks each wallet's recent trades,
    aggregates them by (conditionId, outcome) and upserts into the
    top_trader_positions table. Also drops rows for wallets that fell out of
    the top N.
    """
    traders = fetch_top_polymarket_traders(TOP_TRADERS_LIMIT)
    if not traders:
        return 0
    active_wallets = {t["wallet"] for t in traders}
    try:
        with _get_db() as conn:
            existing_wallets = {
                row[0] for row in conn.execute(
                    "SELECT DISTINCT wallet FROM top_trader_positions"
                ).fetchall()
            }
            for w in existing_wallets - active_wallets:
                conn.execute("DELETE FROM top_trader_positions WHERE wallet = ?", (w,))
    except Exception as e:
        log.debug("ttp prune error: %s", e)
    total_written = 0
    for trader in traders:
        try:
            trades = fetch_trader_trades(trader["wallet"], TOP_TRADER_TRADES_LIMIT)
            positions = _aggregate_positions_by_market(trades)
            # Filter out fully-closed positions (net_size near zero)
            positions = [p for p in positions if abs(p["net_size"]) > 0.5]
            n = _store_top_trader_positions(trader, positions)
            total_written += n
        except Exception as e:
            log.debug("ttp refresh wallet %s error: %s", trader["wallet"][:8], e)
    print(f"top_trader_positions: refreshed {len(traders)} traders, {total_written} positions", flush=True)
    return total_written


def _attach_top_traders_to_comparisons(comparisons: list[dict]) -> None:
    """Annotate each comparison with `top_trader_positions` and `has_top_trader`.
    Single SQL query batches the lookup for every condition_id in the list.
    """
    if not comparisons:
        return
    cids = [c.get("condition_id") for c in comparisons if c.get("condition_id")]
    if not cids:
        for c in comparisons:
            c["top_trader_positions"] = []
            c["has_top_trader"] = False
        return
    placeholders = ",".join("?" for _ in cids)
    lookup: dict[str, list[dict]] = {}
    try:
        with _get_db() as conn:
            rows = conn.execute(
                f"""SELECT wallet, pseudonym, name, profile_image, rank, lifetime_volume,
                           condition_id, outcome, outcome_index, net_size, net_usd,
                           avg_price, last_side, last_traded_ts, trade_count
                    FROM top_trader_positions
                    WHERE condition_id IN ({placeholders})
                    ORDER BY rank ASC, ABS(net_usd) DESC""",
                cids,
            ).fetchall()
    except Exception as e:
        log.debug("ttp attach query error: %s", e)
        rows = []
    for r in rows:
        d = dict(r)
        cid = d["condition_id"]
        lookup.setdefault(cid, []).append({
            "wallet": d["wallet"],
            "pseudonym": d.get("pseudonym") or "",
            "name": d.get("name") or "",
            "profile_image": d.get("profile_image") or "",
            "rank": int(d.get("rank") or 0),
            "lifetime_volume": float(d.get("lifetime_volume") or 0),
            "outcome": d.get("outcome") or "",
            "outcome_index": int(d.get("outcome_index") or 0),
            "net_size": float(d.get("net_size") or 0),
            "net_usd": float(d.get("net_usd") or 0),
            "avg_price": float(d.get("avg_price") or 0),
            "last_side": d.get("last_side") or "",
            "last_traded_ts": int(d.get("last_traded_ts") or 0),
            "trade_count": int(d.get("trade_count") or 0),
            "side": "LONG" if float(d.get("net_size") or 0) >= 0 else "SHORT",
        })
    for c in comparisons:
        cid = c.get("condition_id") or ""
        positions = lookup.get(cid, [])
        c["top_trader_positions"] = positions
        c["has_top_trader"] = bool(positions)


def _get_team_info(sport_key: str, team_name: str) -> dict | None:
    if not team_name:
        return None
    norm = normalize_name(team_name)
    try:
        with _get_db() as conn:
            row = conn.execute(
                "SELECT * FROM sports_team_info WHERE sport = ? AND team_norm = ?",
                (sport_key, norm),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    r = dict(row)
    games = r["wins"] + r["losses"] + (r.get("draws") or 0)
    win_pct = r["win_pct"] if r["win_pct"] else (r["wins"] / games if games else 0)
    return {
        "name": r["team_name"],
        "abbreviation": r.get("abbreviation") or "",
        "wins": r["wins"],
        "losses": r["losses"],
        "draws": r.get("draws") or 0,
        "win_pct": round(win_pct, 3),
        "points_for": r.get("points_for") or 0,
        "points_against": r.get("points_against") or 0,
        "rank": r.get("rank") or 0,
        "conference_rank": r.get("conference_rank") or 0,
        "streak": r.get("streak") or "",
        "last_10": r.get("last_10") or "",
        "logo_url": r.get("logo_url") or "",
    }


def _get_top_players(sport_key: str, team_name: str, limit: int = 3) -> list[dict]:
    if not team_name:
        return []
    norm = normalize_name(team_name)
    try:
        with _get_db() as conn:
            rows = conn.execute(
                """SELECT player_name, position, jersey, stats_json, strengths, weaknesses, impact_score
                   FROM sports_player_info
                   WHERE sport = ? AND team_norm = ?
                   ORDER BY impact_score DESC
                   LIMIT ?""",
                (sport_key, norm, limit),
            ).fetchall()
    except Exception:
        return []
    out: list[dict] = []
    for row in rows:
        r = dict(row)
        try:
            stats = json.loads(r.get("stats_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            stats = {}
        try:
            strengths = json.loads(r.get("strengths") or "[]")
        except (json.JSONDecodeError, TypeError):
            strengths = []
        try:
            weaknesses = json.loads(r.get("weaknesses") or "[]")
        except (json.JSONDecodeError, TypeError):
            weaknesses = []
        out.append({
            "name": r["player_name"],
            "position": r.get("position") or "",
            "jersey": r.get("jersey") or "",
            "stats": stats,
            "strengths": strengths,
            "weaknesses": weaknesses,
            "impact": round(float(r.get("impact_score") or 0), 1),
        })
    return out


def _compute_chemistry(sport_key: str, team: str, form: dict | None, info: dict | None) -> dict | None:
    """Composite 'chemistry' score 0-100. Inputs:
       - Recent form consistency (low variance in W/L = better chemistry)
       - Win % from team_info
       - Streak direction (positive streak = momentum)
    Returns dict with score, label, components.
    """
    if not form and not info:
        return None
    components: dict[str, float] = {}
    score = 50.0  # neutral baseline

    # 1) Recent form consistency (40 points possible)
    if form:
        results = form.get("results") or []
        if results:
            wins = sum(1 for r in results if r == "W")
            recent_win_rate = wins / len(results)
            # 5-0 = +30, 3-2 = neutral, 0-5 = -30
            consistency_pts = (recent_win_rate - 0.5) * 60
            components["recent_form"] = round(consistency_pts, 1)
            score += consistency_pts

    # 2) Season win pct (30 points possible)
    if info:
        win_pct = info.get("win_pct", 0)
        if win_pct > 0:
            season_pts = (win_pct - 0.5) * 60
            components["season_pct"] = round(season_pts, 1)
            score += season_pts

    # 3) Active streak (20 points possible)
    if info and info.get("streak"):
        try:
            s = int(info["streak"])
            streak_pts = max(-20, min(20, s * 4))
            components["streak"] = round(streak_pts, 1)
            score += streak_pts
        except (ValueError, TypeError):
            pass

    score = max(0, min(100, score))
    if score >= 75:
        label = "Excellent"
    elif score >= 60:
        label = "Strong"
    elif score >= 40:
        label = "Average"
    elif score >= 25:
        label = "Struggling"
    else:
        label = "Poor"
    return {
        "score": round(score, 1),
        "label": label,
        "components": components,
    }


# ---------------------------------------------------------------------------
# Line movement: compute trend from recent snapshots
# ---------------------------------------------------------------------------

def _compute_edge_trends(comparisons: list[dict]) -> dict:
    """For each event+outcome, compute trend direction from recent snapshots.

    The previous implementation issued one SELECT per (event, outcome) which
    blew up to N*M queries for large slates. We now prefetch the last 24h of
    snapshots in a single query and group them in-memory.
    """
    trends = {}
    cutoff_2h = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    with _get_db() as conn:
        all_rows = conn.execute(
            "SELECT event_name, outcome, divergence, snapshot_at "
            "FROM sports_market_snapshots WHERE snapshot_at >= ? ORDER BY snapshot_at",
            (cutoff_24h,),
        ).fetchall()

    # Group rows by (event, outcome). dict-of-list keeps insertion order so the
    # rows stay sorted by snapshot_at thanks to the ORDER BY above.
    snap_by_key: dict[tuple[str, str], list] = {}
    for r in all_rows:
        snap_by_key.setdefault((r["event_name"], r["outcome"]), []).append(r)

    for comp in comparisons:
        event_name = comp.get("home_team", "") + (
            " vs " + comp.get("away_team", "") if comp.get("away_team") else ""
        )
        for oc in comp.get("outcomes", []):
            outcome = oc.get("outcome", "")
            key = f"{event_name}|{outcome}"
            rows = snap_by_key.get((event_name, outcome), [])

            if len(rows) < 2:
                trends[key] = {"direction": "new", "change_2h": 0, "change_24h": 0, "points": []}
                continue

            points = [(r["divergence"] or 0) for r in rows]
            current = oc.get("divergence", 0) or 0

            # 2h trend
            recent_rows = [r for r in rows if r["snapshot_at"] >= cutoff_2h]
            if recent_rows:
                change_2h = round(current - (recent_rows[0]["divergence"] or 0), 2)
            else:
                change_2h = 0

            # 24h trend
            change_24h = round(current - (rows[0]["divergence"] or 0), 2)

            if abs(change_2h) < 0.5:
                direction = "stable"
            elif change_2h > 0:
                direction = "widening"  # edge is growing
            else:
                direction = "narrowing"  # edge is closing

            # Keep last 12 points for sparkline
            trends[key] = {
                "direction": direction,
                "change_2h": change_2h,
                "change_24h": change_24h,
                "points": points[-12:],
            }

    return trends


# ---------------------------------------------------------------------------
# Alerts: send notifications for new edges
# ---------------------------------------------------------------------------

def _send_alerts(sport: str, signals: list[dict]):
    """Send Telegram/webhook alerts for new edge signals."""
    if not signals:
        return
    with _get_db() as conn:
        configs = conn.execute(
            "SELECT * FROM sports_alert_config WHERE enabled = 1"
        ).fetchall()

    for cfg in configs:
        cfg = dict(cfg)
        min_edge = cfg.get("min_edge", 5.0)
        # Defensive: a single corrupt row should not block alerts to every
        # other user. Fall back to "all sports allowed" on parse failure.
        try:
            allowed_sports = json.loads(cfg.get("sports", "[]"))
            if not isinstance(allowed_sports, list):
                allowed_sports = []
        except (json.JSONDecodeError, TypeError):
            allowed_sports = []
        if allowed_sports and sport not in allowed_sports:
            continue

        # Check cooldown (max 1 alert per 5 min per user)
        last = cfg.get("last_alert_at", "")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                # Datetime may be naive if older code wrote it without tz info
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last_dt).total_seconds() < 300:
                    continue
            except (ValueError, TypeError):
                pass

        # Filter signals above user's min_edge
        user_signals = []
        for s in signals:
            max_div = s.get("max_divergence", 0)
            if max_div >= min_edge:
                user_signals.append(s)
        if not user_signals:
            continue

        # Build message
        lines = [f"Sharpe Alert: {len(user_signals)} edge(s) in {SPORTS.get(sport, sport)}"]
        for s in user_signals[:5]:
            best = max(s.get("outcomes", [{}]), key=lambda o: abs(o.get("divergence_pct", 0) or 0), default={})
            lines.append(
                f"  {s.get('home_team','')} vs {s.get('away_team','')}: "
                f"{best.get('outcome_name','')} {best.get('divergence_pct',0):+.1f}%"
            )
        if len(user_signals) > 5:
            lines.append(f"  ...and {len(user_signals) - 5} more")
        msg = "\n".join(lines)

        # Send Telegram (decrypt + validate token format to prevent SSRF)
        tg_token = _decrypt_field(cfg.get("telegram_bot_token", ""))
        tg_chat = cfg.get("telegram_chat_id", "")
        if tg_token and tg_chat:
            if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", tg_token):
                log.warning("Telegram token rejected: invalid format")
            else:
                try:
                    requests.post(
                        f"https://api.telegram.org/bot{tg_token}/sendMessage",
                        json={"chat_id": tg_chat, "text": msg},
                        timeout=10,
                    )
                except Exception as e:
                    log.warning("Telegram alert error: %s", e)

        # Send webhook — signed with the user's HMAC key if configured.
        # _signed_webhook_post re-validates the URL at dispatch (DNS
        # rebinding SSRF guard) and handles JSON serialization itself.
        webhook_url = cfg.get("webhook_url", "")
        if webhook_url:
            key = _get_webhook_signing_key(cfg["user_id"])
            ok = _signed_webhook_post(
                webhook_url,
                {"text": msg, "kind": "broadcast", "signals": user_signals[:5], "sport": sport},
                signing_key=key,
            )
            label = "webhook_signed" if key else "webhook_unsigned"
            M_ALERT_SEND.labels(channel=label, result="ok" if ok else "error").inc()

        # Update last alert time
        with _get_db() as conn:
            conn.execute(
                "UPDATE sports_alert_config SET last_alert_at = ? WHERE user_id = ?",
                (datetime.now(timezone.utc).isoformat(), cfg["user_id"]),
            )


# ---------------------------------------------------------------------------
# Watchlist alerts — per-user-per-market divergence triggers
# ---------------------------------------------------------------------------

WATCHLIST_ALERT_COOLDOWN_SECS = 3600  # one ping per item per hour


def _watchlist_market_key(comp: dict) -> str:
    """Normalize a comparison to the same `market_key` shape the frontend
    POSTs to /api/watchlist. Frontend convention: 'home|away'."""
    home = (comp.get("home_team") or "").strip()
    away = (comp.get("away_team") or "").strip()
    return f"{home}|{away}"


def _send_watchlist_alerts(sport: str, comparisons: list[dict]) -> None:
    """Fire per-user alerts for watchlisted markets where divergence has
    crossed the user's configured threshold (default: alert_threshold_pp
    column on the row; falls back to the user's global min_edge).

    Only triggers for items the user explicitly pinned, so it's
    higher-signal than the broadcast _send_alerts feed.
    """
    if not comparisons:
        return
    by_key: dict[str, dict] = {_watchlist_market_key(c): c for c in comparisons}
    if not by_key:
        return

    with _get_db() as conn:
        rows = conn.execute(
            """SELECT w.id, w.user_id, w.market_key, w.alert_threshold_pp, w.last_alerted_at,
                      a.telegram_bot_token, a.telegram_chat_id, a.webhook_url, a.min_edge
               FROM sports_watchlist w
               LEFT JOIN sports_alert_config a ON a.user_id = w.user_id AND a.enabled = 1"""
        ).fetchall()

    now = datetime.now(timezone.utc)
    for r in rows:
        comp = by_key.get(r["market_key"])
        if not comp:
            continue
        threshold = r["alert_threshold_pp"]
        if threshold is None or threshold <= 0:
            threshold = float(r["min_edge"] or DIVERGENCE_THRESHOLD)
        max_div = float(comp.get("max_divergence") or 0)
        if max_div < threshold:
            continue
        # Cooldown
        last = r["last_alerted_at"] or ""
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if (now - last_dt).total_seconds() < WATCHLIST_ALERT_COOLDOWN_SECS:
                    continue
            except (ValueError, TypeError):
                pass

        best_oc = max(comp.get("outcomes") or [{}],
                      key=lambda o: abs(o.get("divergence_pct", 0) or 0), default={})
        msg = (
            f"Watchlist hit ({SPORTS.get(sport, sport)}): "
            f"{comp.get('home_team', '')} vs {comp.get('away_team', '')} — "
            f"{best_oc.get('outcome_name', '')} {best_oc.get('divergence_pct', 0):+.1f}pp "
            f"(threshold {threshold:.1f}pp)"
        )
        trade_url = comp.get("trade_poly_url")
        if trade_url:
            msg += f"\n{trade_url}"

        # Telegram
        tg_token = _decrypt_field(r["telegram_bot_token"] or "")
        tg_chat = r["telegram_chat_id"] or ""
        if tg_token and tg_chat and re.fullmatch(r"\d+:[A-Za-z0-9_-]+", tg_token):
            try:
                requests.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={"chat_id": tg_chat, "text": msg},
                    timeout=10,
                )
                M_ALERT_SEND.labels(channel="telegram_watchlist", result="ok").inc()
            except Exception as e:
                M_ALERT_SEND.labels(channel="telegram_watchlist", result="error").inc()
                log.warning("Watchlist Telegram error: %s", e)

        # Webhook — signed if user has set a key.
        webhook_url = r["webhook_url"] or ""
        if webhook_url:
            key = _get_webhook_signing_key(r["user_id"])
            ok = _signed_webhook_post(
                webhook_url,
                {"text": msg, "kind": "watchlist", "sport": sport,
                 "comparison": comp, "threshold_pp": threshold},
                signing_key=key,
            )
            label = "webhook_watchlist_signed" if key else "webhook_watchlist"
            M_ALERT_SEND.labels(channel=label, result="ok" if ok else "error").inc()

        with _get_db() as conn:
            conn.execute(
                "UPDATE sports_watchlist SET last_alerted_at = ? WHERE id = ?",
                (now.isoformat(), r["id"]),
            )


# ---------------------------------------------------------------------------
# Rule-based alerts — structured per-user alert rules
# ---------------------------------------------------------------------------
#
# Each user can create multiple alert rules. A rule is a set of structured
# filters (sports, market types, min divergence, min volume, time-to-event,
# quality gates) plus a channel and a cooldown. When the poll loop produces
# new signals, _eval_alert_rules walks each enabled rule and fires alerts
# for any signal that passes the filters, respecting cooldown + quiet hours.


def _rule_quiet_hours_active(rule: dict, now: datetime) -> bool:
    """Return True iff the rule's quiet hours window contains `now` (UTC)."""
    start = rule.get("quiet_hours_start")
    end = rule.get("quiet_hours_end")
    if start is None or end is None:
        return False
    try:
        start_h = int(start)
        end_h = int(end)
    except (TypeError, ValueError):
        return False
    h = now.hour
    if start_h <= end_h:
        return start_h <= h < end_h
    # Wraps midnight, e.g. quiet 22:00 -> 07:00
    return h >= start_h or h < end_h


def _signal_matches_rule(signal: dict, sport: str, rule: dict) -> bool:
    """Check whether a comparison-shaped signal satisfies the rule filters.

    Filters are conjunctive: every set field must match. JSON-encoded list
    fields (sports, market_types) of [] mean "any".
    """
    # sports allowlist
    try:
        sports = json.loads(rule.get("sports") or "[]")
    except (TypeError, ValueError):
        sports = []
    if sports and sport not in sports:
        return False

    # market_types allowlist — the comparison may carry per-outcome market_type
    # or a comparison-level "market_type". For h2h+spreads+totals we set the
    # comparison's market_type in the data_updater; futures and esports tag
    # is_futures/is_esport. Map both ways.
    try:
        market_types = json.loads(rule.get("market_types") or "[]")
    except (TypeError, ValueError):
        market_types = []
    if market_types:
        signal_mtype = signal.get("market_type", "h2h")
        if signal_mtype not in market_types:
            return False

    # Divergence floor
    min_div = float(rule.get("min_divergence_pp") or 0.0)
    if float(signal.get("max_divergence") or 0.0) < min_div:
        return False

    # Volume floor
    min_vol = rule.get("min_volume")
    if min_vol is not None:
        try:
            if float(signal.get("poly_volume") or 0.0) < float(min_vol):
                return False
        except (TypeError, ValueError):
            pass

    # Time-to-event window
    max_hours = rule.get("max_time_to_event_hours")
    if max_hours is not None:
        tth = signal.get("time_to_event_hours")
        # Skip rule when we have no commence_time to compare against
        if tth is None:
            return False
        try:
            if float(tth) > float(max_hours):
                return False
        except (TypeError, ValueError):
            pass

    # Quality gates — applied per-outcome since the comparison may have
    # multiple outcomes with different gate states. Require at least one
    # outcome that passes the required gates AND fires the signal.
    outcomes = signal.get("outcomes") or []
    if not outcomes:
        return False
    require_sharp = bool(rule.get("require_sharp_consensus", 1))
    require_stale = bool(rule.get("require_not_stale", 1))
    require_liq = bool(rule.get("require_liquidity_ok", 1))
    for oc in outcomes:
        if not oc.get("is_signal"):
            continue
        if require_sharp and not oc.get("sharp_consensus_ok", True):
            continue
        if require_stale and not oc.get("not_stale", True):
            continue
        if require_liq and not oc.get("liquidity_ok", True):
            continue
        return True
    return False


# ── Web Push (VAPID) ────────────────────────────────────────────────────────
# Set VAPID_PUBLIC_KEY (base64url) + VAPID_PRIVATE_KEY + VAPID_SUBJECT
# (mailto:...) to enable push. Generate keys with: pywebpush vapid_key.
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:admin@narve.ai")

# Lazy import — pywebpush is optional. Without it, /api/push/subscribe
# still works (subscriptions are stored), but actual push delivery is a no-op.
try:
    from pywebpush import webpush as _webpush, WebPushException as _WebPushException
    _PUSH_AVAILABLE = bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)
except ImportError:
    _webpush = None
    _WebPushException = Exception
    _PUSH_AVAILABLE = False


def _send_web_push(user_id: str, payload: dict) -> int:
    """Push `payload` to every registered subscription for user_id.

    Returns count of successful deliveries. Removes dead subscriptions
    (410 Gone) from the DB so we don't keep retrying them forever.
    """
    if not _PUSH_AVAILABLE or not _webpush:
        return 0
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT id, endpoint, p256dh, auth FROM sports_push_subscriptions WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    if not rows:
        return 0
    delivered = 0
    dead_ids: list[int] = []
    for r in rows:
        try:
            _webpush(
                subscription_info={
                    "endpoint": r["endpoint"],
                    "keys": {"p256dh": r["p256dh"], "auth": r["auth"]},
                },
                data=json.dumps(payload),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=300,
            )
            delivered += 1
            M_ALERT_SEND.labels(channel="webpush", result="ok").inc()
        except _WebPushException as e:
            M_ALERT_SEND.labels(channel="webpush", result="error").inc()
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 410:  # Gone — subscription is permanently dead
                dead_ids.append(r["id"])
            else:
                log.warning("Web push error (status=%s): %s", status, e)
        except Exception as e:
            M_ALERT_SEND.labels(channel="webpush", result="error").inc()
            log.warning("Web push generic error: %s", e)
    if dead_ids:
        with _get_db() as conn:
            placeholders = ",".join("?" for _ in dead_ids)
            conn.execute(
                f"DELETE FROM sports_push_subscriptions WHERE id IN ({placeholders})",
                dead_ids,
            )
    if delivered:
        with _get_db() as conn:
            conn.execute(
                "UPDATE sports_push_subscriptions SET last_pushed_at = ? WHERE user_id = ?",
                (datetime.now(timezone.utc).isoformat(), user_id),
            )
    return delivered


def _send_alert_to_channel(rule: dict, msg: str, payload: dict) -> None:
    """Dispatch one alert via the rule's configured channel(s)."""
    user_id = rule.get("user_id")
    if not user_id:
        return
    # Look up the user's stored credentials in sports_alert_config — we
    # reuse the existing single-row config for Telegram/webhook secrets
    # rather than duplicating them per rule.
    with _get_db() as conn:
        cfg = conn.execute(
            "SELECT telegram_chat_id, telegram_bot_token, webhook_url "
            "FROM sports_alert_config WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not cfg:
        cfg = {}
    else:
        cfg = dict(cfg)
    channel = (rule.get("channel") or "telegram").lower()

    if channel in ("telegram", "both"):
        tg_token = _decrypt_field(cfg.get("telegram_bot_token", "") or "")
        tg_chat = cfg.get("telegram_chat_id", "") or ""
        if tg_token and tg_chat and re.fullmatch(r"\d+:[A-Za-z0-9_-]+", tg_token):
            try:
                requests.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={"chat_id": tg_chat, "text": msg},
                    timeout=10,
                )
                M_ALERT_SEND.labels(channel="telegram_rule", result="ok").inc()
            except Exception as e:
                M_ALERT_SEND.labels(channel="telegram_rule", result="error").inc()
                log.warning("Rule alert (telegram) error: %s", e)

    if channel in ("webhook", "both"):
        webhook_url = cfg.get("webhook_url", "") or ""
        if webhook_url:
            key = _get_webhook_signing_key(user_id)
            ok = _signed_webhook_post(
                webhook_url,
                {"text": msg, "kind": "rule", "rule_id": rule.get("id"), **payload},
                signing_key=key,
            )
            label = "webhook_rule_signed" if key else "webhook_rule"
            M_ALERT_SEND.labels(channel=label, result="ok" if ok else "error").inc()

    if channel in ("push", "both"):
        push_payload = {
            "title": f"Sharpe — {len(payload.get('signals', []))} signal(s)",
            "body": msg.split("\n", 1)[0] if msg else "New +EV signal",
            "tag": f"rule-{rule.get('id', '')}",
            "data": {"url": "/", "rule_id": rule.get("id")},
        }
        try:
            _send_web_push(user_id, push_payload)
        except Exception as e:
            log.warning("Web push dispatch error: %s", e)


def _eval_alert_rules(sport: str, signals: list[dict]) -> None:
    """For each enabled rule, find matching signals and fire alerts."""
    if not signals:
        return
    with _get_db() as conn:
        rules = conn.execute(
            "SELECT * FROM sports_alert_rules WHERE enabled = 1"
        ).fetchall()

    now = datetime.now(timezone.utc)
    for r in rules:
        rule = dict(r)
        # Quiet hours skip
        if _rule_quiet_hours_active(rule, now):
            continue
        # Cooldown
        cooldown = int(rule.get("cooldown_secs") or 300)
        last = rule.get("last_fired_at") or ""
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if (now - last_dt).total_seconds() < cooldown:
                    continue
            except (ValueError, TypeError):
                pass

        matching = [s for s in signals if _signal_matches_rule(s, sport, rule)]
        if not matching:
            continue

        # Build a compact message — top 5 matches
        sport_label = SPORTS.get(sport, sport)
        lines = [f"[{rule.get('name') or 'rule #' + str(rule['id'])}] "
                 f"{len(matching)} signal(s) in {sport_label}"]
        for s in matching[:5]:
            best = max(s.get("outcomes") or [{}],
                       key=lambda o: abs(o.get("divergence_pct", 0) or 0), default={})
            lines.append(
                f"  {s.get('home_team','')} vs {s.get('away_team','')}: "
                f"{best.get('outcome_name','')} {best.get('divergence_pct', 0):+.1f}pp"
            )
        if len(matching) > 5:
            lines.append(f"  ...and {len(matching) - 5} more")
        msg = "\n".join(lines)
        _send_alert_to_channel(rule, msg, {"signals": matching[:5], "sport": sport})

        with _get_db() as conn:
            conn.execute(
                "UPDATE sports_alert_rules SET last_fired_at = ? WHERE id = ?",
                (now.isoformat(), rule["id"]),
            )


# ---------------------------------------------------------------------------
# Polymarket WebSocket subscriber — live price feed
# ---------------------------------------------------------------------------
#
# Polymarket exposes a public WebSocket at
#   wss://ws-subscriptions-clob.polymarket.com/ws/market
# Send {"type": "MARKET", "assets_ids": [...]} to subscribe; the server pushes
# price_change / tick_size_change / last_trade_price events for those assets.
#
# We use this to:
#   1. Drop dashboard time-to-signal from one poll interval (~30s to 5 min)
#      down to ~1-2s — whenever a subscribed asset's price moves, the
#      in-memory comparison list is updated immediately.
#   2. Push real-time deltas to every connected dashboard WS client so the
#      browser updates without a refetch.
#
# Subscription set = union of poly_token_id across the current comparison
# set, capped at PM_WS_MAX_SUBSCRIPTIONS to keep the feed manageable. When
# the set changes, we close the connection — the loop reconnects and
# resubscribes.

PM_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PM_WS_MAX_SUBSCRIPTIONS = 200
PM_WS_PRICE_FRESH_SECONDS = 90  # ignore live prices older than this

_LIVE_POLY_PRICES: dict[str, dict] = {}  # asset_id -> {"price": float, "ts": float}

# Ring buffer of recent large Polymarket fills. Pros watch this for
# real-time conviction signals — when a $50k+ buy hits a market, that's
# information OddsJam-class tools don't surface for sports.
PM_FILL_BUFFER_MAX = 500
PM_FILL_MIN_USD = float(os.getenv("PM_FILL_MIN_USD", "1000"))
_LIVE_POLY_FILLS: list[dict] = []
_LIVE_POLY_FILLS_LOCK = threading.Lock()
_PM_WS_DESIRED_TOKENS: set[str] = set()
_pm_ws_reconnect_event: asyncio.Event | None = None  # set on startup


def _collect_poly_token_ids(comparisons: list[dict]) -> set[str]:
    """Pull the union of poly_token_id values out of a comparison list."""
    out: set[str] = set()
    for c in comparisons or []:
        for oc in c.get("outcomes") or []:
            tid = oc.get("poly_token_id")
            if tid:
                out.add(tid)
    return out


def _update_pm_ws_subscriptions(comparisons: list[dict]) -> None:
    """Recompute the desired subscription set after a poll and trigger
    reconnect if it changed. Caps to PM_WS_MAX_SUBSCRIPTIONS by frequency
    of appearance (every subscribed token represents one poll-matched
    market — they're all roughly equal-priority)."""
    global _PM_WS_DESIRED_TOKENS
    tokens = _collect_poly_token_ids(comparisons)
    if len(tokens) > PM_WS_MAX_SUBSCRIPTIONS:
        tokens = set(list(tokens)[:PM_WS_MAX_SUBSCRIPTIONS])
    if tokens != _PM_WS_DESIRED_TOKENS:
        _PM_WS_DESIRED_TOKENS = tokens
        if _pm_ws_reconnect_event is not None:
            _pm_ws_reconnect_event.set()


def _apply_live_prices_to_comparisons(comparisons: list[dict]) -> int:
    """For each outcome with a fresh live price, override poly_prob and
    recompute the dependent fields. Returns the count of outcomes updated.

    Called by /api/data so each request returns the freshest possible
    snapshot, and by the WS handler so dashboard_data also stays current.
    """
    now = time.time()
    updated = 0
    for comp in comparisons or []:
        comp_signal_changed = False
        for oc in comp.get("outcomes") or []:
            tid = oc.get("poly_token_id")
            if not tid:
                continue
            live = _LIVE_POLY_PRICES.get(tid)
            if not live:
                continue
            if (now - live["ts"]) > PM_WS_PRICE_FRESH_SECONDS:
                continue
            new_poly_prob = round(float(live["price"]) * 100.0, 2)
            if new_poly_prob == oc.get("poly_prob"):
                continue
            # Update poly side
            sharp_prob = oc.get("sharp_prob") or 0
            # Use de-vigged divergence if we have it; fall back to raw
            # subtraction otherwise. The original signal_quality call ran
            # against the full event consensus, which we no longer have
            # here — we keep the de-vig ratio from the original divergence
            # vs raw divergence and apply the same ratio to the live one.
            old_poly = oc.get("poly_prob") or 0
            old_raw = oc.get("divergence_raw")
            old_dev = oc.get("divergence")
            if old_raw and old_dev and old_raw != 0:
                ratio = old_dev / old_raw  # how much vig got stripped originally
            else:
                ratio = 1.0
            new_raw = round(sharp_prob - new_poly_prob, 2)
            new_dev = round(new_raw * ratio, 2)
            oc["poly_prob"] = new_poly_prob
            oc["poly_price"] = new_poly_prob / 100.0
            oc["divergence_raw"] = new_raw
            oc["divergence"] = new_dev
            oc["divergence_pct"] = new_dev
            oc["abs_divergence"] = round(abs(new_dev), 2)
            oc["cheap_on"] = "Polymarket" if new_dev > 0 else "Bookmaker"
            oc["live_updated_at"] = live["ts"]
            was_signal = bool(oc.get("is_signal"))
            # Re-evaluate the threshold; gate flags carry over from the
            # last full poll since they depend on data not in the WS feed
            # (sharp consensus, liquidity, staleness).
            new_signal = (
                abs(new_dev) >= DIVERGENCE_THRESHOLD
                and bool(oc.get("sharp_consensus_ok"))
                and bool(oc.get("liquidity_ok"))
                and bool(oc.get("not_stale"))
            )
            oc["is_signal"] = new_signal
            updated += 1
            if was_signal != new_signal:
                comp_signal_changed = True
        if comp.get("outcomes"):
            comp["has_signal"] = any(o.get("is_signal") for o in comp["outcomes"])
            comp["max_divergence"] = max(
                (abs(o.get("divergence", 0) or 0) for o in comp["outcomes"]),
                default=0,
            )
        comp["_signal_changed_live"] = comp_signal_changed
    return updated


async def _broadcast_live_update(changed: list[dict]) -> None:
    """Push a delta to all connected dashboard WS clients."""
    if not changed or not connected_ws:
        return
    payload = json.dumps({"type": "live_update", "comparisons": changed})
    dead = []
    for ws in list(connected_ws):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        connected_ws.discard(ws)


async def _handle_pm_ws_message(raw: str) -> None:
    """Parse one WS frame, update live price map, and broadcast deltas
    if any signal state flipped."""
    try:
        data = json.loads(raw)
    except Exception:
        return
    # Some frames are arrays (batch updates), some are single events
    events = data if isinstance(data, list) else [data]
    affected_ids: set[str] = set()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        asset_id = ev.get("asset_id") or ev.get("token_id")
        if not asset_id:
            continue
        # Polymarket emits price_change, tick_size_change, last_trade_price, etc.
        # All of these carry an updated price we want.
        price_raw = ev.get("price") or ev.get("last_trade_price") or ev.get("mid_price")
        if price_raw is None:
            continue
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            continue
        if not (0.0 < price < 1.0):
            continue
        _LIVE_POLY_PRICES[asset_id] = {"price": price, "ts": time.time()}
        affected_ids.add(asset_id)
        M_WS_PRICE_EVENTS.inc()

        # Capture large fills for the live tape. price_change events on
        # the Polymarket WS carry `size` (shares) and `side` (BUY/SELL);
        # USD value = price * size. Only buffer above PM_FILL_MIN_USD so
        # we don't drown in dust trades.
        size_raw = ev.get("size") or ev.get("matched_amount")
        side = (ev.get("side") or "").upper()
        if size_raw is not None and side in ("BUY", "SELL"):
            try:
                size = float(size_raw)
            except (TypeError, ValueError):
                size = 0.0
            usd = price * size
            if usd >= PM_FILL_MIN_USD:
                fill = {
                    "ts": time.time(),
                    "asset_id": asset_id,
                    "price": round(price, 4),
                    "size": round(size, 2),
                    "usd": round(usd, 2),
                    "side": side,
                    "market": ev.get("market") or ev.get("condition_id") or "",
                }
                with _LIVE_POLY_FILLS_LOCK:
                    _LIVE_POLY_FILLS.append(fill)
                    if len(_LIVE_POLY_FILLS) > PM_FILL_BUFFER_MAX:
                        del _LIVE_POLY_FILLS[: -PM_FILL_BUFFER_MAX]
                M_PM_FILLS_CAPTURED.labels(side=side).inc()
    M_WS_LIVE_PRICES.set(len(_LIVE_POLY_PRICES))

    if not affected_ids:
        return

    # Re-apply against current dashboard_data and broadcast any changes
    async with _data_lock:
        comparisons = dashboard_data.get("comparisons") or []
        affected_comps = [
            c for c in comparisons
            if any((oc.get("poly_token_id") in affected_ids)
                   for oc in (c.get("outcomes") or []))
        ]
        if affected_comps:
            _apply_live_prices_to_comparisons(affected_comps)

    if affected_comps:
        await _broadcast_live_update(affected_comps)


# ── Polymarket WS circuit breaker ───────────────────────────────────────────
# After PM_WS_CIRCUIT_THRESHOLD consecutive failures, open the circuit
# and sleep PM_WS_CIRCUIT_OPEN_SECONDS before the next attempt. Open
# duration doubles on each subsequent failed probe, capped at the max.
# This keeps a wedged Polymarket from triggering 1000+ failed connect
# attempts per hour while still recovering automatically.
PM_WS_CIRCUIT_THRESHOLD = 5         # failures before circuit opens
PM_WS_CIRCUIT_OPEN_SECONDS = 300    # 5 min initial cooldown
PM_WS_CIRCUIT_MAX_SECONDS = 3600    # 1 h cap

_pm_ws_failure_count = 0
_pm_ws_circuit_open_seconds = PM_WS_CIRCUIT_OPEN_SECONDS


def _pm_ws_record_failure() -> tuple[int, float]:
    """Increment the failure counter and compute the next sleep.

    Returns (consecutive_failures, sleep_seconds). Below the threshold
    we use short exponential backoff (1s -> 60s). At threshold we open
    the circuit and sleep longer; subsequent failures double the open
    duration up to the cap.
    """
    global _pm_ws_failure_count, _pm_ws_circuit_open_seconds
    _pm_ws_failure_count += 1
    if _pm_ws_failure_count < PM_WS_CIRCUIT_THRESHOLD:
        # Exponential backoff: 1, 2, 4, 8s for failures 1-4
        sleep = min(2.0 ** (_pm_ws_failure_count - 1), 60.0)
    else:
        # Circuit open. Double duration each failure past threshold.
        sleep = _pm_ws_circuit_open_seconds
        _pm_ws_circuit_open_seconds = min(
            _pm_ws_circuit_open_seconds * 2,
            PM_WS_CIRCUIT_MAX_SECONDS,
        )
    M_PM_WS_FAILURES.set(_pm_ws_failure_count)
    return _pm_ws_failure_count, sleep


def _pm_ws_record_success() -> None:
    """Reset the circuit on a successful connection."""
    global _pm_ws_failure_count, _pm_ws_circuit_open_seconds
    _pm_ws_failure_count = 0
    _pm_ws_circuit_open_seconds = PM_WS_CIRCUIT_OPEN_SECONDS
    M_PM_WS_FAILURES.set(0)


async def _polymarket_ws_loop() -> None:
    """Maintain a persistent WS connection. Reconnects with exponential
    backoff on transient failures, then opens a circuit breaker if
    Polymarket stays unreachable to avoid hammering their endpoint."""
    global _pm_ws_reconnect_event
    _pm_ws_reconnect_event = asyncio.Event()
    try:
        import websockets
    except ImportError:
        log.warning("websockets package missing — live PM feed disabled")
        return

    while True:
        if not _PM_WS_DESIRED_TOKENS:
            # Nothing to subscribe to yet — wait for the first poll to populate.
            try:
                await asyncio.wait_for(_pm_ws_reconnect_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
            _pm_ws_reconnect_event.clear()
            continue

        subscribed = set(_PM_WS_DESIRED_TOKENS)  # snapshot for this connection
        try:
            M_WS_RECONNECTS.inc()
            async with websockets.connect(
                PM_WS_URL,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
                max_size=2_000_000,
            ) as ws:
                await ws.send(json.dumps({
                    "type": "MARKET",
                    "assets_ids": list(subscribed),
                }))
                _pm_ws_record_success()
                log.info("Polymarket WS connected, subscribed=%d", len(subscribed))

                # Race: incoming messages vs. reconnect signal
                while True:
                    if _PM_WS_DESIRED_TOKENS != subscribed:
                        log.info("PM WS subscription set changed, reconnecting")
                        break
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        continue  # No traffic for 30s; loop again
                    await _handle_pm_ws_message(msg)
        except Exception as e:
            failures, sleep = _pm_ws_record_failure()
            if failures >= PM_WS_CIRCUIT_THRESHOLD:
                log.warning(
                    "PM WS circuit OPEN after %d failures: %s "
                    "(cooling down %.0fs)",
                    failures, e, sleep,
                )
            else:
                log.warning("PM WS error: %s (retry in %.1fs)", e, sleep)
            try:
                await asyncio.wait_for(_pm_ws_reconnect_event.wait(), timeout=sleep)
                _pm_ws_reconnect_event.clear()
            except asyncio.TimeoutError:
                pass


# ---------------------------------------------------------------------------
# Multi-sport scanning: scan all sports in background
# ---------------------------------------------------------------------------

# Per-sport cache for cross-sport edge feed
_cross_sport_edges: dict[str, list[dict]] = {}  # sport_key -> top edges
_cross_sport_lock = threading.Lock()


def _update_cross_sport_edges(sport: str, comparisons: list[dict]):
    """Update the cross-sport edge cache with top edges for a sport."""
    top = []
    for c in comparisons:
        if not c.get("has_signal"):
            continue
        best = max(c.get("outcomes", [{}]), key=lambda o: abs(o.get("divergence_pct", 0) or 0), default={})
        top.append({
            "sport": sport,
            "sport_name": SPORTS.get(sport, sport),
            "home_team": c.get("home_team", ""),
            "away_team": c.get("away_team", ""),
            "outcome": best.get("outcome_name", ""),
            "divergence": best.get("divergence_pct", 0),
            "kelly_pct": best.get("kelly_pct", 0),
            "confidence": c.get("confidence_score", 0),
            "time_until": c.get("time_until", ""),
            "commence_time": c.get("commence_time", ""),
        })
    top.sort(key=lambda x: -abs(x["divergence"]))
    with _cross_sport_lock:
        _cross_sport_edges[sport] = top[:10]


def _get_all_cross_sport_edges() -> list[dict]:
    """Return top edges across all sports, sorted by divergence."""
    with _cross_sport_lock:
        all_edges = []
        for edges in _cross_sport_edges.values():
            all_edges.extend(edges)
    all_edges.sort(key=lambda x: -abs(x["divergence"]))
    return all_edges[:20]


# ---------------------------------------------------------------------------
# Orderbook depth helper
# ---------------------------------------------------------------------------

def fetch_orderbook_depth(token_id: str) -> dict:
    """Fetch Polymarket orderbook and compute executable depth at mid price."""
    empty = {"bid_depth": 0, "ask_depth": 0, "mid_price": 0, "spread": 0, "executable_size": 0}
    try:
        resp = requests.get(
            f"{POLYMARKET_HOST}/book",
            params={"token_id": token_id},
            timeout=10,
        )
        resp.raise_for_status()
        book = resp.json()
    except Exception:
        return empty

    if not isinstance(book, dict):
        return empty
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not isinstance(bids, list) or not isinstance(asks, list):
        return empty

    # Defensive parse — if shape drift removes "price"/"size" keys, return the
    # empty sentinel rather than 500-ing.
    try:
        best_bid = float(bids[0].get("price", 0)) if bids else 0
        best_ask = float(asks[0].get("price", 0)) if asks else 0

        # Mid price requires both sides; if only one side is present, fall back
        # to that side's best — this preserves correct semantics for one-sided
        # books instead of zeroing out the depth filter.
        if best_bid > 0 and best_ask > 0:
            mid = (best_bid + best_ask) / 2
            spread = best_ask - best_bid
        elif best_bid > 0:
            mid = best_bid
            spread = 0.0
        elif best_ask > 0:
            mid = best_ask
            spread = 0.0
        else:
            return empty

        bid_depth = 0.0
        for b in bids:
            try:
                bp = float(b.get("price", 0))
                bs = float(b.get("size", 0))
            except (TypeError, ValueError):
                continue
            if bp >= mid * 0.98:
                bid_depth += bs

        ask_depth = 0.0
        for a in asks:
            try:
                ap = float(a.get("price", 0))
                asz = float(a.get("size", 0))
            except (TypeError, ValueError):
                continue
            if ap <= mid * 1.02:
                ask_depth += asz
    except Exception:
        return empty

    return {
        "bid_depth": round(bid_depth, 2),
        "ask_depth": round(ask_depth, 2),
        "mid_price": round(mid, 4),
        "spread": round(spread, 4),
        "executable_size": round(min(bid_depth, ask_depth), 2),
    }


_scan_event = asyncio.Event()  # set to trigger immediate re-scan


async def trigger_rescan():
    """Signal the updater to rescan immediately."""
    _scan_event.set()


_poly_cache: dict[str, list[dict]] = {}   # keyed by sport
_poly_cache_time: dict[str, float] = {}   # keyed by sport

_data_lock = asyncio.Lock()  # guards atomic swaps of dashboard_data

# Track background tasks so they aren't garbage-collected mid-execution.
# Without a strong reference, asyncio may collect a running task and silently
# cancel it. Each helper adds itself to the set on creation and removes itself
# on completion via add_done_callback.
_bg_tasks: set[asyncio.Task] = set()


def _spawn_bg(coro) -> asyncio.Task:
    """Create a background task and track it in _bg_tasks.

    Use this instead of bare asyncio.create_task() for fire-and-forget tasks
    so they aren't garbage-collected before they finish.
    """
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


def _build_esports_comparisons(poly_markets: list[dict]) -> list[dict]:
    """Build comparison cards from Polymarket esports markets (no bookmaker odds).

    For esports we don't have bookmaker odds to compare against, so we present
    the Polymarket data as standalone market cards showing prices, volume, etc.
    """
    comparisons = []
    for pm in poly_markets:
        if pm["volume"] <= 0 and pm["liquidity"] <= 0:
            continue

        outcomes = []
        for oname, odata in pm["outcomes"].items():
            prob = odata["implied_prob"]
            if prob <= 0.5 or prob >= 99.5:
                continue
            outcomes.append({
                "outcome": oname,
                "outcome_name": oname,
                "poly_outcome": oname,
                "sharp_prob": prob,  # use poly as the price (no book to compare)
                "consensus_prob": prob,
                "poly_prob": prob,
                "poly_price": prob / 100,
                "kalshi_prob": None,
                "kalshi_ticker": None,
                "kalshi_divergence": None,
                "divergence": 0,
                "divergence_pct": 0,
                "abs_divergence": 0,
                "cheap_on": "-",
                "kelly_pct": 0,
                "kelly_fraction": 0,
                "is_signal": False,
            })

        if not outcomes:
            continue

        # Use event title as "home_team" and market question as context
        title = pm["event_title"]
        question = pm["market_question"]
        # For multi-outcome events, use group_title if present
        display_name = pm.get("group_title") or question or title

        comparisons.append({
            "home_team": display_name,
            "away_team": "",
            "commence_time": "",
            "time_until": "Esports",
            "num_bookmakers": 0,
            "sharp_book": "Polymarket",
            "bookmaker_breakdown": {},
            "poly_question": question,
            "poly_slug": pm["slug"],
            "condition_id": pm.get("condition_id", ""),
            "poly_volume": pm["volume"],
            "poly_liquidity": pm["liquidity"],
            "poly_spread": pm["spread"],
            "poly_one_day_change": pm["one_day_change"],
            "match_score": 100,
            "outcomes": outcomes,
            "has_signal": False,
            "max_divergence": 0,
            "is_futures": True,
            "is_esport": True,
            # Enriched data points
            "highest_book_prob": 0,
            "lowest_book_prob": 0,
            "book_range": 0,
            "median_book_prob": 0,
            "book_std_dev": 0,
            "book_agreement": 0,
            "implied_vig": 0,
            "true_prob_no_vig": 0,
            "best_decimal_odds": 0,
            "worst_decimal_odds": 0,
            "volume_liquidity_ratio": round(pm["volume"] / max(pm["liquidity"], 1), 2),
            "spread_pct": round((pm["spread"] / max((outcomes[0].get("poly_price") or 0.01), 0.01)) * 100, 2) if (outcomes and isinstance(outcomes[0], dict)) else 0,
            "edge_direction": "-",
            "time_to_event_hours": None,
            "confidence_score": 0,
            "kalshi_event": None,
            "kalshi_volume": 0,
            **_build_trade_urls(pm["slug"]),
        })

    comparisons.sort(key=lambda x: -x["poly_volume"])
    return comparisons


def _build_kalshi_comparisons(kalshi_parsed: list[dict], poly_markets: list[dict] | None = None) -> list[dict]:
    """Build comparison cards from Kalshi markets (with optional Polymarket overlay).

    Used for sports where Kalshi is the primary source (F1, Tennis, etc.).
    """
    comparisons: list[dict] = []
    for km in kalshi_parsed:
        outcomes = []
        for tname, tdata in km["teams"].items():
            prob = tdata["implied_prob"]
            if prob <= 0 or prob >= 100:
                continue
            outcomes.append({
                "outcome": tname,
                "outcome_name": tname,
                "poly_outcome": None,
                "sharp_prob": prob,
                "consensus_prob": prob,
                "poly_prob": None,
                "poly_price": None,
                "kalshi_prob": prob,
                "kalshi_ticker": tdata["ticker"],
                "kalshi_divergence": None,
                "divergence": 0,
                "divergence_pct": 0,
                "abs_divergence": 0,
                "cheap_on": "-",
                "kelly_pct": 0,
                "kelly_fraction": 0,
                "is_signal": False,
            })

        if not outcomes:
            continue

        title = km["title"]
        mtype = km.get("market_type", "game")
        time_label = "Futures" if mtype in ("futures", "props") else "Kalshi"

        comparisons.append({
            "home_team": title,
            "away_team": "",
            "commence_time": "",
            "time_until": time_label,
            "num_bookmakers": 0,
            "sharp_book": "Kalshi",
            "bookmaker_breakdown": {},
            "poly_question": None,
            "poly_slug": None,
            "poly_volume": 0,
            "poly_liquidity": 0,
            "poly_spread": 0,
            "poly_one_day_change": 0,
            "match_score": 100,
            "outcomes": outcomes,
            "has_signal": False,
            "max_divergence": 0,
            "is_futures": mtype != "game",
            "is_esport": False,
            "highest_book_prob": 0,
            "lowest_book_prob": 0,
            "book_range": 0,
            "median_book_prob": 0,
            "book_std_dev": 0,
            "book_agreement": 0,
            "implied_vig": 0,
            "true_prob_no_vig": 0,
            "best_decimal_odds": 0,
            "worst_decimal_odds": 0,
            "volume_liquidity_ratio": 0,
            "spread_pct": 0,
            "edge_direction": "-",
            "time_to_event_hours": None,
            "confidence_score": 0,
            "kalshi_event": km["event_ticker"],
            "kalshi_volume": km["total_volume"],
            **_build_trade_urls(None, km["event_ticker"]),
        })

    comparisons.sort(key=lambda x: -x["kalshi_volume"])
    return comparisons


# These counters are only mutated inside data_updater (single asyncio task,
# guarded by _updater_running), so they are safe without an explicit lock.
_resolve_counter = 0  # run auto-resolve every 6th cycle (~30 min)
_bg_scan_counter = 0  # run background multi-sport scan every 4th cycle (~20 min)
_updater_running = False  # prevent double-start of data_updater


async def data_updater():
    """Background task that polls APIs and updates dashboard_data."""
    global _poly_cache, _poly_cache_time, _resolve_counter, _bg_scan_counter, _updater_running
    if _updater_running:
        return
    _updater_running = True
    while True:
        loop_start = time.monotonic()
        async with _data_lock:
            sport = dashboard_data["active_sport"]
        try:
            is_esport = sport in ESPORTS_KEYS
            is_kalshi_only = sport in KALSHI_ONLY_SPORTS

            # Always fetch Polymarket (used for both sports and esports)
            import time as _time
            cache_age = _time.time() - _poly_cache_time.get("_global", 0)
            if cache_age > 120 or "_global" not in _poly_cache:
                poly_raw = await asyncio.to_thread(fetch_polymarket_sports)
                _poly_cache["_global"] = poly_raw
                _poly_cache_time["_global"] = _time.time()
            else:
                poly_raw = _poly_cache["_global"]

            if is_esport:
                # Esports: Polymarket-only (no bookmaker odds API)
                raw_odds, remaining = [], None
                outright_raw, remaining2 = [], None
                kalshi_raw = []
            elif is_kalshi_only:
                # Kalshi-primary sports (F1, Tennis): Kalshi + Polymarket, no Odds API
                raw_odds, remaining = [], None
                outright_raw, remaining2 = [], None
                kalshi_raw = await asyncio.to_thread(fetch_kalshi_markets, sport)
            else:
                # Traditional sports: fetch bookmaker odds (h2h + spreads + totals) + Kalshi in parallel
                odds_task = asyncio.to_thread(fetch_odds, sport)
                outright_task = asyncio.to_thread(fetch_outright_odds, sport)
                kalshi_task = asyncio.to_thread(fetch_kalshi_markets, sport)
                (raw_odds, remaining), (outright_raw, remaining2), kalshi_raw = await asyncio.gather(
                    odds_task, outright_task, kalshi_task
                )
                if remaining2 and remaining:
                    try:
                        remaining = str(min(int(remaining), int(remaining2)))
                    except ValueError:
                        pass

            # Check if sport changed during fetch — discard stale results
            async with _data_lock:
                current_sport = dashboard_data["active_sport"]
            if sport != current_sport:
                continue

            all_poly_markets = parse_polymarket_events(poly_raw)

            # Filter Polymarket markets to only those relevant to the active sport
            sport_filters = SPORT_POLY_FILTERS.get(sport, [])
            if sport_filters:
                poly_markets = []
                for pm in all_poly_markets:
                    text = (pm["market_question"] + " " + pm["event_title"] + " " + " ".join(pm["tags"])).lower()
                    if any(kw in text for kw in sport_filters):
                        poly_markets.append(pm)
            else:
                poly_markets = all_poly_markets

            odds_events: list = []
            kalshi_parsed: list = []

            if is_esport:
                # For esports: present Polymarket markets directly as comparisons
                comparisons = _build_esports_comparisons(poly_markets)
            elif is_kalshi_only:
                # Kalshi-primary: build cards from Kalshi data, overlay Poly if available
                kalshi_parsed = parse_kalshi_markets(kalshi_raw)
                comparisons = _build_kalshi_comparisons(kalshi_parsed, poly_markets)
            else:
                # Parse h2h (moneyline) markets
                odds_events = parse_odds_events(raw_odds, "h2h")
                outright_odds = parse_outright_events(outright_raw)
                kalshi_parsed = parse_kalshi_markets(kalshi_raw)

                # Match-level comparisons (now with Kalshi)
                match_comparisons = match_and_compare(odds_events, poly_markets, kalshi_parsed)
                # Futures/outright comparisons
                futures_comparisons = compare_outrights(outright_odds, poly_markets)

                # Parse spreads and totals markets
                spreads_events = parse_odds_events(raw_odds, "spreads")
                totals_events = parse_odds_events(raw_odds, "totals")
                spreads_comparisons = match_and_compare(spreads_events, poly_markets, kalshi_parsed)
                totals_comparisons = match_and_compare(totals_events, poly_markets, kalshi_parsed)
                # Tag market type on each comparison
                for c in match_comparisons:
                    c["market_type"] = "h2h"
                for c in spreads_comparisons:
                    c["market_type"] = "spreads"
                for c in totals_comparisons:
                    c["market_type"] = "totals"
                for c in futures_comparisons:
                    c["market_type"] = "futures"

                # Combine all market types
                comparisons = match_comparisons + spreads_comparisons + totals_comparisons + futures_comparisons

            comparisons.sort(key=lambda x: (-x["has_signal"], -x["max_divergence"]))
            signals = [c for c in comparisons if c["has_signal"]]
            M_COMPARISONS.labels(sport=sport).inc(len(comparisons))
            M_SIGNALS.labels(sport=sport).inc(len(signals))

            # Attach historical head-to-head + recent form to each comparison
            try:
                await asyncio.to_thread(_attach_h2h_to_comparisons, sport, comparisons)
            except Exception as h2h_err:
                M_POLL_ERRORS.labels(stage="h2h_attach").inc()
                print(f"H2H attach error: {h2h_err}", flush=True)

            # Attach top-10 Polymarket trader positions (joined on conditionId)
            try:
                await asyncio.to_thread(_attach_top_traders_to_comparisons, comparisons)
            except Exception as ttp_err:
                print(f"Top trader attach error: {ttp_err}", flush=True)

            # Save edges to edge_history (deduplicate within 24h)
            try:
                await asyncio.to_thread(_save_edge_history, sport, comparisons)
            except Exception as eh_err:
                print(f"Edge history save error: {eh_err}", flush=True)

            # Save market snapshots for historical data (pro feature)
            try:
                await asyncio.to_thread(_save_market_snapshots, sport, comparisons)
            except Exception as snap_err:
                print(f"Snapshot save error: {snap_err}", flush=True)

            # Compute line movement trends from snapshot history
            try:
                trends = await asyncio.to_thread(_compute_edge_trends, comparisons)
                # Attach trend data to each comparison
                for comp in comparisons:
                    event_name = comp.get("home_team", "") + (" vs " + comp.get("away_team", "") if comp.get("away_team") else "")
                    for oc in comp.get("outcomes", []):
                        key = f"{event_name}|{oc.get('outcome', '')}"
                        oc["trend"] = trends.get(key, {"direction": "new", "change_2h": 0, "change_24h": 0, "points": []})
            except Exception as trend_err:
                print(f"Trend compute error: {trend_err}", flush=True)

            # Update cross-sport edge cache
            try:
                _update_cross_sport_edges(sport, comparisons)
            except Exception:
                pass

            # Send alerts for new signals
            try:
                await asyncio.to_thread(_send_alerts, sport, signals)
            except Exception as alert_err:
                M_POLL_ERRORS.labels(stage="send_alerts").inc()
                print(f"Alert send error: {alert_err}", flush=True)

            # Watchlist alerts: per-user pinned markets that crossed their threshold
            try:
                await asyncio.to_thread(_send_watchlist_alerts, sport, comparisons)
            except Exception as wl_err:
                M_POLL_ERRORS.labels(stage="send_watchlist_alerts").inc()
                print(f"Watchlist alert error: {wl_err}", flush=True)

            # Rule-based alerts: structured per-user rules over the signal feed.
            try:
                await asyncio.to_thread(_eval_alert_rules, sport, signals)
            except Exception as rule_err:
                M_POLL_ERRORS.labels(stage="alert_rules").inc()
                print(f"Alert rule error: {rule_err}", flush=True)

            # Build complete update in a local dict, then swap atomically
            update = {
                "comparisons": comparisons,
                "signals": signals,
                "odds_events_count": len(odds_events),
                "poly_markets_count": len(poly_markets),
                "poly_events_count": len(all_poly_markets),
                "kalshi_markets_count": len(kalshi_parsed),
                "matched_count": len(comparisons),
                "api_requests_remaining": remaining,
                "last_update": datetime.now(timezone.utc).isoformat(),
                "error": None,
                "active_sport": sport,
                "is_esport": is_esport,
                "cross_sport_edges": _get_all_cross_sport_edges(),
            }
            async with _data_lock:
                # Only apply if sport hasn't changed while we built the update
                if dashboard_data["active_sport"] == sport:
                    dashboard_data.update(update)

            # Update Polymarket WS subscription set so live prices follow
            # the markets we're actually showing.
            try:
                _update_pm_ws_subscriptions(comparisons)
            except Exception as ws_err:
                M_POLL_ERRORS.labels(stage="ws_subscribe").inc()
                log.warning("PM WS subscribe update failed: %s", ws_err)

            # Save signals to file (deduplicated)
            if signals:
                save_signals(signals)

            # Notify WebSocket clients
            await broadcast_update()

            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Updated: {len(odds_events)} odds, "
                  f"{len(poly_markets)} poly, {len(kalshi_parsed)} kalshi, "
                  f"{len(comparisons)} matched, {len(signals)} signals",
                  flush=True)

        except Exception as e:
            M_POLL_ERRORS.labels(stage="main_loop").inc()
            async with _data_lock:
                dashboard_data["error"] = str(e)
            print(f"Update error: {e}", flush=True)
        finally:
            M_POLL_DURATION.observe(time.monotonic() - loop_start)

        # Periodic: auto-resolve edges every ~30 min
        _resolve_counter += 1
        if _resolve_counter >= 6:
            _resolve_counter = 0
            try:
                await asyncio.to_thread(_run_score_resolution, sport)
            except Exception as res_err:
                M_POLL_ERRORS.labels(stage="score_resolution").inc()
                print(f"Auto-resolve error: {res_err}", flush=True)

        # Periodic: background multi-sport scan every ~20 min
        _bg_scan_counter += 1
        if _bg_scan_counter >= 4:
            _bg_scan_counter = 0
            _spawn_bg(_background_multi_sport_scan())

        # Adaptive poll interval — see _compute_poll_interval for the policy.
        # Combines pre-game proximity with quota-aware throttling so we poll
        # fast when a game is about to start and slow down when the Odds API
        # quota is low.
        interval = _compute_poll_interval(comparisons, odds_quota_remaining())
        M_POLL_INTERVAL.set(interval)

        # Wait for poll interval OR immediate rescan trigger
        _scan_event.clear()
        try:
            await asyncio.wait_for(_scan_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


def _run_score_resolution(sport: str):
    """Fetch scores for a sport and auto-resolve edges."""
    scores = fetch_scores(sport)
    if scores:
        _store_scores(sport, scores)
    _auto_resolve_edges()


async def _background_multi_sport_scan():
    """Scan non-active sports in the background for cross-sport edge feed."""
    async with _data_lock:
        active = dashboard_data["active_sport"]
    # Pick a few non-active traditional sports to scan
    scan_sports = [k for k in SPORTS if k != active and k not in ESPORTS_KEYS and k not in KALSHI_ONLY_SPORTS]
    # Limit to 3 per cycle to conserve API calls
    for sport_key in scan_sports[:3]:
        try:
            raw_odds, _ = await asyncio.to_thread(fetch_odds, sport_key, "h2h")
            if not raw_odds:
                continue
            import time as _time
            poly_raw = _poly_cache.get("_global", [])
            if not poly_raw:
                continue
            all_poly = parse_polymarket_events(poly_raw)
            sport_filters = SPORT_POLY_FILTERS.get(sport_key, [])
            if sport_filters:
                poly_filtered = [pm for pm in all_poly if any(kw in (pm["market_question"] + " " + pm["event_title"] + " " + " ".join(pm["tags"])).lower() for kw in sport_filters)]
            else:
                poly_filtered = all_poly
            odds_events = parse_odds_events(raw_odds, "h2h")
            comparisons = match_and_compare(odds_events, poly_filtered)
            for c in comparisons:
                c["market_type"] = "h2h"
            try:
                await asyncio.to_thread(_attach_h2h_to_comparisons, sport_key, comparisons)
            except Exception:
                pass
            _update_cross_sport_edges(sport_key, comparisons)
        except Exception as e:
            log.warning("BG scan %s error: %s", sport_key, e)
        # Small delay between sports to avoid hammering APIs
        await asyncio.sleep(2)


async def broadcast_update():
    """Push update to all connected WebSocket clients."""
    global connected_ws
    if not connected_ws:
        return
    async with _data_lock:
        msg = json.dumps({"type": "update", "data": dashboard_data}, default=str)
    dead = set()
    for ws in connected_ws.copy():
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    if dead:
        connected_ws.difference_update(dead)


def save_signals(signals: list[dict]):
    """Append signals to sports_signals.json, deduplicating by event+outcome."""
    existing = []
    if SIGNALS_FILE.exists():
        try:
            existing = json.loads(SIGNALS_FILE.read_text())
        except Exception:
            existing = []

    # Deduplicate: only add signals not already present (by event + outcome key)
    seen = set()
    for s in existing:
        for o in s.get("outcomes", []):
            key = f"{s.get('home_team','')}|{s.get('away_team','')}|{o.get('outcome','')}"
            seen.add(key)

    new_signals = []
    for s in signals:
        dominated = False
        for o in s.get("outcomes", []):
            key = f"{s.get('home_team','')}|{s.get('away_team','')}|{o.get('outcome','')}"
            if key in seen:
                dominated = True
                break
            seen.add(key)
        if not dominated:
            s["saved_at"] = datetime.now(timezone.utc).isoformat()
            new_signals.append(s)

    if new_signals:
        existing.extend(new_signals)
        existing = existing[-500:]
        # Atomic write: temp file + os.replace to avoid partial writes
        fd, tmp = tempfile.mkstemp(dir=str(SIGNALS_FILE.parent))
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(existing, f, indent=2, default=str)
            os.replace(tmp, str(SIGNALS_FILE))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _config_check() -> list[dict]:
    """Inventory of every important env var / config knob, returned as a
    structured list of {key, status, effect, remediation} dicts.

    status:
      "ok"   — configured
      "warn" — missing but the service still works (degraded feature)
      "fail" — missing in a way that breaks core functionality

    Single source of truth for both the startup-log warning summary
    and the /api/diagnostics/config-check endpoint, so the two never
    drift apart.
    """
    items: list[dict] = []

    def _ok(key, effect=""):
        items.append({"key": key, "status": "ok", "effect": effect, "remediation": ""})

    def _warn(key, effect, remediation):
        items.append({"key": key, "status": "warn", "effect": effect,
                       "remediation": remediation})

    def _fail(key, effect, remediation):
        items.append({"key": key, "status": "fail", "effect": effect,
                       "remediation": remediation})

    # Core auth — fail closed in production
    if _BEHIND_GATEWAY:
        _ok("GATEWAY_SSO_SECRET", "gateway SSO middleware active")
    elif _DEV_MODE:
        _warn("DEV_MODE", "auth bypassed for local dev",
                "set GATEWAY_SSO_SECRET in production")
    else:
        _fail("GATEWAY_SSO_SECRET",
                "no auth and DEV_MODE not set — every request will 503",
                "set GATEWAY_SSO_SECRET to match gateway/.env, OR DEV_MODE=1")

    # Odds API — needed for h2h + spreads + totals + player props
    if ODDS_API_KEY:
        _ok("ODDS_API_KEY", "bookmaker odds available")
    else:
        _warn("ODDS_API_KEY",
                "bookmaker odds will be unavailable — only Polymarket + Kalshi feeds will work",
                "get a key at https://the-odds-api.com (free tier 500 req/mo)")

    # Anthropic — needed for /api/signals/explain
    if ANTHROPIC_API_KEY:
        _ok("ANTHROPIC_API_KEY", "AI signal explanations enabled")
    else:
        _warn("ANTHROPIC_API_KEY",
                "/api/signals/explain returns 503; everything else works",
                "set ANTHROPIC_API_KEY to enable explanations")

    # Web Push / VAPID
    if VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY:
        if _PUSH_AVAILABLE:
            _ok("VAPID_PUBLIC_KEY/PRIVATE_KEY", "Web Push delivery enabled")
        else:
            _warn("pywebpush",
                    "VAPID keys set but pywebpush not installed",
                    "pip install pywebpush==2.0.0")
    else:
        _warn("VAPID_PUBLIC_KEY/PRIVATE_KEY",
                "Web Push subscriptions stored but no notifications are delivered",
                "generate keys with `pywebpush vapid_key` and set both env vars")

    # Polymarket WS — implicit; flag if connection has been failing
    if _pm_ws_failure_count >= PM_WS_CIRCUIT_THRESHOLD:
        _fail("polymarket_ws",
                f"WS circuit breaker OPEN after {_pm_ws_failure_count} failures",
                "check Polymarket status; check outbound network to "
                "wss://ws-subscriptions-clob.polymarket.com")
    else:
        _ok("polymarket_ws", "WS subscriber healthy")

    return items


def _print_config_check() -> None:
    """Pretty-print the config check at startup. One line per issue;
    silent on `ok` entries to keep the boot log clean."""
    issues = [i for i in _config_check() if i["status"] != "ok"]
    if not issues:
        print("Config check: all systems nominal")
        return
    print(f"Config check: {len(issues)} issue(s) — see below")
    for i in issues:
        marker = "FAIL" if i["status"] == "fail" else "WARN"
        print(f"  [{marker}] {i['key']}: {i['effect']}")
        if i.get("remediation"):
            print(f"         → {i['remediation']}")


async def _run_startup_tasks():
    """Body of the original startup hook. Kept as a plain async function
    so it's easy to call from a lifespan context manager OR from a test
    that wants to drive the startup path directly."""
    _spawn_bg(data_updater())
    _spawn_bg(_polymarket_ws_loop())
    # Backfill historical markets in background thread (non-blocking)
    async def _backfill_wrapper():
        try:
            await asyncio.get_event_loop().run_in_executor(None, backfill_historical_markets)
        except Exception as e:
            print(f"Backfill error: {e}", flush=True)
    _spawn_bg(_backfill_wrapper())
    # Run initial auto-resolution in background
    async def _initial_resolve():
        try:
            await asyncio.to_thread(_auto_resolve_edges)
        except Exception as e:
            print(f"Initial auto-resolve error: {e}", flush=True)
    _spawn_bg(_initial_resolve())

    # Backfill team history (head-to-head) for all sports in background.
    # Active sport gets a fast 30-day refresh first, then a deeper backfill
    # for every sport runs over the next minute or two.
    async def _h2h_backfill():
        try:
            async with _data_lock:
                active = dashboard_data.get("active_sport")
            if active and active in ESPN_LEAGUE_MAP:
                n = await asyncio.to_thread(_refresh_history_for_sport, active, 30)
                print(f"team_history: initial {active} +{n} games", flush=True)
            # Check existing rows — only run full backfill if DB is mostly empty
            with _get_db() as conn:
                row_count = conn.execute("SELECT COUNT(*) FROM sports_team_history").fetchone()[0]
            if row_count < 200:
                await asyncio.to_thread(_backfill_all_team_history, 365)
            else:
                # Lighter refresh: pull last 14 days for every sport
                for sport_key in ESPN_LEAGUE_MAP:
                    try:
                        await asyncio.to_thread(_refresh_history_for_sport, sport_key, 14)
                    except Exception:
                        pass
                print(f"team_history: refreshed last 14 days for {len(ESPN_LEAGUE_MAP)} sports", flush=True)
        except Exception as e:
            print(f"team_history backfill error: {e}", flush=True)
    _spawn_bg(_h2h_backfill())

    # Backfill team info + rosters (team stats, top players, strengths/weaknesses)
    async def _team_info_backfill():
        try:
            # Wait briefly so the H2H backfill has a head start (avoids hammering ESPN)
            await asyncio.sleep(8)
            with _get_db() as conn:
                row_count = conn.execute("SELECT COUNT(*) FROM sports_team_info").fetchone()[0]
            if row_count < 50:
                await asyncio.to_thread(_backfill_all_team_info)
                print("team_info: initial backfill complete", flush=True)
            else:
                # Lighter refresh: just refresh active sport
                async with _data_lock:
                    active = dashboard_data.get("active_sport")
                if active and active in ESPN_LEAGUE_MAP:
                    await asyncio.to_thread(_refresh_team_info_for_sport, active)
                    print(f"team_info: refreshed {active}", flush=True)
        except Exception as e:
            print(f"team_info backfill error: {e}", flush=True)
    _spawn_bg(_team_info_backfill())

    # Periodically (every 3 days) refresh team info + rosters
    async def _team_info_periodic_refresh():
        while True:
            try:
                await asyncio.sleep(3 * 24 * 60 * 60)  # 3 days
                for sport_key in ESPN_LEAGUE_MAP:
                    try:
                        await asyncio.to_thread(_refresh_team_info_for_sport, sport_key)
                    except Exception:
                        pass
                print("team_info: periodic refresh complete", flush=True)
            except Exception as e:
                print(f"team_info periodic error: {e}", flush=True)
    _spawn_bg(_team_info_periodic_refresh())

    # Periodically (daily) refresh recent games for every supported sport
    async def _h2h_periodic_refresh():
        while True:
            try:
                await asyncio.sleep(24 * 60 * 60)  # 24 hours
                for sport_key in ESPN_LEAGUE_MAP:
                    try:
                        await asyncio.to_thread(_refresh_history_for_sport, sport_key, 7)
                    except Exception:
                        pass
                print("team_history: daily refresh complete", flush=True)
            except Exception as e:
                print(f"team_history periodic error: {e}", flush=True)
    _spawn_bg(_h2h_periodic_refresh())

    # Top-10 Polymarket trader positions: initial fetch on boot, then refresh
    # every TOP_TRADERS_REFRESH_SECS (default 10 min). The data updater
    # joins comparisons against the cached table on every poll.
    async def _top_traders_backfill():
        try:
            await asyncio.sleep(4)
            await asyncio.to_thread(_refresh_top_trader_positions)
        except Exception as e:
            print(f"top_traders backfill error: {e}", flush=True)
    _spawn_bg(_top_traders_backfill())

    async def _top_traders_periodic_refresh():
        while True:
            try:
                await asyncio.sleep(TOP_TRADERS_REFRESH_SECS)
                await asyncio.to_thread(_refresh_top_trader_positions)
            except Exception as e:
                print(f"top_traders periodic error: {e}", flush=True)
    _spawn_bg(_top_traders_periodic_refresh())

    print(f"Sports Dashboard started. Polling {dashboard_data.get('active_sport', 'unknown')} every {POLL_INTERVAL}s")

    # ── Loud, single-pass key-status banner ────────────────────────────────
    # If any third-party API key is missing, the corresponding feature will
    # silently degrade. Surface that here so misconfiguration is immediately
    # visible in the log instead of producing 564 cycles of "0 odds".
    print("─── env files loaded ───")
    if _loaded_from:
        for _p in _loaded_from:
            print(f"  ✓ {_p}")
    else:
        print("  (none — using process environment only)")

    print("─── third-party API keys ───")
    _key_status = [
        ("ODDS_API_KEY", ODDS_API_KEY, "the-odds-api.com — bookmaker odds (CRITICAL)"),
        ("KALSHI_API_KEY_ID", os.getenv("KALSHI_API_KEY_ID", ""), "Kalshi authenticated requests (optional)"),
        ("KALSHI_PRIVATE_KEY", os.getenv("KALSHI_PRIVATE_KEY", ""), "Kalshi key-pair signing (optional)"),
        ("GATEWAY_SSO_SECRET", os.getenv("GATEWAY_SSO_SECRET", ""), "Gateway SSO header verification"),
    ]
    _missing_critical = []
    for _name, _value, _desc in _key_status:
        if _value:
            print(f"  ✓ {_name:25s} set ({len(_value)} chars) — {_desc}")
        else:
            print(f"  ✗ {_name:25s} MISSING — {_desc}")
            if "CRITICAL" in _desc:
                _missing_critical.append(_name)
    if _missing_critical:
        print(f"⚠ CRITICAL keys missing: {', '.join(_missing_critical)} — core features will silently return empty.")
        print("  → add them to /home/julianhabbig/Polymarket/gateway/.env.production and restart polymarket-sports.")
    print("────────────────────────")
    _print_config_check()


@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    """FastAPI lifespan handler. Runs startup tasks, then yields control
    to the request-handling loop. No shutdown cleanup — background tasks
    are daemon-style and exit with the process."""
    await _run_startup_tasks()
    yield


# Attach the lifespan post-construction. We can't pass `lifespan=` to
# `FastAPI(...)` at construction because that line is hundreds of lines
# above where the helpers it calls (data_updater, _polymarket_ws_loop,
# etc.) are defined — reordering would mean moving every @app decorator
# below the helpers. Setting lifespan_context after the fact has the
# same effect and is supported by FastAPI >= 0.100.
app.router.lifespan_context = _app_lifespan


# ---------------------------------------------------------------------------
# Auth pages & endpoints
# ---------------------------------------------------------------------------
# Auth is handled by the gateway. These endpoints redirect to it or return
# user info from the gateway-forwarded headers + local profiles.

@app.get("/login")
async def login_page():
    return RedirectResponse("https://narve.ai/login", status_code=302)


@app.get("/api/health")
async def health():
    """Liveness + key-status. Returns 200 if alive, 'degraded' if a critical
    third-party key is missing (i.e. the dashboard is up but core feature is
    silently empty). Use this from monitoring to catch the silent-failure
    mode that bit us with ODDS_API_KEY."""
    keys = {
        "ODDS_API_KEY":       {"set": bool(ODDS_API_KEY),                   "critical": True},
        "KALSHI_API_KEY_ID":  {"set": bool(os.getenv("KALSHI_API_KEY_ID")), "critical": False},
        "KALSHI_PRIVATE_KEY": {"set": bool(os.getenv("KALSHI_PRIVATE_KEY")), "critical": False},
        "GATEWAY_SSO_SECRET": {"set": bool(os.getenv("GATEWAY_SSO_SECRET")), "critical": False},
    }
    missing_critical = [k for k, v in keys.items() if v["critical"] and not v["set"]]
    odds_breaker_open = time.time() < _ODDS_BREAKER_OPEN_UNTIL
    status = "healthy"
    if missing_critical:
        status = "degraded"
    elif odds_breaker_open:
        # Still healthy operationally, just on the cross-venue-only fallback
        status = "degraded-quota-exhausted"
    return JSONResponse({
        "ok": True,
        "service": "sports-dashboard",
        "status": status,
        "missing_critical_keys": missing_critical,
        "keys": keys,
        "odds_breaker": {
            "open": odds_breaker_open,
            "reopen_in_s": max(0, int(_ODDS_BREAKER_OPEN_UNTIL - time.time())),
            "last_reason": _ODDS_BREAKER_LAST_REASON or None,
        },
        "env_files_loaded": _loaded_from,
        "ts": time.time(),
    })


@app.get("/api/logout")
async def logout():
    return RedirectResponse("https://narve.ai/logout", status_code=302)


@app.get("/api/me")
async def get_me(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    settings = get_user_settings(user["id"])
    return JSONResponse({
        "username": user["username"],
        "email": user["email"],
        "settings": settings,
        "is_admin": bool(user.get("is_admin")),
    })


@app.post("/api/settings")
async def update_settings(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await request.json()

    try:
        divergence = float(body.get("divergence_threshold", 5.0))
    except (ValueError, TypeError):
        return JSONResponse({"error": "Invalid divergence_threshold"}, status_code=400)
    try:
        notif = int(body.get("notifications_enabled", 1))
    except (ValueError, TypeError):
        notif = 1

    # Upsert settings into sports_user_settings
    with _get_db() as conn:
        conn.execute(
            """INSERT INTO sports_user_settings (user_id, default_sport, divergence_threshold, notifications_enabled, theme)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   default_sport = excluded.default_sport,
                   divergence_threshold = excluded.divergence_threshold,
                   notifications_enabled = excluded.notifications_enabled,
                   theme = excluded.theme""",
            (user["id"],
             body.get("default_sport", "basketball_nba"),
             divergence,
             notif,
             body.get("theme", "dark")),
        )
    return JSONResponse({"status": "ok"})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return HTMLResponse(SETTINGS_HTML)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not user.get("is_admin"):
        return HTMLResponse("<h1>403 -- Access Denied</h1>", status_code=403)
    return HTMLResponse(ADMIN_HTML)


@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not user.get("is_admin"):
        return HTMLResponse("<h1>403 -- Access Denied</h1>", status_code=403)
    return HTMLResponse(USERS_HTML)


@app.get("/api/admin/users")
async def admin_users_api(request: Request):
    """Quick JSON dump of all users -- admin only."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not user.get("is_admin"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    with _get_db() as conn:
        rows = conn.execute("SELECT * FROM profiles ORDER BY created_at DESC").fetchall()
        profiles = [dict(r) for r in rows]
    return JSONResponse({"users": profiles, "total": len(profiles)})


@app.get("/api/admin/stats")
async def admin_stats(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not user.get("is_admin"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    with _get_db() as conn:
        # Profiles (users are managed by gateway, we just read them)
        rows = conn.execute("SELECT * FROM profiles ORDER BY created_at DESC").fetchall()
        profiles = [dict(r) for r in rows]
        total_users = len(profiles)

        # Recent sports activity
        act_rows = conn.execute(
            "SELECT action, detail, created_at, user_id FROM sports_user_activity ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        activity = [dict(r) for r in act_rows]

        # Edge performance stats
        edge_total = (conn.execute("SELECT COUNT(*) FROM sports_edge_history").fetchone() or (0,))[0]
        edge_resolved = (conn.execute("SELECT COUNT(*) FROM sports_edge_history WHERE resolved = 1").fetchone() or (0,))[0]
        edge_correct = (conn.execute("SELECT COUNT(*) FROM sports_edge_history WHERE resolution = 'correct'").fetchone() or (0,))[0]
        edge_incorrect = (conn.execute("SELECT COUNT(*) FROM sports_edge_history WHERE resolution = 'incorrect'").fetchone() or (0,))[0]
        edge_win_rate = round(edge_correct / edge_resolved * 100, 1) if edge_resolved > 0 else 0.0

    return JSONResponse({
        "users": profiles,
        "total_users": total_users,
        "activity": activity,
        "edge_performance": {
            "total_edges": edge_total,
            "resolved": edge_resolved,
            "correct": edge_correct,
            "incorrect": edge_incorrect,
            "pending": edge_total - edge_resolved,
            "win_rate": edge_win_rate,
        },
    })


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

def _require_auth(request: Request):
    """Return user or raise 401."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


@app.get("/api/data")
async def api_data(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    # Subscription/tier gating is handled by the gateway before the request
    # reaches this dashboard. If the user is here, they have access.
    #
    # Optional ?sport= param: if the client passes this, and the server's
    # current data is for a different sport, return a 202 with a hint so
    # the client knows it needs to wait for the data_updater to catch up.
    requested_sport = request.query_params.get("sport")
    # Optional ?min_liquidity=N filter — hides markets with Polymarket
    # liquidity below the threshold. Defaults to 0 (no filter) so existing
    # callers behave the same. Recommended UI default: $1000.
    try:
        min_liquidity = float(request.query_params.get("min_liquidity") or 0)
    except (ValueError, TypeError):
        min_liquidity = 0.0
    # Snapshot dashboard_data under the lock so we never serialize a half-
    # updated dict (the data_updater mutates this dict in-place).
    async with _data_lock:
        active_sport = dashboard_data.get("active_sport")
        snapshot = copy.deepcopy(dashboard_data)
    # Apply live Polymarket prices to the snapshot so the API always
    # returns the freshest possible state, even between poll loops.
    try:
        _apply_live_prices_to_comparisons(snapshot.get("comparisons") or [])
    except Exception as live_err:
        log.warning("apply_live_prices failed: %s", live_err)
    if requested_sport and requested_sport != active_sport:
        return JSONResponse(
            {"status": "switching", "active_sport": active_sport,
             "requested_sport": requested_sport},
            status_code=202,
        )
    if min_liquidity > 0:
        # Filter the per-game signals list. Each event carries
        # poly_liquidity (USD); markets below the threshold drop.
        events = snapshot.get("events", []) or []
        filtered = [e for e in events if (e.get("poly_liquidity") or 0) >= min_liquidity]
        snapshot["events"] = filtered
        snapshot["_min_liquidity_filter"] = min_liquidity
        snapshot["_filtered_out"] = len(events) - len(filtered)
    return JSONResponse(snapshot)


@app.get("/api/sports")
async def api_sports(request: Request):
    if not get_current_user(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    # Return sports grouped by category for two-tier nav.
    # Accept optional ?sport= param so clients can request a specific
    # sport as "active" (mirrors their local selection, not the global).
    categories = []
    for cat_name, keys in SPORT_CATEGORIES.items():
        items = [{"key": k, "title": SPORTS[k]} for k in keys if k in SPORTS]
        categories.append({"name": cat_name, "sports": items})
    requested_sport = request.query_params.get("sport")
    async with _data_lock:
        current_active = dashboard_data["active_sport"]
    active = requested_sport if (requested_sport and requested_sport in SPORTS) else current_active
    return JSONResponse({
        "categories": categories,
        "sports": SPORTS,
        "active": active,
    })


@app.post("/api/sport/{sport_key}")
async def set_sport(sport_key: str, request: Request):
    if not get_current_user(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if sport_key in SPORTS:
        async with _data_lock:
            dashboard_data["active_sport"] = sport_key
            # Don't clear comparisons/signals here — the updater will
            # build fresh data in a local variable and swap it in
            # atomically, so other users never see an empty state.
            dashboard_data["error"] = None
        await broadcast_update()
        await trigger_rescan()  # immediate rescan instead of waiting 5min
        return JSONResponse({"status": "ok", "sport": sport_key})
    return JSONResponse({"error": "Unknown sport"}, status_code=400)


@app.get("/api/orderbook/{token_id}")
async def get_orderbook(token_id: str, request: Request):
    """Fetch orderbook from Polymarket CLOB for a specific token."""
    if not get_current_user(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        resp = await asyncio.to_thread(
            lambda: requests.get(f"{POLYMARKET_HOST}/book", params={"token_id": token_id}, timeout=10)
        )
        resp.raise_for_status()
        return JSONResponse(resp.json())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Subscription endpoints
# ---------------------------------------------------------------------------

@app.post("/api/admin/set-tier")
async def admin_set_tier(request: Request):
    """Subscriptions are managed by the gateway. This is a no-op stub."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not user.get("is_admin"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    return JSONResponse({"error": "Subscriptions are managed by the gateway"}, status_code=400)


@app.get("/api/subscription")
async def get_subscription(request: Request):
    """Subscriptions are managed by the gateway. Return basic info."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    # If the user reached this dashboard, the gateway already verified their subscription
    return JSONResponse({"tier": "active", "managed_by": "gateway"})


# ---------------------------------------------------------------------------
# Trades (profit tracker) endpoints
# ---------------------------------------------------------------------------

def _compute_trade_clv(trade: dict) -> tuple[float | None, float | None]:
    """Look up the closing line for this trade from sports_market_snapshots.

    Returns (closing_book_prob_pct, clv_pp). The CLV is computed relative
    to the bet direction:
      - If entry_price represents YES (over), CLV = closing - entry
      - If under/no, CLV = entry - closing
    Since we store entry_price as cents (0-100) on the YES side, we treat
    everything as YES-direction and let the user interpret negative
    values as "line moved against me".
    """
    home = trade.get("home_team") or ""
    away = trade.get("away_team") or ""
    event_name = home + (f" vs {away}" if away else "")
    outcome = trade.get("outcome") or ""
    sport = trade.get("sport") or ""
    commence = trade.get("commence_time") or trade.get("resolved_at") or ""
    created = trade.get("created_at") or ""
    if not event_name or not outcome or not commence:
        return None, None
    with _get_db() as conn:
        row = conn.execute(
            """SELECT poly_prob FROM sports_market_snapshots
               WHERE sport = ? AND event_name = ? AND outcome = ?
                     AND snapshot_at <= ? AND snapshot_at >= ?
               ORDER BY snapshot_at DESC LIMIT 1""",
            (sport, event_name, outcome, commence, created),
        ).fetchone()
    if not row or row["poly_prob"] is None:
        return None, None
    closing = float(row["poly_prob"])
    entry = float(trade.get("entry_price") or 0)
    clv = round(closing - entry, 2)
    return closing, clv


@app.post("/api/trades")
async def create_trade(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    body = await request.json()
    market_name = body.get("market_name", "")
    outcome = body.get("outcome", "")
    try:
        entry_price = float(body.get("entry_price", 0))
        amount = float(body.get("amount", 0))
    except (ValueError, TypeError):
        return JSONResponse({"error": "entry_price and amount must be valid numbers"}, status_code=400)
    if not market_name or entry_price <= 0 or amount <= 0:
        return JSONResponse({"error": "market_name, entry_price > 0, and amount > 0 required"}, status_code=400)
    # Optional enriched fields
    sport = (body.get("sport") or "")[:40]
    book = (body.get("book") or "")[:40]
    market_type = (body.get("market_type") or "h2h")[:20]
    home_team = (body.get("home_team") or "")[:80]
    away_team = (body.get("away_team") or "")[:80]
    commence_time = body.get("commence_time") or ""
    source = (body.get("source") or "manual")[:20]
    notes = (body.get("notes") or "")[:500]
    line = None
    if body.get("line") not in (None, ""):
        try:
            line = float(body["line"])
        except (TypeError, ValueError):
            pass

    with _get_db() as conn:
        cur = conn.execute(
            """INSERT INTO sports_trades
               (user_id, market_name, outcome, entry_price, amount,
                sport, book, market_type, line, commence_time, source, notes,
                home_team, away_team)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user["id"], market_name, outcome, entry_price, amount,
             sport, book, market_type, line, commence_time, source, notes,
             home_team, away_team),
        )
        trade_id = cur.lastrowid
    log_activity(user["id"], "create_trade", f"Trade #{trade_id}: {market_name}")
    return JSONResponse({"status": "ok", "trade_id": trade_id})


@app.get("/api/trades")
async def list_trades(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sport_filter = request.query_params.get("sport")
    status_filter = request.query_params.get("status")
    book_filter = request.query_params.get("book")
    where = ["user_id = ?"]
    params: list = [user["id"]]
    if sport_filter:
        where.append("sport = ?")
        params.append(sport_filter)
    if status_filter:
        where.append("status = ?")
        params.append(status_filter)
    if book_filter:
        where.append("book = ?")
        params.append(book_filter)
    sql = "SELECT * FROM sports_trades WHERE " + " AND ".join(where) + " ORDER BY created_at DESC"
    with _get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return JSONResponse({"trades": [dict(r) for r in rows]})


@app.post("/api/trades/{trade_id}/resolve")
async def resolve_trade(trade_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    body = await request.json()
    try:
        exit_price = float(body.get("exit_price", 0))
    except (ValueError, TypeError):
        return JSONResponse({"error": "exit_price must be a valid number"}, status_code=400)
    if exit_price <= 0:
        return JSONResponse({"error": "exit_price > 0 required"}, status_code=400)
    with _get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sports_trades WHERE id = ? AND user_id = ? LIMIT 1",
            (trade_id, user["id"]),
        ).fetchone()
        if not row:
            return JSONResponse({"error": "Trade not found"}, status_code=404)
        trade = dict(row)
        entry = trade["entry_price"]
        pnl = round((exit_price - entry) * trade["amount"] / entry, 2)
        # Compute CLV from snapshot history (returns (None, None) when we
        # don't have enough data — that's fine, it just stays unfilled).
        closing_prob, clv_pp = _compute_trade_clv(trade)
        conn.execute(
            """UPDATE sports_trades
               SET status = 'closed', exit_price = ?, pnl = ?,
                   resolved_at = ?, closing_book_prob = ?, clv_pp = ?
               WHERE id = ?""",
            (exit_price, pnl, datetime.now(timezone.utc).isoformat(),
             closing_prob, clv_pp, trade_id),
        )
    log_activity(user["id"], "resolve_trade", f"Trade #{trade_id}: PnL={pnl}")
    return JSONResponse({"status": "ok", "pnl": pnl, "clv_pp": clv_pp,
                          "closing_book_prob": closing_prob})


@app.delete("/api/trades/{trade_id}")
async def delete_trade(trade_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    with _get_db() as conn:
        cur = conn.execute(
            "DELETE FROM sports_trades WHERE id = ? AND user_id = ?",
            (trade_id, user["id"]),
        )
        if cur.rowcount == 0:
            return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"status": "ok"})


def _trade_stats_summary(trades: list[dict]) -> dict:
    """Common stats for a slice of trades. Used both at top level and per-group."""
    closed = [t for t in trades if t.get("status") == "closed"]
    open_trades = [t for t in trades if t.get("status") == "open"]
    total_pnl = round(sum((t.get("pnl") or 0) for t in closed), 2)
    wins = sum(1 for t in closed if (t.get("pnl") or 0) > 0)
    win_rate = round(wins / len(closed), 4) if closed else 0.0
    total_invested = sum((t.get("amount") or 0) for t in closed)
    roi = round(total_pnl / total_invested * 100, 3) if total_invested > 0 else 0.0
    # CLV summary: average over closed trades that have a clv_pp value
    clv_vals = [t.get("clv_pp") for t in closed if t.get("clv_pp") is not None]
    mean_clv = round(sum(clv_vals) / len(clv_vals), 3) if clv_vals else None
    return {
        "n_closed": len(closed),
        "n_open": len(open_trades),
        "n_total": len(trades),
        "total_pnl": total_pnl,
        "total_staked": round(total_invested, 2),
        "win_rate": win_rate,
        "roi_pct": roi,
        "mean_clv_pp": mean_clv,
        "n_with_clv": len(clv_vals),
    }


@app.get("/api/trades/stats")
async def trade_stats(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sports_trades WHERE user_id = ?", (user["id"],)
        ).fetchall()
    trades = [dict(r) for r in rows]
    overall = _trade_stats_summary(trades)

    # Group by sport / book / market_type
    def _group_by(key: str) -> dict:
        groups: dict[str, list[dict]] = {}
        for t in trades:
            k = t.get(key) or "(unset)"
            groups.setdefault(k, []).append(t)
        return {k: _trade_stats_summary(g) for k, g in groups.items()}

    return JSONResponse({
        "overall": overall,
        "by_sport": _group_by("sport"),
        "by_book": _group_by("book"),
        "by_market_type": _group_by("market_type"),
    })


@app.get("/api/trades/csv")
async def trade_csv(request: Request):
    """Export the user's trade history as CSV. Includes CLV columns."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sports_trades WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    cols = ["id", "created_at", "sport", "book", "market_type", "home_team",
            "away_team", "market_name", "outcome", "line", "entry_price",
            "amount", "exit_price", "pnl", "closing_book_prob", "clv_pp",
            "status", "resolved_at", "source", "notes"]
    writer = _csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({c: dict(r).get(c) for c in cols})
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades.csv"},
    )


# ---------------------------------------------------------------------------
# Bankroll + Kelly stake suggestions (T4.3)
# ---------------------------------------------------------------------------

DEFAULT_KELLY_FRACTION = 0.5      # half-Kelly is the de-facto retail standard
DEFAULT_MAX_PER_BET_PCT = 5.0     # never risk more than 5% of bankroll per bet
DEFAULT_DRAWDOWN_ALERT_PCT = 10.0  # alert when down 10% from starting


# ---------------------------------------------------------------------------
# Bettor calculators — pure math, no external state, used by /calculators
# ---------------------------------------------------------------------------
#
# Standalone tools every sharp bettor needs:
#   - odds_convert: American ↔ Decimal ↔ implied probability
#   - arb_calculator: split a stake across opposing prices to lock in profit
#   - hedge_calculator: given an existing bet, what stake on the other side?
#   - promo_conversion: convert a "free bet" or boosted-odds offer to cash
#   - devig: strip vig from a 2-way market to estimate the fair line
#
# Implemented as pure functions over `dict` inputs (matches the rest of the
# file's style) so each is trivially testable with no fixtures.

def calc_odds_convert(value: float | int, fmt: str) -> dict:
    """Convert between odds formats. `fmt` is the input format:
    'american' | 'decimal' | 'implied_pct' | 'implied_frac'.

    Returns all four representations + the canonical decimal price.
    """
    fmt = (fmt or "").lower()
    v = float(value)

    if fmt == "american":
        if v == 0 or v == -100 or v == 100:
            raise ValueError("American odds cannot be 0 or ±100 exactly")
        if v > 0:
            decimal = 1.0 + (v / 100.0)
        else:
            decimal = 1.0 + (100.0 / abs(v))
    elif fmt == "decimal":
        if v <= 1.0:
            raise ValueError("Decimal odds must be > 1.0")
        decimal = v
    elif fmt == "implied_pct":
        if not (0.0 < v < 100.0):
            raise ValueError("Implied % must be in (0, 100)")
        decimal = 100.0 / v
    elif fmt == "implied_frac":
        if not (0.0 < v < 1.0):
            raise ValueError("Implied fraction must be in (0, 1)")
        decimal = 1.0 / v
    else:
        raise ValueError(f"unknown format: {fmt!r}")

    implied_frac = 1.0 / decimal
    implied_pct = implied_frac * 100.0
    # American — symmetric inverse of the input conversion above.
    # At exactly 50% (decimal 2.00 / evens), convention is +100, not -100,
    # so the > here is strict, not >=.
    if implied_frac > 0.5:
        american = -100.0 * implied_frac / (1.0 - implied_frac)
    else:
        american = (1.0 - implied_frac) / implied_frac * 100.0

    return {
        "decimal": round(decimal, 4),
        "american": round(american, 0),
        "implied_pct": round(implied_pct, 3),
        "implied_frac": round(implied_frac, 5),
    }


def calc_arbitrage(decimal_odds_a: float, decimal_odds_b: float,
                   total_stake: float = 100.0) -> dict:
    """Given two opposing decimal odds, split `total_stake` so payout is
    identical regardless of outcome.

    profit > 0 = guaranteed arbitrage; profit == 0 = no edge; profit < 0
    = no arb exists. We return the breakdown anyway so the UI can show
    "this combination has -1.2% hold" instead of refusing to compute.
    """
    if decimal_odds_a <= 1.0 or decimal_odds_b <= 1.0:
        raise ValueError("decimal odds must be > 1.0")
    if total_stake <= 0:
        raise ValueError("total_stake must be > 0")

    implied_a = 1.0 / decimal_odds_a
    implied_b = 1.0 / decimal_odds_b
    total_implied = implied_a + implied_b

    stake_a = total_stake * implied_a / total_implied
    stake_b = total_stake - stake_a
    payout = stake_a * decimal_odds_a   # same as stake_b * decimal_odds_b
    profit = payout - total_stake
    margin_pct = (1.0 / total_implied - 1.0) * 100.0
    is_arb = total_implied < 1.0

    return {
        "stake_a": round(stake_a, 2),
        "stake_b": round(stake_b, 2),
        "payout": round(payout, 2),
        "profit": round(profit, 2),
        "profit_margin_pct": round(margin_pct, 3),
        "total_implied_pct": round(total_implied * 100.0, 3),
        "is_arbitrage": is_arb,
    }


def calc_hedge(original_decimal: float, original_stake: float,
               hedge_decimal: float, mode: str = "equal") -> dict:
    """Compute the hedge stake against an existing position.

    modes:
      'equal'     — equal payout regardless of outcome (guarantees a
                    specific profit/loss)
      'breakeven' — hedge just enough to recover the original stake;
                    keep upside on the original
      'no_hedge'  — return zero stake; useful for showing the no-hedge
                    P&L side-by-side
    """
    if original_decimal <= 1.0 or hedge_decimal <= 1.0:
        raise ValueError("decimal odds must be > 1.0")
    if original_stake <= 0:
        raise ValueError("original_stake must be > 0")

    original_payout = original_stake * original_decimal

    if mode == "equal":
        # Solve S' such that hedge_decimal * S' == original_payout
        hedge_stake = original_payout / hedge_decimal
    elif mode == "breakeven":
        # Solve S' such that hedge_decimal * S' == original_stake + S'
        # → S' * (hedge_decimal - 1) = original_stake
        # → S' = original_stake / (hedge_decimal - 1)
        hedge_stake = original_stake / (hedge_decimal - 1.0)
    elif mode == "no_hedge":
        hedge_stake = 0.0
    else:
        raise ValueError(f"unknown hedge mode: {mode!r}")

    # Payouts in each scenario (net of all stakes)
    total_outlay = original_stake + hedge_stake
    payout_original_wins = original_payout - total_outlay
    payout_hedge_wins = (hedge_stake * hedge_decimal) - total_outlay

    return {
        "mode": mode,
        "hedge_stake": round(hedge_stake, 2),
        "total_outlay": round(total_outlay, 2),
        "profit_if_original_wins": round(payout_original_wins, 2),
        "profit_if_hedge_wins": round(payout_hedge_wins, 2),
        "guaranteed_min_profit": round(
            min(payout_original_wins, payout_hedge_wins), 2,
        ),
    }


def calc_promo_conversion(free_bet_amount: float, free_bet_decimal: float,
                          hedge_decimal: float,
                          stake_returned: bool = False) -> dict:
    """Convert a "free bet" / boosted-odds offer to expected cash via
    hedging the opposite side at a real book.

    stake_returned=False (standard "free bet"): you win (decimal-1)*amount
    if the free bet wins, 0 otherwise.
    stake_returned=True (boosted-odds bet placed with your own money):
    you win decimal*amount if it wins.

    Returns the hedge stake that maximizes the worst-case payout, plus
    the conversion rate (cash returned / face value).
    """
    if free_bet_decimal <= 1.0 or hedge_decimal <= 1.0:
        raise ValueError("decimal odds must be > 1.0")
    if free_bet_amount <= 0:
        raise ValueError("free_bet_amount must be > 0")

    # Payout if free bet wins (does NOT include the stake when not returned)
    free_bet_payout_if_win = (
        free_bet_amount * free_bet_decimal
        if stake_returned
        else free_bet_amount * (free_bet_decimal - 1.0)
    )

    # Maximize worst-case: hedge_stake * hedge_decimal - hedge_stake
    #                     == free_bet_payout_if_win - hedge_stake
    # → hedge_stake * hedge_decimal == free_bet_payout_if_win
    # → hedge_stake = free_bet_payout_if_win / hedge_decimal
    hedge_stake = free_bet_payout_if_win / hedge_decimal

    # Guaranteed cash either way (the same in both scenarios after the maximize)
    guaranteed_cash = free_bet_payout_if_win - hedge_stake
    conversion_rate = guaranteed_cash / free_bet_amount

    return {
        "hedge_stake": round(hedge_stake, 2),
        "guaranteed_cash": round(guaranteed_cash, 2),
        "conversion_rate_pct": round(conversion_rate * 100.0, 3),
        "free_bet_amount": float(free_bet_amount),
        "stake_returned": stake_returned,
    }


def calc_devig(prob_a_pct: float, prob_b_pct: float) -> dict:
    """Strip vig from a 2-way market. Returns fair probability for each
    side + the implied vig percentage. Several methods exist; we use
    the standard proportional method (the dashboard's matcher uses
    the same approach for de-vigged divergence).
    """
    if not (0.0 < prob_a_pct < 100.0) or not (0.0 < prob_b_pct < 100.0):
        raise ValueError("probabilities must be in (0, 100)")
    total = prob_a_pct + prob_b_pct
    fair_a = prob_a_pct / total * 100.0
    fair_b = prob_b_pct / total * 100.0
    vig = (total - 100.0)
    return {
        "fair_prob_a_pct": round(fair_a, 3),
        "fair_prob_b_pct": round(fair_b, 3),
        "vig_pct": round(vig, 3),
        "total_implied_pct": round(total, 3),
    }


def _kelly_suggested_stake(bankroll: dict, kelly_pct: float | None) -> dict:
    """Compute a Kelly-adjusted stake suggestion for a single bet.

    Inputs:
      bankroll: row from sports_bankroll (or defaults). We use
        current_bankroll, kelly_fraction, max_per_bet_pct.
      kelly_pct: full-Kelly fraction in percent (0-100), as already
        computed by match_and_compare (`kelly_pct` field on outcomes).

    Returns dict with the suggested stake in USD plus the ceiling that
    bound it (helpful for the UI to explain WHY the suggestion is what
    it is — capped by max-per-bet, by available bankroll, or zero).
    """
    current = float(bankroll.get("current_bankroll") or 0)
    frac = float(bankroll.get("kelly_fraction") or DEFAULT_KELLY_FRACTION)
    cap_pct = float(bankroll.get("max_per_bet_pct") or DEFAULT_MAX_PER_BET_PCT)

    if current <= 0 or kelly_pct is None or kelly_pct <= 0:
        return {"stake_usd": 0.0, "kelly_pct": 0.0, "capped_by": "no_edge"}

    # match_and_compare already applies half-Kelly. If the user wants
    # something different (full Kelly = 1.0, quarter-Kelly = 0.25), we
    # rescale: stored kelly_pct = full_kelly * 0.5, so full_kelly = kelly_pct * 2.
    full_kelly_pct = float(kelly_pct) * 2.0
    fractional_pct = full_kelly_pct * frac

    # Two ceilings: user's max-per-bet, and the bankroll itself.
    stake_from_kelly = current * (fractional_pct / 100.0)
    stake_from_cap = current * (cap_pct / 100.0)

    if stake_from_kelly <= stake_from_cap:
        capped_by = "kelly"
        stake = stake_from_kelly
    else:
        capped_by = "max_per_bet_pct"
        stake = stake_from_cap

    return {
        "stake_usd": round(stake, 2),
        "kelly_pct": round(fractional_pct, 3),
        "capped_by": capped_by,
    }


def _get_user_bankroll(user_id: str) -> dict | None:
    with _get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sports_bankroll WHERE user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def _annotate_bankroll(bankroll: dict | None) -> dict | None:
    """Add computed fields the UI cares about: pnl, return%, drawdown
    flag. Returns None when no bankroll is configured."""
    if not bankroll:
        return None
    starting = float(bankroll.get("starting_bankroll") or 0)
    current = float(bankroll.get("current_bankroll") or 0)
    pnl = round(current - starting, 2)
    return_pct = round((pnl / starting) * 100, 3) if starting > 0 else 0.0
    dd_threshold = float(bankroll.get("drawdown_alert_pct")
                          or DEFAULT_DRAWDOWN_ALERT_PCT)
    in_drawdown = (starting > 0) and (return_pct <= -dd_threshold)
    out = dict(bankroll)
    out["pnl"] = pnl
    out["return_pct"] = return_pct
    out["in_drawdown"] = in_drawdown
    return out


@app.get("/api/bankroll")
async def api_get_bankroll(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    row = await asyncio.to_thread(_get_user_bankroll, user["id"])
    return JSONResponse({"bankroll": _annotate_bankroll(row)})


@app.put("/api/bankroll")
async def api_put_bankroll(request: Request):
    """Create or replace the user's bankroll config.

    Body: {"starting_bankroll": float, "current_bankroll": float?,
    "kelly_fraction": float?, "max_per_bet_pct": float?,
    "drawdown_alert_pct": float?}. If current_bankroll is omitted, it
    defaults to starting_bankroll (initial setup case).
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    try:
        starting = float(body.get("starting_bankroll", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "starting_bankroll must be a number"}, status_code=400)
    if starting <= 0 or starting > 10_000_000:
        return JSONResponse({"error": "starting_bankroll must be 0 < x <= 10,000,000"},
                             status_code=400)
    current = body.get("current_bankroll", starting)
    try:
        current = float(current)
    except (TypeError, ValueError):
        return JSONResponse({"error": "current_bankroll must be a number"}, status_code=400)
    if current < 0:
        return JSONResponse({"error": "current_bankroll must be >= 0"}, status_code=400)

    def _clamp_float(name: str, default: float, lo: float, hi: float) -> float:
        if name not in body:
            return default
        try:
            v = float(body[name])
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number")
        if not (lo <= v <= hi):
            raise ValueError(f"{name} out of range")
        return v

    try:
        kelly = _clamp_float("kelly_fraction", DEFAULT_KELLY_FRACTION, 0.0, 1.0)
        cap = _clamp_float("max_per_bet_pct", DEFAULT_MAX_PER_BET_PCT, 0.1, 100.0)
        dd = _clamp_float("drawdown_alert_pct", DEFAULT_DRAWDOWN_ALERT_PCT, 0.5, 100.0)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    with _get_db() as conn:
        conn.execute(
            "INSERT INTO sports_bankroll "
            "(user_id, starting_bankroll, current_bankroll, kelly_fraction, "
            " max_per_bet_pct, drawdown_alert_pct, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "  starting_bankroll = excluded.starting_bankroll, "
            "  current_bankroll = excluded.current_bankroll, "
            "  kelly_fraction = excluded.kelly_fraction, "
            "  max_per_bet_pct = excluded.max_per_bet_pct, "
            "  drawdown_alert_pct = excluded.drawdown_alert_pct, "
            "  updated_at = excluded.updated_at",
            (user["id"], starting, current, kelly, cap, dd),
        )
    row = await asyncio.to_thread(_get_user_bankroll, user["id"])
    return JSONResponse({"bankroll": _annotate_bankroll(row)})


# ---------------------------------------------------------------------------
# API tokens — Bearer auth for programmatic clients (T5.1)
# ---------------------------------------------------------------------------

import secrets as _secrets


@app.post("/api/webhooks/signing-key")
async def api_rotate_webhook_signing_key(request: Request):
    """Generate a new HMAC signing key for this user's webhook payloads.

    Returns the plaintext ONCE; the user must record it. The key is
    encrypted at rest via the same Fernet key used for Telegram tokens.
    Rotating invalidates the previous key.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if user.get("_bearer_token_id"):
        return JSONResponse(
            {"error": "Bearer tokens cannot rotate webhook keys"},
            status_code=403,
        )
    plaintext = "whsec_" + _secrets.token_urlsafe(32)
    encrypted = _encrypt_field(plaintext)
    with _get_db() as conn:
        # Ensure a row exists in sports_alert_config; PUT-like upsert.
        conn.execute(
            "INSERT INTO sports_alert_config (user_id, webhook_signing_key) "
            "VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "  webhook_signing_key = excluded.webhook_signing_key",
            (user["id"], encrypted),
        )
    return JSONResponse({
        "signing_key": plaintext,
        "warning": ("Save this key now — it's encrypted at rest and "
                    "not retrievable later. Use it to verify "
                    "X-Sharpe-Signature on incoming webhooks."),
    })


@app.delete("/api/webhooks/signing-key")
async def api_revoke_webhook_signing_key(request: Request):
    """Clear the user's signing key — future webhooks fire unsigned."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if user.get("_bearer_token_id"):
        return JSONResponse(
            {"error": "Bearer tokens cannot revoke webhook keys"},
            status_code=403,
        )
    with _get_db() as conn:
        conn.execute(
            "UPDATE sports_alert_config SET webhook_signing_key = '' "
            "WHERE user_id = ?",
            (user["id"],),
        )
    return JSONResponse({"status": "ok"})


@app.post("/api/webhooks/test")
async def api_test_webhook(request: Request):
    """Fire a test webhook to the user's configured webhook_url, signed
    with their current key if one is set. Useful for verifying the
    signature flow before relying on it for production alerts."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    with _get_db() as conn:
        row = conn.execute(
            "SELECT webhook_url FROM sports_alert_config WHERE user_id = ?",
            (user["id"],),
        ).fetchone()
    if not row or not row["webhook_url"]:
        return JSONResponse({"error": "no webhook_url configured"}, status_code=400)
    key = _get_webhook_signing_key(user["id"])
    payload = {
        "kind": "test",
        "ts": int(time.time()),
        "message": "Sharpe webhook test — if you can verify this signature, you're good to go.",
    }
    ok = await asyncio.to_thread(_signed_webhook_post, row["webhook_url"], payload, key)
    return JSONResponse({"status": "ok" if ok else "failed", "signed": bool(key)})


@app.get("/api/auth/tokens")
async def api_list_tokens(request: Request):
    """List the current user's API tokens. Plaintext tokens are never
    returned — only metadata + prefix for visual identification."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if user.get("_bearer_token_id"):
        # A Bearer token can't list itself / other tokens — that's a
        # session-only operation.
        return JSONResponse(
            {"error": "Bearer tokens cannot manage tokens; use a session"},
            status_code=403,
        )
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, token_prefix, scopes, created_at, last_used_at, "
            "       revoked_at "
            "FROM sports_api_tokens WHERE user_id = ? ORDER BY id DESC",
            (user["id"],),
        ).fetchall()
    return JSONResponse({"tokens": [dict(r) for r in rows]})


@app.post("/api/auth/tokens")
async def api_create_token(request: Request):
    """Create a new API token. Returns the plaintext token ONCE.

    Body: {"name": str (optional), "scopes": list[str] (optional)}.
    The plaintext token is generated server-side and never persisted —
    only the SHA-256 hash + a short prefix for visual identification.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if user.get("_bearer_token_id"):
        return JSONResponse(
            {"error": "Bearer tokens cannot create tokens; use a session"},
            status_code=403,
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    name = (body.get("name") or "")[:80]
    raw_scopes = body.get("scopes") or []
    if not isinstance(raw_scopes, list) or not all(isinstance(s, str) for s in raw_scopes):
        return JSONResponse({"error": "scopes must be a list of strings"}, status_code=400)
    scopes = json.dumps(raw_scopes)

    # Token format: "shrp_" + 40 url-safe chars. ~240 bits of entropy.
    plaintext = "shrp_" + _secrets.token_urlsafe(30)
    token_hash = _hash_api_token(plaintext)
    prefix = plaintext[:8]

    with _get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sports_api_tokens "
            "(user_id, name, token_hash, token_prefix, scopes) "
            "VALUES (?, ?, ?, ?, ?)",
            (user["id"], name, token_hash, prefix, scopes),
        )
        token_id = cur.lastrowid

    return JSONResponse({
        "id": token_id,
        "name": name,
        "token": plaintext,  # only time the plaintext is returned
        "token_prefix": prefix,
        "scopes": raw_scopes,
        "warning": ("Save this token now — it is not retrievable later. "
                    "Use as Authorization: Bearer <token>"),
    })


@app.delete("/api/auth/tokens/{token_id}")
async def api_revoke_token(token_id: int, request: Request):
    """Revoke a token. Idempotent — revoking an already-revoked token
    returns 200 (Bearer auth fails immediately for revoked rows)."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if user.get("_bearer_token_id"):
        return JSONResponse(
            {"error": "Bearer tokens cannot revoke tokens; use a session"},
            status_code=403,
        )
    with _get_db() as conn:
        cur = conn.execute(
            "UPDATE sports_api_tokens SET revoked_at = datetime('now') "
            "WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            (token_id, user["id"]),
        )
        if cur.rowcount == 0:
            # Either doesn't exist or already revoked — return 404 only
            # if no row matches the user at all
            exists = conn.execute(
                "SELECT 1 FROM sports_api_tokens WHERE id = ? AND user_id = ?",
                (token_id, user["id"]),
            ).fetchone()
            if not exists:
                return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"status": "ok"})


@app.post("/api/bankroll/suggest-stake")
async def api_bankroll_suggest_stake(request: Request):
    """Return a Kelly-adjusted stake suggestion for a given kelly_pct.

    Body: {"kelly_pct": float}. Uses the user's current bankroll config;
    returns 404 if no bankroll is set so the UI can prompt the user to
    configure one before showing suggestions.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    kelly_pct = body.get("kelly_pct")
    try:
        kelly_pct = float(kelly_pct) if kelly_pct is not None else None
    except (TypeError, ValueError):
        return JSONResponse({"error": "kelly_pct must be a number"}, status_code=400)

    row = await asyncio.to_thread(_get_user_bankroll, user["id"])
    if not row:
        return JSONResponse({"error": "no bankroll configured"}, status_code=404)
    suggestion = _kelly_suggested_stake(row, kelly_pct)
    annotated = _annotate_bankroll(row)
    return JSONResponse({
        "suggestion": suggestion,
        "in_drawdown": annotated.get("in_drawdown", False) if annotated else False,
    })


# ---------------------------------------------------------------------------
# Calculators (public — pure math, no auth needed)
# ---------------------------------------------------------------------------

def _float_or_400(body: dict, key: str) -> tuple[float, JSONResponse | None]:
    """Coerce body[key] to float; return (value, None) or (0.0, response)."""
    if key not in body:
        return 0.0, JSONResponse(
            {"error": f"missing required field: {key}"}, status_code=400,
        )
    try:
        return float(body[key]), None
    except (TypeError, ValueError):
        return 0.0, JSONResponse(
            {"error": f"{key} must be a number"}, status_code=400,
        )


@app.post("/api/calc/odds-convert")
async def api_calc_odds_convert(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    value, err = _float_or_400(body, "value")
    if err:
        return err
    fmt = (body.get("format") or "").strip()
    try:
        return JSONResponse(calc_odds_convert(value, fmt))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/calc/arbitrage")
async def api_calc_arbitrage(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    a, err = _float_or_400(body, "decimal_odds_a")
    if err:
        return err
    b, err = _float_or_400(body, "decimal_odds_b")
    if err:
        return err
    stake = body.get("total_stake", 100.0)
    try:
        stake = float(stake)
    except (TypeError, ValueError):
        return JSONResponse({"error": "total_stake must be a number"}, status_code=400)
    try:
        return JSONResponse(calc_arbitrage(a, b, stake))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/calc/hedge")
async def api_calc_hedge(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    orig_dec, err = _float_or_400(body, "original_decimal")
    if err:
        return err
    orig_stake, err = _float_or_400(body, "original_stake")
    if err:
        return err
    hedge_dec, err = _float_or_400(body, "hedge_decimal")
    if err:
        return err
    mode = (body.get("mode") or "equal").lower()
    try:
        return JSONResponse(calc_hedge(orig_dec, orig_stake, hedge_dec, mode))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/calc/promo-conversion")
async def api_calc_promo_conversion(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    amt, err = _float_or_400(body, "free_bet_amount")
    if err:
        return err
    fb_dec, err = _float_or_400(body, "free_bet_decimal")
    if err:
        return err
    hedge_dec, err = _float_or_400(body, "hedge_decimal")
    if err:
        return err
    stake_returned = bool(body.get("stake_returned", False))
    try:
        return JSONResponse(
            calc_promo_conversion(amt, fb_dec, hedge_dec, stake_returned),
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/calc/devig")
async def api_calc_devig(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    a, err = _float_or_400(body, "prob_a_pct")
    if err:
        return err
    b, err = _float_or_400(body, "prob_b_pct")
    if err:
        return err
    try:
        return JSONResponse(calc_devig(a, b))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/calculators", response_class=HTMLResponse)
async def calculators_page(request: Request):
    """Public page with the standalone bettor calculators."""
    return HTMLResponse(_load_template("calculators"))


# ---------------------------------------------------------------------------
# Watchlist endpoints
# ---------------------------------------------------------------------------

@app.post("/api/watchlist")
async def add_watchlist(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    body = await request.json()
    market_key = body.get("market_key", "")
    home_team = body.get("home_team", "")
    away_team = body.get("away_team", "")
    if not market_key:
        return JSONResponse({"error": "market_key required"}, status_code=400)
    with _get_db() as conn:
        # Check for existing entry (UNIQUE constraint on user_id, market_key)
        existing = conn.execute(
            "SELECT id FROM sports_watchlist WHERE user_id = ? AND market_key = ? LIMIT 1",
            (user["id"], market_key),
        ).fetchone()
        if existing:
            return JSONResponse({"error": "Already in watchlist"}, status_code=409)
        cur = conn.execute(
            "INSERT INTO sports_watchlist (user_id, market_key, home_team, away_team) VALUES (?, ?, ?, ?)",
            (user["id"], market_key, home_team, away_team),
        )
        item_id = cur.lastrowid
    return JSONResponse({"status": "ok", "id": item_id})


@app.delete("/api/watchlist/{item_id}")
async def remove_watchlist(item_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    with _get_db() as conn:
        conn.execute(
            "DELETE FROM sports_watchlist WHERE id = ? AND user_id = ?",
            (item_id, user["id"]),
        )
    return JSONResponse({"status": "ok"})


@app.get("/api/watchlist")
async def list_watchlist(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sports_watchlist WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
    return JSONResponse({"watchlist": [dict(r) for r in rows]})


@app.patch("/api/watchlist/{item_id}")
async def update_watchlist_threshold(item_id: int, request: Request):
    """Set or clear the divergence-threshold (in pp) for a watchlist item.

    POST body: {"alert_threshold_pp": 5.0} or {"alert_threshold_pp": null}.
    A null/0/missing threshold disables per-item alerting (the item still
    appears in the watchlist; broadcast alerts via /api/alerts still apply
    if configured).
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw = body.get("alert_threshold_pp")
    if raw is None or raw == "":
        threshold = None
    else:
        try:
            threshold = float(raw)
            if threshold <= 0 or threshold > 100:
                threshold = None
        except (TypeError, ValueError):
            return JSONResponse({"error": "invalid threshold"}, status_code=400)
    with _get_db() as conn:
        cur = conn.execute(
            "UPDATE sports_watchlist SET alert_threshold_pp = ? WHERE id = ? AND user_id = ?",
            (threshold, item_id, user["id"]),
        )
        if cur.rowcount == 0:
            return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"status": "ok", "alert_threshold_pp": threshold})


# ---------------------------------------------------------------------------
# Rule-based alerts CRUD
# ---------------------------------------------------------------------------

_ALLOWED_MARKET_TYPES = {"h2h", "spreads", "totals", "futures", "props"}
_ALLOWED_CHANNELS = {"telegram", "webhook", "push", "both"}


def _validate_rule_body(body: dict) -> tuple[dict | None, str | None]:
    """Coerce + validate a rule POST/PATCH body. Returns (clean_dict, error_msg).
    Only fields that are present in `body` are returned so PATCH can do
    partial updates."""
    out: dict = {}
    if "name" in body:
        out["name"] = str(body.get("name") or "")[:80]
    if "enabled" in body:
        out["enabled"] = 1 if body.get("enabled") else 0
    if "sports" in body:
        sports = body.get("sports") or []
        if not isinstance(sports, list) or not all(isinstance(s, str) for s in sports):
            return None, "sports must be a list of strings"
        out["sports"] = json.dumps(sports)
    if "market_types" in body:
        mts = body.get("market_types") or []
        if not isinstance(mts, list):
            return None, "market_types must be a list"
        if not all(m in _ALLOWED_MARKET_TYPES for m in mts):
            return None, f"market_types must be a subset of {sorted(_ALLOWED_MARKET_TYPES)}"
        out["market_types"] = json.dumps(mts)
    if "min_divergence_pp" in body:
        try:
            v = float(body["min_divergence_pp"])
            if not (0 <= v <= 100):
                return None, "min_divergence_pp out of range"
            out["min_divergence_pp"] = v
        except (TypeError, ValueError):
            return None, "min_divergence_pp must be a number"
    if "min_volume" in body:
        raw = body["min_volume"]
        if raw is None or raw == "":
            out["min_volume"] = None
        else:
            try:
                out["min_volume"] = max(0.0, float(raw))
            except (TypeError, ValueError):
                return None, "min_volume must be a number"
    if "max_time_to_event_hours" in body:
        raw = body["max_time_to_event_hours"]
        if raw is None or raw == "":
            out["max_time_to_event_hours"] = None
        else:
            try:
                out["max_time_to_event_hours"] = max(0.0, float(raw))
            except (TypeError, ValueError):
                return None, "max_time_to_event_hours must be a number"
    for flag in ("require_sharp_consensus", "require_not_stale", "require_liquidity_ok"):
        if flag in body:
            out[flag] = 1 if body.get(flag) else 0
    if "channel" in body:
        ch = (body.get("channel") or "telegram").lower()
        if ch not in _ALLOWED_CHANNELS:
            return None, f"channel must be one of {sorted(_ALLOWED_CHANNELS)}"
        out["channel"] = ch
    for hr_field in ("quiet_hours_start", "quiet_hours_end"):
        if hr_field in body:
            raw = body[hr_field]
            if raw is None or raw == "":
                out[hr_field] = None
            else:
                try:
                    h = int(raw)
                    if not (0 <= h <= 23):
                        return None, f"{hr_field} must be 0-23"
                    out[hr_field] = h
                except (TypeError, ValueError):
                    return None, f"{hr_field} must be an integer 0-23"
    if "cooldown_secs" in body:
        try:
            v = int(body["cooldown_secs"])
            if not (10 <= v <= 86400):
                return None, "cooldown_secs must be 10..86400"
            out["cooldown_secs"] = v
        except (TypeError, ValueError):
            return None, "cooldown_secs must be an integer"
    return out, None


@app.get("/api/alert-rules")
async def list_alert_rules(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sports_alert_rules WHERE user_id = ? ORDER BY id ASC",
            (user["id"],),
        ).fetchall()
    rules = []
    for r in rows:
        d = dict(r)
        # Parse JSON fields back so the client doesn't have to
        try:
            d["sports"] = json.loads(d.get("sports") or "[]")
        except (TypeError, ValueError):
            d["sports"] = []
        try:
            d["market_types"] = json.loads(d.get("market_types") or "[]")
        except (TypeError, ValueError):
            d["market_types"] = []
        rules.append(d)
    return JSONResponse({"rules": rules})


@app.post("/api/alert-rules")
async def create_alert_rule(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    fields, err = _validate_rule_body(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    fields.setdefault("name", "")
    fields.setdefault("enabled", 1)
    fields.setdefault("sports", "[]")
    fields.setdefault("market_types", "[]")
    fields.setdefault("min_divergence_pp", 5.0)
    fields.setdefault("require_sharp_consensus", 1)
    fields.setdefault("require_not_stale", 1)
    fields.setdefault("require_liquidity_ok", 1)
    fields.setdefault("channel", "telegram")
    fields.setdefault("cooldown_secs", 300)
    cols = ["user_id"] + list(fields.keys())
    placeholders = ",".join("?" for _ in cols)
    values = [user["id"]] + list(fields.values())
    with _get_db() as conn:
        cur = conn.execute(
            f"INSERT INTO sports_alert_rules ({','.join(cols)}) VALUES ({placeholders})",
            values,
        )
        rule_id = cur.lastrowid
    return JSONResponse({"status": "ok", "id": rule_id})


@app.patch("/api/alert-rules/{rule_id}")
async def update_alert_rule(rule_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    fields, err = _validate_rule_body(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    if not fields:
        return JSONResponse({"error": "no fields to update"}, status_code=400)
    set_clause = ",".join(f"{k} = ?" for k in fields.keys())
    with _get_db() as conn:
        cur = conn.execute(
            f"UPDATE sports_alert_rules SET {set_clause} WHERE id = ? AND user_id = ?",
            list(fields.values()) + [rule_id, user["id"]],
        )
        if cur.rowcount == 0:
            return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"status": "ok"})


@app.delete("/api/alert-rules/{rule_id}")
async def delete_alert_rule(rule_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    with _get_db() as conn:
        cur = conn.execute(
            "DELETE FROM sports_alert_rules WHERE id = ? AND user_id = ?",
            (rule_id, user["id"]),
        )
        if cur.rowcount == 0:
            return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Web Push: subscription CRUD
# ---------------------------------------------------------------------------

@app.get("/api/push/vapid-public-key")
async def push_vapid_public_key():
    """Public key the browser needs to register a subscription. Anonymous —
    the key is public by design. Returns 503 if push isn't configured."""
    if not VAPID_PUBLIC_KEY:
        return JSONResponse(
            {"error": "Web Push not configured (VAPID_PUBLIC_KEY unset)"},
            status_code=503,
        )
    return JSONResponse({"public_key": VAPID_PUBLIC_KEY,
                          "push_available": _PUSH_AVAILABLE})


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    """Persist a PushManager.subscribe() result. Idempotent on
    (user_id, endpoint)."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    endpoint = body.get("endpoint") or ""
    keys = body.get("keys") or {}
    p256dh = keys.get("p256dh") or ""
    auth = keys.get("auth") or ""
    if not (endpoint and p256dh and auth):
        return JSONResponse({"error": "endpoint + keys.p256dh + keys.auth required"}, status_code=400)
    if not endpoint.startswith("https://"):
        return JSONResponse({"error": "endpoint must be HTTPS"}, status_code=400)
    ua = (request.headers.get("user-agent") or "")[:200]
    with _get_db() as conn:
        # Upsert by (user_id, endpoint) — keys can rotate
        conn.execute(
            """INSERT INTO sports_push_subscriptions (user_id, endpoint, p256dh, auth, user_agent)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, endpoint) DO UPDATE SET
                 p256dh = excluded.p256dh,
                 auth = excluded.auth,
                 user_agent = excluded.user_agent""",
            (user["id"], endpoint, p256dh, auth, ua),
        )
    return JSONResponse({"status": "ok"})


@app.delete("/api/push/subscribe")
async def push_unsubscribe(request: Request):
    """Remove a push subscription by its endpoint."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    endpoint = body.get("endpoint") or ""
    if not endpoint:
        return JSONResponse({"error": "endpoint required"}, status_code=400)
    with _get_db() as conn:
        cur = conn.execute(
            "DELETE FROM sports_push_subscriptions WHERE user_id = ? AND endpoint = ?",
            (user["id"], endpoint),
        )
    return JSONResponse({"status": "ok", "deleted": cur.rowcount})


@app.post("/api/push/test")
async def push_test(request: Request):
    """Fire a test push notification to the current user. Useful for
    debugging the subscription flow without waiting for a real signal."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    delivered = _send_web_push(user["id"], {
        "title": "Sharpe — test push",
        "body": "If you see this, push is wired up correctly.",
        "tag": "sharpe-test",
        "data": {"url": "/"},
    })
    return JSONResponse({"status": "ok", "delivered": delivered,
                          "push_available": _PUSH_AVAILABLE})


# ---------------------------------------------------------------------------
# AI-explained signals (Claude)
# ---------------------------------------------------------------------------
#
# For every flagged signal, generate a plain-English "why this is mispriced"
# explanation via Claude. Two layers of caching keep cost down:
#   1. Prompt caching: the system prompt is identical across all calls, so
#      we mark it with cache_control and Anthropic serves repeated requests
#      at ~10% the normal input price.
#   2. DB cache: we hash the signal's identity (event + outcome + divergence
#      rounded to 0.1pp + sport) and cache the explanation for 30 min, so
#      multiple users viewing the same signal cost one API call total.

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
EXPLAIN_MODEL = os.getenv("EXPLAIN_MODEL", "claude-opus-4-7")
EXPLAIN_CACHE_TTL_SECONDS = int(os.getenv("EXPLAIN_CACHE_TTL_SECONDS", "1800"))

_anthropic_client = None

_EXPLAIN_SYSTEM_PROMPT = """You are a sharp sports-betting analyst writing concise plain-English explanations of why a specific market divergence exists between a bookmaker consensus and a prediction-market venue (Polymarket or Kalshi).

Each user message is a single JSON object describing ONE comparison: event, outcome, sharp bookmaker probability, prediction-market price, divergence in percentage points, plus context (vig, liquidity, time-to-event, books present).

Write a clear explanation for experienced bettors. Cover, in order:
1. WHICH side is mispricing (book consensus vs prediction market) and by how much.
2. WHY this gap likely exists — slow-to-react market, retail vs sharp money mix, low liquidity, recent news catalyst, vig differences.
3. WHAT would close it — a steam move on the slow venue, a liquidity event, or resolution.

Constraints:
- Maximum 3 sentences total.
- No financial-advice disclaimers, no emoji, no markdown formatting.
- If |divergence| < 3pp, note that fees likely eat the edge.
- Don't recommend specific stake sizes or guarantee outcomes.
- Use specific numbers from the input — vague generalities fail this task.
"""


def _get_anthropic_client():
    """Lazy-init the Anthropic client. Returns None if SDK or API key is missing."""
    global _anthropic_client
    if _anthropic_client is not None:
        return _anthropic_client
    if not ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic SDK not installed — /api/signals/explain disabled")
        return None
    _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client


def _signal_cache_key(signal: dict) -> str:
    """Stable hash for the signal's identity.

    Rounded to 0.1pp on divergence so tiny line wobbles don't cause cache
    misses on what's effectively the same signal. SHA-256 truncated to 32
    hex chars — collision probability is negligible at our volumes.
    """
    parts = (
        (signal.get("home_team") or "").strip().lower(),
        (signal.get("away_team") or "").strip().lower(),
        (signal.get("outcome") or signal.get("outcome_name") or "").strip().lower(),
        round(float(signal.get("divergence") or signal.get("divergence_pct") or 0), 1),
        (signal.get("sport") or "").strip().lower(),
    )
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _get_cached_explanation(cache_key: str) -> str | None:
    """Return cached explanation if within TTL; None otherwise.

    SQLite's `datetime('now')` writes `YYYY-MM-DD HH:MM:SS` (no timezone,
    space separator) — we format the cutoff to match so lexicographic
    comparison in the WHERE clause works correctly. Don't use
    `datetime.isoformat()` here: its 'T' separator sorts ABOVE space and
    causes every cached row to look stale.
    """
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=EXPLAIN_CACHE_TTL_SECONDS))
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    with _get_db() as conn:
        row = conn.execute(
            "SELECT explanation FROM sports_signal_explanations "
            "WHERE cache_key = ? AND created_at >= ? LIMIT 1",
            (cache_key, cutoff_str),
        ).fetchone()
    return row["explanation"] if row else None


def _store_explanation(cache_key: str, signal: dict, explanation: str, model: str) -> None:
    summary = json.dumps({
        "event": f"{signal.get('home_team', '')} vs {signal.get('away_team', '')}",
        "outcome": signal.get("outcome") or signal.get("outcome_name"),
        "divergence": signal.get("divergence"),
        "sport": signal.get("sport"),
    }, separators=(",", ":"))
    with _get_db() as conn:
        conn.execute(
            "INSERT INTO sports_signal_explanations "
            "(cache_key, signal_summary, explanation, model) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET "
            "  explanation = excluded.explanation, "
            "  model = excluded.model, "
            "  created_at = datetime('now')",
            (cache_key, summary, explanation, model),
        )


def _build_explain_payload(signal: dict) -> dict:
    """Project a comparison/signal dict into the fields the prompt cares about."""
    return {
        "sport": signal.get("sport") or "unknown",
        "event": _event_name(signal.get("home_team"), signal.get("away_team")),
        "outcome": signal.get("outcome") or signal.get("outcome_name") or "",
        "sharp_book_prob_pct": signal.get("sharp_prob"),
        "consensus_devigged_pct": (signal.get("consensus_over_devigged")
                                    or signal.get("true_prob_no_vig")
                                    or signal.get("consensus_prob")),
        "polymarket_price_pct": signal.get("poly_prob"),
        "kalshi_price_pct": signal.get("kalshi_prob"),
        "divergence_pp": signal.get("divergence") or signal.get("divergence_pct"),
        "vig_pct": signal.get("vig_pct") or signal.get("implied_vig"),
        "books_present": signal.get("sharp_books_present") or [],
        "poly_volume_usd": signal.get("poly_volume"),
        "poly_spread_pct": signal.get("spread_pct") or signal.get("poly_spread"),
        "time_to_event_hours": signal.get("time_to_event_hours"),
        "kelly_pct": signal.get("kelly_pct"),
    }


def _explain_signal_via_claude(signal: dict) -> str:
    """Generate a plain-English explanation via Claude. Returns empty string
    if the SDK or API key isn't configured.

    Uses prompt caching on the system prompt so repeated calls in the same
    5-min window share a cached prefix (~10% cost on the system tokens).
    No streaming — outputs are short (≤300 tokens) and we want the full
    string in one go for the API response.
    """
    client = _get_anthropic_client()
    if client is None:
        return ""

    response = client.messages.create(
        model=EXPLAIN_MODEL,
        max_tokens=300,
        thinking={"type": "disabled"},  # 2-3 sentence task, no reasoning needed
        system=[{
            "type": "text",
            "text": _EXPLAIN_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{
            "role": "user",
            "content": json.dumps(_build_explain_payload(signal),
                                    separators=(",", ":"), sort_keys=True),
        }],
    )
    for block in response.content:
        if block.type == "text":
            return block.text.strip()
    return ""


@app.post("/api/signals/explain")
async def api_explain_signal(request: Request):
    """Return a plain-English explanation of why a signal is mispriced.

    POST body: a comparison/signal dict (home_team, away_team, outcome,
    sharp_prob, poly_prob, divergence, sport, ...). Returns
    {"explanation": str, "cached": bool}. Cached for EXPLAIN_CACHE_TTL_SECONDS
    by signal identity (event + outcome + divergence rounded to 0.1pp + sport).
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        signal = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(signal, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    # Require at least an outcome OR home_team so we have something identifying
    if not (signal.get("outcome") or signal.get("outcome_name") or signal.get("home_team")):
        return JSONResponse({"error": "signal missing event/outcome"}, status_code=400)

    cache_key = _signal_cache_key(signal)
    cached = await asyncio.to_thread(_get_cached_explanation, cache_key)
    if cached:
        M_EXPLAIN_REQUESTS.labels(result="cache_hit").inc()
        return JSONResponse({"explanation": cached, "cached": True})

    if not ANTHROPIC_API_KEY:
        M_EXPLAIN_REQUESTS.labels(result="disabled").inc()
        return JSONResponse(
            {"error": "AI explanations not configured (ANTHROPIC_API_KEY unset)",
             "explanation": None, "cached": False},
            status_code=503,
        )

    try:
        explanation = await asyncio.to_thread(_explain_signal_via_claude, signal)
    except Exception as e:
        M_EXPLAIN_REQUESTS.labels(result="error").inc()
        log.warning("Claude explanation error: %s", e)
        return JSONResponse({"error": f"explanation failed: {e}"}, status_code=502)

    if not explanation:
        M_EXPLAIN_REQUESTS.labels(result="error").inc()
        return JSONResponse({"error": "empty explanation from model"}, status_code=502)

    await asyncio.to_thread(_store_explanation, cache_key, signal, explanation, EXPLAIN_MODEL)
    M_EXPLAIN_REQUESTS.labels(result="api_call").inc()
    return JSONResponse({"explanation": explanation, "cached": False})


# ---------------------------------------------------------------------------
# Smart-money mirror — Polymarket top-trader positions on sports markets
# ---------------------------------------------------------------------------
#
# We already fetch top_trader_positions for the leaderboard wallets every
# 10 minutes via _refresh_top_trader_positions. Each row in that table is
# (wallet, condition_id, outcome, net_size, avg_price, ...). We also
# already attach matching positions to every comparison via
# _attach_top_traders_to_comparisons. What was missing: a dedicated UI
# surface that says "of the top 50 Polymarket whales, N of them hold
# position X on this market" — a powerful conversion signal because
# nobody else surfaces this for sports.

def _smart_money_for_comparisons(comparisons: list[dict]) -> list[dict]:
    """For each comparison with at least one matching whale position,
    return an enriched row: condition_id, event, outcome, top-trader
    wallets in/against the position, aggregate USD exposure, and the
    most-recent timestamp."""
    out: list[dict] = []
    for c in comparisons or []:
        positions = c.get("top_trader_positions") or []
        if not positions:
            continue

        # Group by outcome side
        by_outcome: dict[str, list[dict]] = {}
        for p in positions:
            outcome = p.get("outcome") or ""
            by_outcome.setdefault(outcome, []).append(p)

        sides = []
        for outcome, ps in by_outcome.items():
            net_usd = sum(float(p.get("net_usd") or 0) for p in ps)
            if abs(net_usd) < 50:  # ignore dust positions
                continue
            avg_entry = (
                sum(float(p.get("avg_price") or 0) * abs(float(p.get("net_size") or 0))
                    for p in ps)
                / max(sum(abs(float(p.get("net_size") or 0)) for p in ps), 1.0)
            )
            top_wallets = sorted(
                ps,
                key=lambda x: -abs(float(x.get("net_usd") or 0)),
            )[:5]
            most_recent = max(
                (int(p.get("last_traded_ts") or 0) for p in ps),
                default=0,
            )
            sides.append({
                "outcome": outcome,
                "n_whales": len(ps),
                "net_usd": round(net_usd, 2),
                "avg_entry_price": round(avg_entry, 4),
                "last_trade_ts": most_recent,
                "top_wallets": [
                    {
                        "wallet": w.get("wallet"),
                        "pseudonym": w.get("pseudonym") or "",
                        "name": w.get("name") or "",
                        "rank": w.get("rank"),
                        "net_usd": round(float(w.get("net_usd") or 0), 2),
                        "net_size": round(float(w.get("net_size") or 0), 2),
                        "avg_price": round(float(w.get("avg_price") or 0), 4),
                        "last_side": w.get("last_side") or "",
                    } for w in top_wallets
                ],
            })

        if not sides:
            continue

        # Sort sides by absolute USD exposure (biggest conviction first)
        sides.sort(key=lambda s: -abs(s["net_usd"]))

        out.append({
            "event": _event_name(c.get("home_team"), c.get("away_team")),
            "home_team": c.get("home_team", ""),
            "away_team": c.get("away_team", ""),
            "sport": c.get("sport"),  # may be None on legacy comparisons
            "commence_time": c.get("commence_time", ""),
            "condition_id": c.get("condition_id", ""),
            "poly_slug": c.get("poly_slug"),
            "poly_question": c.get("poly_question"),
            "trade_poly_url": c.get("trade_poly_url"),
            "has_signal": bool(c.get("has_signal")),
            "max_divergence": c.get("max_divergence"),
            "sides": sides,
            "total_whales": sum(s["n_whales"] for s in sides),
            "total_usd": round(sum(abs(s["net_usd"]) for s in sides), 2),
        })

    # Sort by total whale exposure desc
    out.sort(key=lambda r: -r["total_usd"])
    return out


@app.get("/api/smart-money")
async def api_smart_money(request: Request):
    """Smart-money positions overlaid on current sports comparisons.

    For each market with at least one top-50-wallet position, returns the
    aggregated whale exposure by side, the top-5 wallets per side, and
    the most-recent trade timestamp.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    requested_sport = request.query_params.get("sport")
    async with _data_lock:
        active_sport = dashboard_data.get("active_sport")
        snapshot = copy.deepcopy(dashboard_data.get("comparisons") or [])

    if requested_sport and requested_sport != active_sport:
        return JSONResponse(
            {"status": "switching", "active_sport": active_sport,
             "requested_sport": requested_sport, "rows": []},
            status_code=202,
        )

    rows = _smart_money_for_comparisons(snapshot)
    return JSONResponse({
        "sport": active_sport,
        "n_markets": len(rows),
        "n_whales_unique": len({
            w["wallet"]
            for r in rows for s in r["sides"] for w in s["top_wallets"]
            if w.get("wallet")
        }),
        "rows": rows,
    })


@app.get("/smart-money", response_class=HTMLResponse)
async def smart_money_page(request: Request):
    """UI for smart-money positions overlaid on sports comparisons."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return HTMLResponse(_load_template("smart_money"))


# ---------------------------------------------------------------------------
# Layout customization endpoints
# ---------------------------------------------------------------------------

@app.get("/api/layout")
async def get_layout(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    with _get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sports_user_layout WHERE user_id = ? LIMIT 1", (user["id"],)
        ).fetchone()
    if row:
        r = dict(row)
        # visible_widgets and visible_data_points are stored as JSON text in SQLite
        widgets = r.get("visible_widgets", '["stats","top_opps","hero","events"]')
        data_points = r.get("visible_data_points", "[]")
        if isinstance(widgets, str):
            try:
                widgets = json.loads(widgets)
            except (json.JSONDecodeError, TypeError):
                widgets = ["stats", "top_opps", "hero", "events"]
        if isinstance(data_points, str):
            try:
                data_points = json.loads(data_points)
            except (json.JSONDecodeError, TypeError):
                data_points = []
        return JSONResponse({
            "visible_widgets": widgets,
            "visible_data_points": data_points,
            "card_expanded_default": bool(r.get("card_expanded_default", 0)),
        })
    return JSONResponse({
        "visible_widgets": ["stats", "top_opps", "hero", "events"],
        "visible_data_points": ["volume", "spread", "sharp_book", "bookmakers", "24h_change",
                                "match_confidence", "kelly", "edge", "consensus", "sharp_prob", "poly_prob"],
        "card_expanded_default": False,
    })


@app.post("/api/layout")
async def save_layout(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Body must be an object"}, status_code=400)
    visible_widgets = body.get("visible_widgets", ["stats", "top_opps", "hero", "events"])
    visible_data_points = body.get("visible_data_points", [])
    if not isinstance(visible_widgets, list):
        visible_widgets = ["stats", "top_opps", "hero", "events"]
    if not isinstance(visible_data_points, list):
        visible_data_points = []
    # Coerce card_expanded_default to a 0/1 int. The previous code did
    # int(body.get(...)) which crashes on strings like "true" or arbitrary
    # JSON values. Treat any truthy non-bool as True/1.
    raw_expanded = body.get("card_expanded_default", False)
    if isinstance(raw_expanded, bool):
        card_expanded_default = 1 if raw_expanded else 0
    elif isinstance(raw_expanded, (int, float)):
        card_expanded_default = 1 if raw_expanded else 0
    elif isinstance(raw_expanded, str):
        card_expanded_default = 1 if raw_expanded.strip().lower() in ("1", "true", "yes", "on") else 0
    else:
        card_expanded_default = 0
    with _get_db() as conn:
        conn.execute(
            """INSERT INTO sports_user_layout (user_id, visible_widgets, visible_data_points, card_expanded_default)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   visible_widgets = excluded.visible_widgets,
                   visible_data_points = excluded.visible_data_points,
                   card_expanded_default = excluded.card_expanded_default""",
            (user["id"], json.dumps(visible_widgets), json.dumps(visible_data_points), card_expanded_default),
        )
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Historical data (pro-only)
# ---------------------------------------------------------------------------

# Sport keyword map for classifying Polymarket resolved events
_HIST_SPORT_KEYWORDS: dict[str, list[str]] = {
    "basketball_nba": ["nba", "lakers", "celtics", "warriors", "bucks", "76ers", "knicks", "nuggets", "heat", "nets",
                       "cavaliers", "hawks", "bulls", "mavs", "mavericks", "suns", "kings", "pacers", "thunder",
                       "timberwolves", "clippers", "rockets", "grizzlies", "pelicans", "spurs", "magic", "raptors",
                       "pistons", "hornets", "trail blazers", "blazers", "jazz", "wizards"],
    "americanfootball_nfl": ["nfl", "super bowl", "chiefs", "eagles", "49ers", "cowboys", "bills", "ravens",
                            "dolphins", "bengals", "lions", "packers", "texans", "steelers", "jets",
                            "patriots", "raiders", "chargers", "seahawks", "rams", "bears", "saints",
                            "colts", "jaguars", "broncos", "falcons", "cardinals", "titans", "panthers",
                            "giants", "vikings", "browns", "commanders", "buccaneers", "bucs"],
    "icehockey_nhl": ["nhl", "stanley cup"],
    "baseball_mlb": ["mlb", "world series"],
    "soccer_epl": ["premier league", "epl", "arsenal", "man city", "manchester city", "liverpool",
                   "chelsea", "tottenham", "spurs", "man united", "manchester united", "newcastle"],
    "soccer_spain_la_liga": ["la liga", "real madrid", "barcelona", "atletico madrid"],
    "soccer_germany_bundesliga": ["bundesliga", "bayern munich", "borussia dortmund"],
    "soccer_italy_serie_a": ["serie a", "inter milan", "juventus", "ac milan", "napoli"],
    "soccer_uefa_champs_league": ["champions league", "ucl"],
    "mma_mixed_martial_arts": ["ufc", "mma"],
    "boxing_boxing": ["boxing", "heavyweight", "fight"],
    "motorsport_f1": ["formula 1", "f1", "grand prix"],
}


def _classify_sport(title: str) -> str | None:
    """Classify a Polymarket event title into a sport key."""
    t = title.lower()
    for sport_key, keywords in _HIST_SPORT_KEYWORDS.items():
        if any(kw in t for kw in keywords):
            return sport_key
    return None


def backfill_historical_markets():
    """Fetch resolved Polymarket sports events from the past year and store them."""
    session = _make_http_session()
    # Fetch existing slugs to deduplicate
    with _get_db() as conn:
        rows = conn.execute("SELECT slug FROM sports_historical_markets WHERE source = 'polymarket'").fetchall()
        existing_slugs = set(r["slug"] for r in rows if r["slug"])

    # Fetch closed/resolved sports events from Gamma API
    offset = 0
    inserted = 0
    for _ in range(20):  # max 20 pages
        try:
            resp = session.get(
                f"{GAMMA_API_HOST}/events",
                params={
                    "active": "false",
                    "closed": "true",
                    "limit": 100,
                    "offset": offset,
                    "tag": "sports",
                },
                timeout=30,
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            print(f"Backfill fetch error at offset {offset}: {e}", flush=True)
            break

        if not events:
            break

        batch = []
        for ev in events:
            slug = ev.get("slug", "")
            title = ev.get("title", "")
            end_date = ev.get("endDate") or ev.get("end_date") or ev.get("closedTime", "")
            start_date = ev.get("startDate") or ev.get("start_date") or ev.get("createdAt", "")
            sport = _classify_sport(title)

            for mkt in ev.get("markets", []):
                mkt_slug = mkt.get("slug") or slug
                if mkt_slug in existing_slugs:
                    continue
                question = mkt.get("question", title)
                outcome = mkt.get("outcome", "")
                if not outcome:
                    outcome = mkt.get("groupItemTitle", question)
                final_price = None
                op = mkt.get("outcomePrices", "0")
                # Polymarket returns outcomePrices in three observed shapes:
                #   - a list of strings/floats: ["0.97", "0.03"]
                #   - a JSON-encoded list string: '["0.97", "0.03"]'
                #   - a bare numeric string: "0.97"
                # The previous parser used str(op).strip("[]").split(",")[0]
                # which crashed on quoted JSON ('["0.97"' has a stray quote)
                # and silently returned the wrong value when the list had
                # whitespace around commas. Parse JSON properly with a fallback.
                try:
                    if isinstance(op, list):
                        if op:
                            final_price = float(op[0])
                    elif isinstance(op, (int, float)):
                        final_price = float(op)
                    elif isinstance(op, str):
                        op_stripped = op.strip()
                        if op_stripped.startswith("["):
                            try:
                                parsed = json.loads(op_stripped)
                                if isinstance(parsed, list) and parsed:
                                    final_price = float(parsed[0])
                            except (json.JSONDecodeError, ValueError, TypeError):
                                # Last-ditch fallback: strip brackets/quotes and take first token
                                tok = op_stripped.strip("[]").split(",")[0].strip().strip('"').strip("'")
                                if tok:
                                    final_price = float(tok)
                        elif op_stripped:
                            final_price = float(op_stripped)
                except (ValueError, IndexError, TypeError):
                    final_price = None
                volume = 0
                try:
                    volume = float(mkt.get("volume", 0) or 0)
                except (ValueError, TypeError):
                    pass
                resolution = mkt.get("resolution", "")
                batch.append({
                    "sport": sport,
                    "event_title": title,
                    "market_question": question,
                    "outcome": outcome,
                    "final_price": final_price,
                    "volume": volume,
                    "start_date": start_date,
                    "end_date": end_date,
                    "resolution": resolution,
                    "source": "polymarket",
                    "slug": mkt_slug,
                })
                existing_slugs.add(mkt_slug)

        if batch:
            try:
                # upsert to handle the UNIQUE(source, slug, outcome) constraint
                with _get_db() as conn:
                    for row in batch:
                        conn.execute(
                            """INSERT INTO sports_historical_markets
                               (sport, event_title, market_question, outcome, final_price, volume, start_date, end_date, resolution, source, slug)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(source, slug, outcome) DO UPDATE SET
                                   final_price = excluded.final_price,
                                   volume = excluded.volume,
                                   resolution = excluded.resolution""",
                            (row["sport"], row["event_title"], row["market_question"],
                             row["outcome"], row["final_price"], row["volume"],
                             row["start_date"], row["end_date"], row["resolution"],
                             row["source"], row["slug"]),
                        )
                inserted += len(batch)
            except Exception as e:
                print(f"Backfill insert error: {e}", flush=True)

        offset += 100

    print(f"Backfill complete: {inserted} historical markets inserted", flush=True)
    return inserted


@app.get("/api/history")
async def api_history(request: Request):
    """Return historical market data."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    sport = request.query_params.get("sport", "")
    try:
        limit = min(int(request.query_params.get("limit", "100")), 500)
    except (ValueError, TypeError):
        limit = 100

    # Recent snapshots (last 7 days)
    cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with _get_db() as conn:
        if sport:
            snap_rows = conn.execute(
                "SELECT * FROM sports_market_snapshots WHERE snapshot_at >= ? AND sport = ? ORDER BY snapshot_at DESC LIMIT ?",
                (cutoff_7d, sport, limit),
            ).fetchall()
        else:
            snap_rows = conn.execute(
                "SELECT * FROM sports_market_snapshots WHERE snapshot_at >= ? ORDER BY snapshot_at DESC LIMIT ?",
                (cutoff_7d, limit),
            ).fetchall()
        snapshots = [dict(r) for r in snap_rows]

        # Resolved historical markets
        if sport:
            hist_rows = conn.execute(
                "SELECT * FROM sports_historical_markets WHERE sport = ? ORDER BY end_date DESC LIMIT ?",
                (sport, limit),
            ).fetchall()
        else:
            hist_rows = conn.execute(
                "SELECT * FROM sports_historical_markets ORDER BY end_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        historical = [dict(r) for r in hist_rows]

    return JSONResponse({
        "snapshots": snapshots,
        "historical": historical,
    })


@app.get("/api/market-history/{event_name:path}")
async def api_market_history(event_name: str, request: Request):
    """Return time-series snapshot data for a specific event."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    with _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sports_market_snapshots WHERE event_name = ? ORDER BY snapshot_at LIMIT 1000",
            (event_name,),
        ).fetchall()
    return JSONResponse({"event": event_name, "series": [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# Edge history endpoints
# ---------------------------------------------------------------------------

@app.get("/api/edge-history")
async def edge_history(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sports_edge_history ORDER BY detected_at DESC LIMIT 100"
        ).fetchall()
    return JSONResponse({"edges": [dict(r) for r in rows]})


@app.get("/api/edge-stats")
async def edge_stats(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    with _get_db() as conn:
        total = (conn.execute("SELECT COUNT(*) FROM sports_edge_history").fetchone() or (0,))[0]
        correct = (conn.execute("SELECT COUNT(*) FROM sports_edge_history WHERE resolution = 'correct'").fetchone() or (0,))[0]
        incorrect = (conn.execute("SELECT COUNT(*) FROM sports_edge_history WHERE resolution = 'incorrect'").fetchone() or (0,))[0]
        pending = (conn.execute("SELECT COUNT(*) FROM sports_edge_history WHERE resolved = 0").fetchone() or (0,))[0]
    resolved = correct + incorrect
    win_rate = round(correct / resolved * 100, 1) if resolved > 0 else 0.0
    return JSONResponse({
        "total_edges": total,
        "correct": correct,
        "incorrect": incorrect,
        "pending": pending,
        "win_rate": win_rate,
    })


# ---------------------------------------------------------------------------
# Diagnostics — match-engine near-rejects + Odds API quota
# ---------------------------------------------------------------------------

@app.get("/api/diagnostics/match-rejects")
async def api_match_rejects(request: Request):
    """Recent fuzzy-match near-rejects (admin only). Used to tune team aliases."""
    user = get_current_user(request)
    if not user or not user.get("is_admin"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    return JSONResponse({
        "total": len(_NEAR_REJECTS),
        "max": _NEAR_REJECTS_MAX,
        "items": list(reversed(_NEAR_REJECTS)),  # newest first
    })


@app.get("/api/diagnostics/odds-quota")
async def api_odds_quota(request: Request):
    """Latest Odds API quota counters (admin only)."""
    user = get_current_user(request)
    if not user or not user.get("is_admin"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    return JSONResponse(dict(_ODDS_QUOTA))


@app.get("/api/diagnostics/config-check")
async def api_config_check(request: Request):
    """Structured config-check report. Surfaces every important env var
    and configuration knob with status + remediation. Admin-only —
    the report names env vars that an attacker would want to
    fingerprint."""
    user = get_current_user(request)
    if not user or not user.get("is_admin"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    items = _config_check()
    n_fail = sum(1 for i in items if i["status"] == "fail")
    n_warn = sum(1 for i in items if i["status"] == "warn")
    return JSONResponse({
        "n_total": len(items),
        "n_fail": n_fail,
        "n_warn": n_warn,
        "items": items,
    })


@app.get("/metrics")
async def metrics():
    """Prometheus scrape endpoint. No auth — bind behind a private network."""
    if not _PROM_ENABLED:
        return Response("# prometheus_client not installed\n", media_type="text/plain")
    return Response(_prom_generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Track-record endpoints (CLV, P&L simulation, calibration)
# ---------------------------------------------------------------------------

@app.get("/track-record", response_class=HTMLResponse)
async def track_record_page(request: Request):
    """Public proof-of-edge page. No auth — this is the conversion surface.

    Anonymous viewers see the aggregated CLV / P&L / calibration that
    we'd otherwise hide behind login. The signal *list* is still private.
    """
    return HTMLResponse(_load_template("track_record"))


@app.get("/player-props", response_class=HTMLResponse)
async def player_props_page(request: Request):
    """Kalshi player-prop browser."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return HTMLResponse(_load_template("player_props"))


@app.get("/api/track-record/clv")
async def api_track_record_clv(request: Request):
    """Closing line value across resolved signals.

    Query params: sport (optional), days (default 30, capped to 365).
    Anonymous endpoint — this is the conversion proof. We expose
    aggregates only (no per-signal detail).
    """
    sport = request.query_params.get("sport")
    try:
        days = max(1, min(365, int(request.query_params.get("days", "30"))))
    except ValueError:
        days = 30
    return JSONResponse(await asyncio.to_thread(_compute_clv, sport, days))


@app.get("/api/track-record/pnl")
async def api_track_record_pnl(request: Request):
    """Replay signals at a given threshold and stake. Returns total PnL,
    win rate, ROI, Sharpe, max drawdown, and the per-bet equity curve.
    Anonymous — see /api/track-record/clv docstring."""
    sport = request.query_params.get("sport")
    try:
        days = max(1, min(365, int(request.query_params.get("days", "90"))))
    except ValueError:
        days = 90
    try:
        threshold = max(0.0, min(50.0, float(request.query_params.get("threshold", "5"))))
    except ValueError:
        threshold = 5.0
    try:
        stake = max(1.0, min(10000.0, float(request.query_params.get("stake", "100"))))
    except ValueError:
        stake = 100.0
    return JSONResponse(
        await asyncio.to_thread(_compute_pnl_simulation, sport, days, threshold, stake)
    )


@app.get("/api/track-record/calibration")
async def api_track_record_calibration(request: Request):
    """Calibration plot data: win rate per divergence bucket vs predicted.
    Anonymous — see /api/track-record/clv docstring."""
    sport = request.query_params.get("sport")
    try:
        days = max(1, min(365, int(request.query_params.get("days", "180"))))
    except ValueError:
        days = 180
    return JSONResponse(await asyncio.to_thread(_compute_calibration, sport, days))


# ---------------------------------------------------------------------------
# Public CLV leaderboard (T4.7) — opt-in social proof / virality lever
# ---------------------------------------------------------------------------
#
# Users who opt in see their (display_name, n_resolved_trades, mean_clv,
# total_pnl) on a public ranking page. Distinct from /track-record:
#   - /track-record is the AGGREGATE proof across all signals (dashboard-
#     level).
#   - /leaderboard is the per-user roster — sharp bettors aspire to be
#     on it, which surfaces them to other users and creates social proof.
# Both pages are anonymous-readable. Opting in requires auth.

LEADERBOARD_MIN_TRADES = int(os.getenv("LEADERBOARD_MIN_TRADES", "10"))


def _compute_clv_leaderboard(days: int = 90, limit: int = 50) -> list[dict]:
    """Aggregate per-user CLV from sports_trades for users who opted in.

    Only counts closed trades with a non-null clv_pp. A user must have
    at least LEADERBOARD_MIN_TRADES qualifying trades to appear —
    keeps the list out of "1 lucky bet" territory.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _get_db() as conn:
        rows = conn.execute(
            """SELECT o.user_id, o.display_name,
                      COUNT(t.id) AS n_trades,
                      AVG(t.clv_pp) AS mean_clv,
                      SUM(t.pnl) AS total_pnl,
                      SUM(CASE WHEN t.pnl > 0 THEN 1 ELSE 0 END) AS wins
               FROM sports_clv_leaderboard_optin o
               JOIN sports_trades t ON t.user_id = o.user_id
               WHERE t.status = 'closed'
                 AND t.clv_pp IS NOT NULL
                 AND t.resolved_at >= ?
               GROUP BY o.user_id, o.display_name
               HAVING COUNT(t.id) >= ?
               ORDER BY mean_clv DESC
               LIMIT ?""",
            (cutoff, LEADERBOARD_MIN_TRADES, limit),
        ).fetchall()

    out: list[dict] = []
    for rank, r in enumerate(rows, start=1):
        n = r["n_trades"] or 0
        wins = r["wins"] or 0
        out.append({
            "rank": rank,
            "display_name": r["display_name"],
            "n_trades": n,
            "mean_clv_pp": round(float(r["mean_clv"] or 0), 3),
            "total_pnl": round(float(r["total_pnl"] or 0), 2),
            "win_rate": round(wins / n, 4) if n > 0 else 0.0,
        })
    return out


@app.get("/api/leaderboard/clv")
async def api_leaderboard_clv(request: Request):
    """Public CLV leaderboard — anonymous-readable. Same docstring
    rationale as /api/track-record/clv: this is the conversion surface."""
    try:
        days = max(1, min(365, int(request.query_params.get("days", "90"))))
    except ValueError:
        days = 90
    try:
        limit = max(1, min(200, int(request.query_params.get("limit", "50"))))
    except ValueError:
        limit = 50
    rows = await asyncio.to_thread(_compute_clv_leaderboard, days, limit)
    return JSONResponse({
        "days": days, "min_trades": LEADERBOARD_MIN_TRADES,
        "n_users": len(rows), "rows": rows,
    })


@app.get("/api/leaderboard/optin")
async def api_get_optin(request: Request):
    """Return the user's current opt-in status (display_name or null)."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    with _get_db() as conn:
        row = conn.execute(
            "SELECT display_name, joined_at FROM sports_clv_leaderboard_optin "
            "WHERE user_id = ?",
            (user["id"],),
        ).fetchone()
    return JSONResponse({"optin": dict(row) if row else None})


_LEADERBOARD_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-. ]{1,29}$")


@app.put("/api/leaderboard/optin")
async def api_put_optin(request: Request):
    """Join the public leaderboard with a display name.

    Body: {"display_name": str}. Names must be 2-30 chars, start with
    alphanumeric/underscore, and contain only [A-Za-z0-9_-. ]. Display
    name must be unique across all opted-in users (case-insensitive)
    to prevent impersonation.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = (body.get("display_name") or "").strip()
    if not _LEADERBOARD_NAME_RE.match(name):
        return JSONResponse(
            {"error": "display_name must be 2-30 chars, alphanumeric + _-. and space"},
            status_code=400,
        )
    with _get_db() as conn:
        # Reject names already taken by another user (case-insensitive)
        existing = conn.execute(
            "SELECT user_id FROM sports_clv_leaderboard_optin "
            "WHERE LOWER(display_name) = LOWER(?) AND user_id != ? LIMIT 1",
            (name, user["id"]),
        ).fetchone()
        if existing:
            return JSONResponse(
                {"error": "display_name already taken"}, status_code=409
            )
        conn.execute(
            "INSERT INTO sports_clv_leaderboard_optin (user_id, display_name) "
            "VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET display_name = excluded.display_name",
            (user["id"], name),
        )
    return JSONResponse({"status": "ok", "display_name": name})


@app.delete("/api/leaderboard/optin")
async def api_delete_optin(request: Request):
    """Leave the public leaderboard."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    with _get_db() as conn:
        conn.execute(
            "DELETE FROM sports_clv_leaderboard_optin WHERE user_id = ?",
            (user["id"],),
        )
    return JSONResponse({"status": "ok"})


@app.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard_page(request: Request):
    """Public CLV leaderboard page. Anonymous-readable (matches the
    /track-record conversion strategy)."""
    return HTMLResponse(_load_template("leaderboard"))


@app.post("/api/backtest/replay")
async def api_backtest_replay(request: Request):
    """Replay a hypothetical alert rule against resolved historical signals.

    Body: {"rule": {...same shape as /api/alert-rules...}, "days": int,
    "stake": float}. Returns aggregate stats + equity curve + first 200
    matched signals. Auth required — backtests can be a paid feature
    later but logic is shared.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    raw_rule = body.get("rule") or {}
    if not isinstance(raw_rule, dict):
        return JSONResponse({"error": "rule must be an object"}, status_code=400)

    # Validate + coerce through the same path the CRUD endpoints use so the
    # backtest behaves identically to a live rule.
    fields, err = _validate_rule_body(raw_rule)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    # _signal_matches_rule reads the JSON-encoded fields directly, so build
    # a rule dict the same shape rows have when read from sqlite (sports
    # and market_types are JSON strings, flags are 0/1 ints).
    rule = {
        "sports": fields.get("sports", "[]"),
        "market_types": fields.get("market_types", "[]"),
        "min_divergence_pp": fields.get("min_divergence_pp", 5.0),
        "min_volume": fields.get("min_volume"),
        "max_time_to_event_hours": fields.get("max_time_to_event_hours"),
        "require_sharp_consensus": fields.get("require_sharp_consensus", 1),
        "require_not_stale": fields.get("require_not_stale", 1),
        "require_liquidity_ok": fields.get("require_liquidity_ok", 1),
    }
    try:
        days = max(1, min(365, int(body.get("days", 90))))
    except (TypeError, ValueError):
        days = 90
    try:
        stake = max(1.0, min(10000.0, float(body.get("stake", 100))))
    except (TypeError, ValueError):
        stake = 100.0

    result = await asyncio.to_thread(_simulate_alert_rule, rule, days, stake)
    return JSONResponse(result)


@app.get("/backtest", response_class=HTMLResponse)
async def backtest_page(request: Request):
    """Backtest replay UI — tune a rule, see what would have triggered."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return HTMLResponse(_load_template("backtest"))


@app.get("/api/steam-moves")
async def api_steam_moves(request: Request):
    """Recent sharp-book line moves above the steam threshold.

    Query params:
      sport: optional sport key filter
      hours: lookback in hours (default 24, max 168)
      min_delta_pp: minimum |swing| in pp (default 2)
      window_min: max minutes between snapshots to qualify (default 30)
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sport = request.query_params.get("sport")
    try:
        hours = max(1, min(168, int(request.query_params.get("hours", "24"))))
    except ValueError:
        hours = 24
    try:
        min_d = max(0.5, min(50.0, float(request.query_params.get("min_delta_pp", STEAM_MIN_DELTA_PP))))
    except ValueError:
        min_d = STEAM_MIN_DELTA_PP
    try:
        window = max(1, min(240, int(request.query_params.get("window_min", STEAM_WINDOW_MINUTES))))
    except ValueError:
        window = STEAM_WINDOW_MINUTES

    rows = await asyncio.to_thread(_detect_steam_moves, sport, hours, min_d, window)
    return JSONResponse({
        "sport": sport, "hours": hours,
        "min_delta_pp": min_d, "window_min": window,
        "n_moves": len(rows), "moves": rows,
    })


@app.get("/api/closing-lines")
async def api_closing_lines(request: Request):
    """Sharp-book closing line per (event, outcome) over the recent window."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sport = request.query_params.get("sport")
    try:
        days = max(1, min(60, int(request.query_params.get("days", "7"))))
    except ValueError:
        days = 7
    rows = await asyncio.to_thread(_compute_closing_lines, sport, days)
    return JSONResponse({"sport": sport, "days": days,
                          "n_events": len(rows), "rows": rows})


@app.get("/steam-moves", response_class=HTMLResponse)
async def steam_moves_page(request: Request):
    """Sharp-book line-move feed."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return HTMLResponse(_load_template("steam_moves"))


@app.get("/api/poly-fills")
async def api_poly_fills(request: Request):
    """Recent large Polymarket fills captured from the live WS feed.

    Joins each fill to its asset's market context (event, outcome) using
    the current comparison set. Capped at PM_FILL_BUFFER_MAX (~500)
    rows in memory — older fills age out as new ones arrive.

    Query params:
      min_usd: USD floor (default = PM_FILL_MIN_USD env var or 1000)
      side: BUY | SELL (optional filter)
      limit: max rows to return (default 100, max 500)
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        min_usd = max(0.0, float(request.query_params.get("min_usd", PM_FILL_MIN_USD)))
    except ValueError:
        min_usd = PM_FILL_MIN_USD
    side_filter = (request.query_params.get("side") or "").upper() or None
    try:
        limit = max(1, min(PM_FILL_BUFFER_MAX, int(request.query_params.get("limit", "100"))))
    except ValueError:
        limit = 100

    # Build a token_id → (event, outcome) lookup from current comparisons
    async with _data_lock:
        comparisons = dashboard_data.get("comparisons") or []
    token_lookup: dict[str, dict] = {}
    for c in comparisons:
        event_name = _event_name(c.get("home_team"), c.get("away_team"))
        for oc in c.get("outcomes") or []:
            tid = oc.get("poly_token_id")
            if tid:
                token_lookup[tid] = {
                    "event": event_name,
                    "outcome": oc.get("outcome") or oc.get("outcome_name", ""),
                    "condition_id": c.get("condition_id"),
                    "trade_poly_url": c.get("trade_poly_url"),
                }

    # Snapshot the ring buffer
    with _LIVE_POLY_FILLS_LOCK:
        all_fills = list(_LIVE_POLY_FILLS)

    out: list[dict] = []
    for f in reversed(all_fills):  # newest first
        if f["usd"] < min_usd:
            continue
        if side_filter and f["side"] != side_filter:
            continue
        ctx = token_lookup.get(f["asset_id"])
        row = dict(f)
        if ctx:
            row.update(ctx)
        out.append(row)
        if len(out) >= limit:
            break

    return JSONResponse({
        "n_buffer": len(all_fills),
        "min_usd": min_usd,
        "limit": limit,
        "fills": out,
    })


@app.get("/poly-fills", response_class=HTMLResponse)
async def poly_fills_page(request: Request):
    """Live Polymarket fills tape."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return HTMLResponse(_load_template("poly_fills"))


# ---------------------------------------------------------------------------
# Kalshi player-prop surface (NBA pts/3pt/ast/reb today; extensible)
# ---------------------------------------------------------------------------

def _format_player_props(parsed_kalshi: list[dict]) -> list[dict]:
    """Reshape parse_kalshi_markets output to a player-prop friendly schema."""
    props: list[dict] = []
    for ev in parsed_kalshi:
        if ev.get("market_type") != "props":
            continue
        for player_name, data in (ev.get("teams") or {}).items():
            props.append({
                "event": ev.get("title", ""),
                "ticker": data.get("ticker", ""),
                "player": player_name,
                "yes_bid": data.get("yes_bid"),
                "yes_ask": data.get("yes_ask"),
                "mid_price": data.get("mid_price"),
                "implied_prob": data.get("implied_prob"),
                "volume": data.get("volume", 0),
            })
    # Sort by volume desc — most-traded props are the actionable ones.
    props.sort(key=lambda p: -(p.get("volume") or 0))
    return props


@app.get("/api/kalshi/player-props")
async def api_kalshi_player_props(request: Request):
    """Kalshi-only player-prop feed. Kept for backwards compatibility;
    new clients should use /api/player-props/cross-venue."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sport = request.query_params.get("sport", "basketball_nba")
    if sport not in KALSHI_SERIES:
        return JSONResponse({"error": "unknown sport", "sport": sport}, status_code=400)
    raw = await asyncio.to_thread(fetch_kalshi_markets, sport)
    parsed = parse_kalshi_markets(raw)
    return JSONResponse({
        "sport": sport,
        "props": _format_player_props(parsed),
    })


def _build_cross_venue_player_props(sport: str) -> dict:
    """Fetch book player-props for imminent games, join against Kalshi
    and Polymarket prop markets, return the cross-venue table.

    Cost-controlled: only fetches book props for events starting in the
    next PROP_EVENT_LOOKAHEAD_HOURS, and only N events per request to
    cap quota burn. Cached per event at PROP_CACHE_TTL_SECONDS.
    """
    if sport not in PROP_MARKETS_BY_SPORT:
        return {"sport": sport, "props": [], "error": "sport not configured for props"}

    # Always-available: Kalshi side (no per-event call)
    kalshi_raw = fetch_kalshi_markets(sport)
    kalshi_parsed = parse_kalshi_markets(kalshi_raw)
    kalshi_props = parse_kalshi_player_props(kalshi_parsed)

    # Polymarket: use cached poly markets we already fetched in the main loop
    global _poly_cache
    poly_markets = parse_polymarket_events(_poly_cache.get("_global", []))

    # Book side: imminent events only
    events = fetch_imminent_events(sport)
    # Cap to 12 events per call to bound quota burn (12 NBA games × 1 call = 12)
    events = events[:12]

    all_book_props: list[dict] = []
    for ev in events:
        ev_id = ev.get("id")
        if not ev_id:
            continue
        raw, _remaining = fetch_player_props_for_event(sport, ev_id)
        if not raw:
            continue
        all_book_props.extend(parse_player_props(raw))

    rows = match_player_props_cross_venue(all_book_props, kalshi_props, poly_markets)
    return {
        "sport": sport,
        "n_events_fetched": len(events),
        "n_book_props": len(all_book_props),
        "n_kalshi_props": len(kalshi_props),
        "props": rows,
        # Surface a Kalshi-only fallback so the page still works if the
        # user has no Odds API key (props will be Kalshi-side only).
        "kalshi_only_props": _format_player_props(kalshi_parsed),
    }


def _build_cross_book_arbitrage(sport: str) -> dict:
    """Pull current h2h + spreads + totals odds for the sport, run both
    arb scanners, return everything in one payload."""
    if not ODDS_API_KEY:
        return {"sport": sport, "low_holds": [], "middles": [],
                "error": "ODDS_API_KEY not configured"}

    h2h_raw, _ = fetch_odds(sport, markets="h2h")
    spreads_raw, _ = fetch_odds(sport, markets="spreads")
    totals_raw, _ = fetch_odds(sport, markets="totals")

    h2h_events = parse_odds_events(h2h_raw, "h2h")
    spreads_events = parse_odds_events(spreads_raw, "spreads")
    totals_events = parse_odds_events(totals_raw, "totals")
    all_events = h2h_events + spreads_events + totals_events

    return {
        "sport": sport,
        "low_holds": find_low_hold_opportunities(all_events),
        "middles": find_middle_opportunities(all_events),
        "n_events": len(h2h_events),
    }


@app.get("/api/cross-book-arbitrage")
async def api_cross_book_arbitrage(request: Request):
    """Pure book-vs-book +EV: low-hold (negative vig) + middling opportunities.

    No Polymarket / Kalshi involved — this is the table-stakes feature
    that OddsJam-class tools have and ours didn't.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sport = request.query_params.get("sport", "basketball_nba")
    return JSONResponse(await asyncio.to_thread(_build_cross_book_arbitrage, sport))


# ── PWA: serve manifest and service worker from root paths so the worker
# can control the whole origin (browsers scope sw.js to its own directory).

@app.get("/manifest.json")
async def pwa_manifest():
    return FileResponse(str(_STATIC_DIR / "manifest.json"), media_type="application/manifest+json")


@app.get("/sw.js")
async def pwa_service_worker():
    return FileResponse(str(_STATIC_DIR / "sw.js"), media_type="application/javascript",
                         headers={"Service-Worker-Allowed": "/"})


@app.get("/favicon.png")
async def favicon():
    return FileResponse(str(_STATIC_DIR / "favicon.png"), media_type="image/png")


@app.get("/trades", response_class=HTMLResponse)
async def trades_page(request: Request):
    """Bet tracker: log placed bets, track P&L + CLV over time."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return HTMLResponse(_load_template("trades"))


@app.get("/cross-book-arbitrage", response_class=HTMLResponse)
async def cross_book_arbitrage_page(request: Request):
    """Cross-book arb table (low-hold + middles)."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return HTMLResponse(_load_template("cross_book_arbitrage"))


@app.get("/api/player-props/cross-venue")
async def api_player_props_cross_venue(request: Request):
    """Cross-venue player-prop comparison: book consensus vs Kalshi vs Polymarket.

    Joins on (player, market, line). Books use continuous half-integer
    lines; Kalshi uses discrete tiers (T(N) maps to book over N-0.5);
    Polymarket prop questions are extracted by regex.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sport = request.query_params.get("sport", "basketball_nba")
    return JSONResponse(await asyncio.to_thread(_build_cross_venue_player_props, sport))


# ---------------------------------------------------------------------------
# Cross-sport edges endpoint
# ---------------------------------------------------------------------------

@app.get("/api/cross-sport-edges")
async def api_cross_sport_edges(request: Request):
    """Return top edges across all scanned sports."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return JSONResponse({"edges": _get_all_cross_sport_edges()})


# ---------------------------------------------------------------------------
# Alerts configuration endpoints
# ---------------------------------------------------------------------------

@app.get("/api/alerts")
async def get_alert_config(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    with _get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sports_alert_config WHERE user_id = ? LIMIT 1", (user["id"],)
        ).fetchone()
    if row:
        cfg = dict(row)
        # Defensive: a corrupt JSON value should not 500 the settings page.
        try:
            cfg["sports"] = json.loads(cfg.get("sports", "[]"))
            if not isinstance(cfg["sports"], list):
                cfg["sports"] = []
        except (json.JSONDecodeError, TypeError):
            cfg["sports"] = []
        # Don't expose bot token to client
        cfg["telegram_bot_token"] = "****" if cfg.get("telegram_bot_token") else ""
        return JSONResponse(cfg)
    return JSONResponse({
        "enabled": 0, "telegram_chat_id": "", "telegram_bot_token": "",
        "webhook_url": "", "min_edge": 5.0, "sports": [],
    })


@app.post("/api/alerts")
async def save_alert_config(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Body must be an object"}, status_code=400)

    raw_enabled = body.get("enabled", 0)
    if isinstance(raw_enabled, bool):
        enabled = 1 if raw_enabled else 0
    else:
        try:
            enabled = 1 if int(raw_enabled) else 0
        except (TypeError, ValueError):
            enabled = 0

    tg_chat = str(body.get("telegram_chat_id", "") or "").strip()[:200]
    tg_token = str(body.get("telegram_bot_token", "") or "").strip()[:500]
    webhook_url = str(body.get("webhook_url", "") or "").strip()[:2048]

    try:
        min_edge = float(body.get("min_edge", 5.0))
    except (TypeError, ValueError):
        min_edge = 5.0
    if math.isnan(min_edge) or math.isinf(min_edge):
        min_edge = 5.0
    # Clamp to a sensible range so users can't poison the alert filter with
    # extreme values that disable alerting entirely.
    min_edge = max(0.0, min(min_edge, 100.0))

    alert_sports = body.get("sports", [])
    if not isinstance(alert_sports, list):
        alert_sports = []
    # Drop non-string entries so the JSON column can't store junk
    alert_sports = [str(s)[:80] for s in alert_sports if isinstance(s, (str, int))][:50]

    # Validate webhook URL against SSRF
    if webhook_url and not _is_safe_webhook_url(webhook_url):
        return JSONResponse({"error": "Webhook URL must be HTTPS and not target private networks"}, status_code=400)

    with _get_db() as conn:
        # If token is masked, keep existing (already encrypted in DB)
        if tg_token == "****":
            existing = conn.execute(
                "SELECT telegram_bot_token FROM sports_alert_config WHERE user_id = ?", (user["id"],)
            ).fetchone()
            if existing:
                tg_token = existing[0] or ""
        else:
            # Encrypt new token before storing
            tg_token = _encrypt_field(tg_token)
        conn.execute(
            """INSERT INTO sports_alert_config (user_id, enabled, telegram_chat_id, telegram_bot_token, webhook_url, min_edge, sports)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   enabled = excluded.enabled, telegram_chat_id = excluded.telegram_chat_id,
                   telegram_bot_token = excluded.telegram_bot_token, webhook_url = excluded.webhook_url,
                   min_edge = excluded.min_edge, sports = excluded.sports""",
            (user["id"], enabled, tg_chat, tg_token, webhook_url, min_edge, json.dumps(alert_sports)),
        )
    log_activity(user["id"], "update_alerts", f"enabled={enabled}")
    return JSONResponse({"status": "ok"})


@app.post("/api/alerts/test")
async def test_alert(request: Request):
    """Send a test alert to verify config."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    with _get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sports_alert_config WHERE user_id = ? LIMIT 1", (user["id"],)
        ).fetchone()
    if not row:
        return JSONResponse({"error": "No alert config found"}, status_code=404)
    cfg = dict(row)
    msg = "Sharpe Test Alert: Your alerts are configured correctly!"
    sent = False
    tg_token = _decrypt_field(cfg.get("telegram_bot_token", ""))
    tg_chat = cfg.get("telegram_chat_id", "")
    if tg_token and tg_chat:
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", tg_token):
            return JSONResponse({"error": "Invalid Telegram bot token format"}, status_code=400)
        try:
            requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={"chat_id": tg_chat, "text": msg},
                timeout=10,
            )
            sent = True
        except Exception:
            return JSONResponse({"error": "Telegram delivery failed"}, status_code=500)
    webhook_url = cfg.get("webhook_url", "")
    if webhook_url:
        if not _is_safe_webhook_url(webhook_url):
            return JSONResponse({"error": "Webhook URL must be HTTPS and not target private networks"}, status_code=400)
        try:
            requests.post(webhook_url, json={"text": msg}, timeout=10)
            sent = True
        except Exception:
            return JSONResponse({"error": "Webhook delivery failed"}, status_code=500)
    if not sent:
        return JSONResponse({"error": "No Telegram or webhook configured"}, status_code=400)
    return JSONResponse({"status": "ok", "message": "Test alert sent"})


# ---------------------------------------------------------------------------
# Match flagging endpoint
# ---------------------------------------------------------------------------

_FLAG_MATCH_RATE: dict[int, list[float]] = {}
_FLAG_MATCH_RATE_LIMIT = 10  # max flags per user per 5 minutes
_FLAG_MATCH_RATE_WINDOW = 300


@app.post("/api/flag-match")
async def flag_match(request: Request):
    """Let users report a bad fuzzy match.

    Lengths and rate are bounded so a misbehaving (or malicious) client
    can't flood the table with megabyte payloads.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    # Per-user rate limit (in-memory; resets on restart)
    now_ts = time.time()
    uid = user["id"]
    history = [t for t in _FLAG_MATCH_RATE.get(uid, []) if now_ts - t < _FLAG_MATCH_RATE_WINDOW]
    if len(history) >= _FLAG_MATCH_RATE_LIMIT:
        return JSONResponse(
            {"error": "Too many flags. Try again later."}, status_code=429,
        )
    history.append(now_ts)
    _FLAG_MATCH_RATE[uid] = history
    # Cap dict size so an attacker with many accounts can't OOM us
    if len(_FLAG_MATCH_RATE) > 10_000:
        # Drop the oldest half (cheap heuristic — entries are unordered, so
        # just keep half the keys arbitrarily).
        for k in list(_FLAG_MATCH_RATE.keys())[:5_000]:
            _FLAG_MATCH_RATE.pop(k, None)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Body must be an object"}, status_code=400)

    home = str(body.get("home_team", "") or "").strip()[:200]
    away = str(body.get("away_team", "") or "").strip()[:200]
    poly_q = str(body.get("poly_question", "") or "").strip()[:500]
    reason = str(body.get("reason", "") or "").strip()[:500]
    if not home:
        return JSONResponse({"error": "home_team required"}, status_code=400)
    with _get_db() as conn:
        conn.execute(
            "INSERT INTO sports_match_flags (user_id, home_team, away_team, poly_question, reason) VALUES (?, ?, ?, ?, ?)",
            (uid, home, away, poly_q, reason),
        )
    log_activity(uid, "flag_match", f"{home} vs {away}: {reason}")
    return JSONResponse({"status": "ok"})


@app.get("/api/flagged-matches")
async def list_flagged_matches(request: Request):
    """Admin: view flagged matches."""
    user = get_current_user(request)
    if not user or not user.get("is_admin"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sports_match_flags ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    return JSONResponse({"flags": [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# Orderbook depth endpoint
# ---------------------------------------------------------------------------

@app.get("/api/orderbook-depth/{token_id}")
async def get_orderbook_depth(token_id: str, request: Request):
    """Fetch orderbook depth summary for a Polymarket token."""
    if not get_current_user(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        depth = await asyncio.to_thread(fetch_orderbook_depth, token_id)
        return JSONResponse(depth)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Edge performance & Sharpe ratio endpoint
# ---------------------------------------------------------------------------

@app.get("/api/edge-performance")
async def edge_performance(request: Request):
    """Return rolling edge performance stats and Sharpe ratio over time."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    with _get_db() as conn:
        # Get resolved edges grouped by week
        rows = conn.execute("""
            SELECT
                strftime('%Y-%W', detected_at) as week,
                COUNT(*) as total,
                SUM(CASE WHEN resolution = 'correct' THEN 1 ELSE 0 END) as correct,
                SUM(CASE WHEN resolution = 'incorrect' THEN 1 ELSE 0 END) as incorrect,
                AVG(divergence) as avg_divergence,
                AVG(CASE WHEN resolution = 'correct' THEN divergence ELSE 0 END) as avg_winning_edge
            FROM sports_edge_history
            WHERE resolved = 1
            GROUP BY week
            ORDER BY week
        """).fetchall()

        weekly = []
        returns = []
        for r in rows:
            r = dict(r)
            total = r["total"] or 1
            correct = r["correct"] or 0
            win_rate = correct / total
            # Simplified return: win_rate * avg_edge - (1 - win_rate) * avg_edge
            avg_edge = abs(r["avg_divergence"] or 0) / 100
            expected_return = win_rate * avg_edge - (1 - win_rate) * avg_edge
            returns.append(expected_return)

            weekly.append({
                "week": r["week"],
                "total": total,
                "correct": correct,
                "incorrect": r["incorrect"] or 0,
                "win_rate": round(win_rate * 100, 1),
                "avg_edge": round(abs(r["avg_divergence"] or 0), 2),
                "expected_return": round(expected_return * 100, 2),
            })

        # Compute rolling Sharpe ratio (annualized)
        if len(returns) >= 2:
            mean_ret = statistics.mean(returns)
            std_ret = statistics.stdev(returns)
            sharpe = round((mean_ret / std_ret) * (52 ** 0.5), 2) if std_ret > 0 else 0
        else:
            sharpe = 0

        # Overall stats
        total_resolved = sum(w["total"] for w in weekly)
        total_correct = sum(w["correct"] for w in weekly)
        overall_win_rate = round(total_correct / total_resolved * 100, 1) if total_resolved > 0 else 0

    return JSONResponse({
        "weekly": weekly,
        "sharpe_ratio": sharpe,
        "overall_win_rate": overall_win_rate,
        "total_resolved": total_resolved,
        "total_correct": total_correct,
    })


# ---------------------------------------------------------------------------
# Auto-resolution scores endpoint (admin)
# ---------------------------------------------------------------------------

@app.get("/api/scores")
async def api_scores(request: Request):
    """Return recent completed scores."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sports_scores WHERE completed = 1 ORDER BY fetched_at DESC LIMIT 50"
        ).fetchall()
    return JSONResponse({"scores": [dict(r) for r in rows]})


@app.get("/api/h2h")
async def api_h2h(request: Request):
    """Return historical head-to-head record between two teams.

    Query params: sport (sport_key), team_a, team_b, limit (default 10).
    Also returns recent form for each team.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    sport = request.query_params.get("sport") or ""
    team_a = request.query_params.get("team_a") or ""
    team_b = request.query_params.get("team_b") or ""
    try:
        limit = max(1, min(50, int(request.query_params.get("limit") or 10)))
    except ValueError:
        limit = 10
    if not sport or not team_a or not team_b:
        return JSONResponse({"error": "sport, team_a, team_b required"}, status_code=400)
    h2h = _compute_h2h(sport, team_a, team_b, lookback=limit)
    home_form = _compute_team_form(sport, team_a)
    away_form = _compute_team_form(sport, team_b)
    return JSONResponse({
        "sport": sport,
        "h2h": h2h,
        "home_form": home_form,
        "away_form": away_form,
    })


@app.get("/api/h2h-stats")
async def api_h2h_stats(request: Request):
    """Return summary stats about the team_history table (per-sport row counts)."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    with _get_db() as conn:
        meta_rows = conn.execute(
            "SELECT sport, last_fetch_at, last_date_covered, rows_total FROM sports_history_meta"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM sports_team_history").fetchone()[0]
    return JSONResponse({
        "total_rows": total,
        "per_sport": [dict(r) for r in meta_rows],
        "supported_sports": list(ESPN_LEAGUE_MAP.keys()),
    })


# ---------------------------------------------------------------------------
# Referral endpoint
# ---------------------------------------------------------------------------

@app.get("/api/referral")
async def get_referral(request: Request):
    """Referrals are managed by the gateway. Return stub info."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return JSONResponse({
        "referral_code": None,
        "referred_count": 0,
        "managed_by": "gateway",
    })


# ---------------------------------------------------------------------------
# FX rates (frankfurter.dev) — used by client-side currency picker
# ---------------------------------------------------------------------------

_FX_CACHE: dict = {"rates": None, "fetched_at": 0.0}
_FX_TTL = 3600  # 1 hour
_FX_FALLBACK = {
    "USD": 1.0, "EUR": 0.92, "GBP": 0.79, "JPY": 150.0, "AUD": 1.52,
    "CAD": 1.36, "CHF": 0.88, "CNY": 7.20, "HKD": 7.83, "NZD": 1.65,
    "SEK": 10.5, "KRW": 1340.0, "SGD": 1.34, "NOK": 10.6, "MXN": 17.0,
    "INR": 83.0, "ZAR": 18.5, "TRY": 32.0, "BRL": 5.0, "DKK": 6.85,
    "PLN": 3.95, "THB": 35.0, "IDR": 15700.0, "HUF": 360.0, "CZK": 23.0,
    "ILS": 3.7, "PHP": 56.0, "MYR": 4.7, "RON": 4.6, "ISK": 137.0,
}


def _fetch_fx_blocking() -> dict:
    try:
        r = requests.get(
            "https://api.frankfurter.dev/v1/latest",
            params={"base": "USD"},
            timeout=5,
        )
        if r.ok:
            data = r.json() or {}
            rates = dict(data.get("rates") or {})
            rates["USD"] = 1.0
            return rates
    except Exception:
        pass
    return dict(_FX_FALLBACK)


@app.get("/api/fx-rates")
async def api_fx_rates():
    """Return USD-base FX rates with 1h server cache."""
    now = time.time()
    cached = _FX_CACHE.get("rates")
    fetched = _FX_CACHE.get("fetched_at", 0.0)
    if cached and (now - fetched) < _FX_TTL:
        return JSONResponse({"base": "USD", "rates": cached, "fetched_at": fetched})
    rates = await asyncio.to_thread(_fetch_fx_blocking)
    _FX_CACHE["rates"] = rates
    _FX_CACHE["fetched_at"] = now
    return JSONResponse({"base": "USD", "rates": rates, "fetched_at": now})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Authenticate via gateway SSO headers
    _sso_secret = os.environ.get("GATEWAY_SSO_SECRET")
    authed = False
    if _sso_secret:
        headers = ws.headers
        if hmac.compare_digest(headers.get("x-gateway-secret", ""), _sso_secret):
            if headers.get("x-gateway-user-id") and headers.get("x-gateway-user-email"):
                authed = True
        # Also check query params (WebSocket headers can be tricky for browsers)
        if not authed:
            qs_secret = ws.query_params.get("gateway_secret", "")
            if qs_secret and hmac.compare_digest(qs_secret, _sso_secret):
                authed = True
    else:
        # No SSO secret configured (dev mode) — allow all connections
        authed = True
    if not authed:
        await ws.close(code=1008, reason="Not authenticated")
        return
    await ws.accept()
    connected_ws.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "update", "data": dashboard_data}, default=str))
        while True:
            await ws.receive_text()
    except Exception:
        pass
    finally:
        connected_ws.discard(ws)


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    """Liveness probe. Returns 200 immediately as long as the process
    is up — does not check downstream APIs (Odds API, Polymarket, etc.)
    so a third-party outage doesn't fail the LB health check."""
    return JSONResponse({
        "status": "ok",
        "service": "sports-dashboard",
        "polymarket_ws_failures": _pm_ws_failure_count,
        "odds_quota_remaining": odds_quota_remaining(),
    })


@app.get("/readyz")
async def readyz():
    """Readiness probe. 503 if critical subsystems aren't ready yet
    (data updater hasn't run, DB unreachable). Use this as the gate
    for adding the pod to load balancing."""
    issues: list[str] = []
    # DB reachable?
    try:
        with _get_db() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as e:
        issues.append(f"db: {e}")
    # Data updater has produced at least one comparison set?
    try:
        async with _data_lock:
            has_data = bool(dashboard_data.get("last_update"))
    except Exception as e:
        issues.append(f"data_lock: {e}")
        has_data = False
    if not has_data:
        issues.append("data_updater has not run yet")
    status = "ready" if not issues else "not_ready"
    return JSONResponse({"status": status, "issues": issues},
                          status_code=200 if not issues else 503)


@app.get("/changelog", response_class=HTMLResponse)
async def changelog_page(request: Request):
    """What's new in Sharpe. Public — same conversion rationale as
    /features and /track-record."""
    return HTMLResponse(_load_template("changelog"))


def _compute_signal_history(sport: str | None, days: int, limit: int,
                              resolved_only: bool) -> list[dict]:
    """Pull recent flagged signals from sports_edge_history.

    Per-signal log — distinct from /track-record (aggregate). This is
    the public receipt: "here's exactly what fired and what happened."
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    where = ["detected_at >= ?"]
    params: list = [cutoff]
    if sport:
        where.append("sport = ?")
        params.append(sport)
    if resolved_only:
        where.append("resolved = 1")
        where.append("resolution IN ('correct', 'incorrect')")

    sql = (
        "SELECT sport, home_team, away_team, outcome, "
        "       sharp_prob, poly_prob, divergence, kelly_pct, "
        "       resolved, resolution, detected_at, commence_time, market_type "
        "FROM sports_edge_history "
        "WHERE " + " AND ".join(where) + " "
        "ORDER BY detected_at DESC LIMIT ?"
    )
    params.append(int(limit))
    with _get_db() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return rows


@app.get("/api/signal-history")
async def api_signal_history(request: Request):
    """Public per-signal ledger.

    Query params:
      sport: optional sport key
      days: lookback (default 30, max 365)
      limit: max rows (default 100, max 500)
      resolved_only: '1' to only return signals with a final resolution
    """
    try:
        days = max(1, min(365, int(request.query_params.get("days", "30"))))
    except ValueError:
        days = 30
    try:
        limit = max(1, min(500, int(request.query_params.get("limit", "100"))))
    except ValueError:
        limit = 100
    sport = request.query_params.get("sport")
    resolved_only = request.query_params.get("resolved_only") == "1"
    rows = await asyncio.to_thread(
        _compute_signal_history, sport, days, limit, resolved_only,
    )
    # Tally for the summary card
    n_total = len(rows)
    n_resolved = sum(1 for r in rows if r.get("resolved"))
    n_correct = sum(1 for r in rows if r.get("resolution") == "correct")
    win_rate = round(n_correct / n_resolved, 4) if n_resolved else 0.0
    return JSONResponse({
        "sport": sport,
        "days": days,
        "n_total": n_total,
        "n_resolved": n_resolved,
        "n_correct": n_correct,
        "win_rate": win_rate,
        "rows": rows,
    })


@app.get("/signal-history", response_class=HTMLResponse)
async def signal_history_page(request: Request):
    """Public per-signal ledger page."""
    return HTMLResponse(_load_template("signal_history"))


@app.get("/features", response_class=HTMLResponse)
async def features_page(request: Request):
    """Command-center index page that lists every dashboard surface.

    Anonymous-readable: shows the same map but with login CTAs on the
    auth-gated pages. The main /dashboard is huge and predates most of
    these features, so this is the discoverability surface that ties
    them together.
    """
    return HTMLResponse(_load_template("features"))


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    settings = get_user_settings(user["id"])
    try:
        user_threshold = float(settings.get("divergence_threshold", DIVERGENCE_THRESHOLD))
    except (ValueError, TypeError):
        user_threshold = DIVERGENCE_THRESHOLD
    user_sport = settings.get("default_sport", "basketball_nba")
    import html as _html_mod
    # Server-side state for the bookmaker-quota banner. Avoids needing
    # /api/health to be accessible without auth (which would leak status).
    quota_banner_display = "block" if (time.time() < _ODDS_BREAKER_OPEN_UNTIL) else "none"
    quota_banner_reopen_hrs = max(0, int((_ODDS_BREAKER_OPEN_UNTIL - time.time()) / 3600))
    html = DASHBOARD_HTML.replace("__USER_THRESHOLD__", str(user_threshold))
    html = html.replace("__USER_SPORT__", _html_mod.escape(user_sport).replace("\\", "\\\\").replace("'", "\\'"))
    html = html.replace("__USERNAME__", _html_mod.escape(user["username"]).replace("\\", "\\\\").replace("'", "\\'"))
    html = html.replace("__QUOTA_BANNER_DISPLAY__", quota_banner_display)
    html = html.replace("__QUOTA_BANNER_HOURS__", str(quota_banner_reopen_hrs))
    return HTMLResponse(html)


DASHBOARD_HTML = _load_template("dashboard")

# ---------------------------------------------------------------------------

USERS_HTML = _load_template("users")


# ---------------------------------------------------------------------------
# Settings HTML
# ---------------------------------------------------------------------------

SETTINGS_HTML = _load_template("settings")


# ---------------------------------------------------------------------------
# Admin HTML
# ---------------------------------------------------------------------------

ADMIN_HTML = _load_template("admin")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8888"))
    uvicorn.run(app, host=host, port=port, log_level="info")

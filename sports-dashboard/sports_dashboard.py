#!/usr/bin/env python3
"""
Sports Betting Dashboard — Polymarket vs Bookmaker Odds Comparison

Serves a live HTML dashboard that compares bookmaker odds (via The Odds API)
with Polymarket market prices to help spot mispriced markets.
Signals only — no trading logic.
"""

import asyncio
import hmac
import json
import logging
import os
import tempfile
import statistics
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from rapidfuzz import fuzz
import sqlite3
import threading
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
POLYMARKET_HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
GAMMA_API_HOST = "https://gamma-api.polymarket.com"
KALSHI_API_HOST = "https://api.elections.kalshi.com/trade-api/v2"
DIVERGENCE_THRESHOLD = float(os.getenv("DIVERGENCE_THRESHOLD", "5"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))

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

_migrate_db()


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

    # No gateway SSO -- not authenticated (auth handled by gateway)
    return None


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
    "man utd": "manchester united",
    "man city": "manchester city",
    "man united": "manchester united",
    "manchester utd": "manchester united",
    "spurs": "tottenham hotspur",
    "tottenham": "tottenham hotspur",
    "wolves": "wolverhampton wanderers",
    "wolverhampton": "wolverhampton wanderers",
    "wolverhampton wanderers": "wolverhampton wanderers",
    "newcastle utd": "newcastle united",
    "brighton": "brighton and hove albion",
    "west ham": "west ham united",
    "nott'm forest": "nottingham forest",
    "nottm forest": "nottingham forest",
    "leicester": "leicester city",
    "ipswich": "ipswich town",
    "afc bournemouth": "bournemouth",
    "luton": "luton town",
    "athletic bilbao": "athletic club",
    "atletico madrid": "atletico de madrid",
    "inter milan": "internazionale",
    "ac milan": "milan",
    "bayern": "bayern munich",
    "bayern munchen": "bayern munich",
    "borussia dortmund": "dortmund",
    "rb leipzig": "rasenballsport leipzig",
    "psg": "paris saint-germain",
    "paris sg": "paris saint-germain",
    # NBA
    "sixers": "philadelphia 76ers",
    "blazers": "portland trail blazers",
    "timberwolves": "minnesota timberwolves",
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


def fetch_odds(sport_key: str, markets: str = "h2h,spreads,totals") -> tuple[list[dict], str | None]:
    """Fetch match odds from The Odds API. Returns (events, requests_remaining)."""
    if not ODDS_API_KEY:
        return [], None

    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "eu,uk,us",
        "markets": markets,
        "oddsFormat": "decimal",
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    remaining = resp.headers.get("x-requests-remaining")
    return resp.json(), remaining


def fetch_outright_odds(sport_key: str) -> tuple[list[dict], str | None]:
    """Fetch outright/futures odds for a sport (e.g. league winner)."""
    if not ODDS_API_KEY:
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
        resp.raise_for_status()
        remaining = resp.headers.get("x-requests-remaining")
        return resp.json(), remaining
    except Exception:
        return [], None


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
                    implied = (1.0 / o["price"]) * 100
                    label = o["name"]
                    # For spreads/totals, include point value in label
                    if market_type == "spreads" and o.get("point") is not None:
                        label = f"{o['name']} {o['point']:+g}"
                    elif market_type == "totals" and o.get("point") is not None:
                        label = f"{o['name']} {o['point']}"
                    outcomes[label] = {
                        "decimal_odds": o["price"],
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
        sharp_key = pin_key if pin_key in bookmakers_data else list(bookmakers_data.keys())[0]
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
            })
    return parsed


# ---------------------------------------------------------------------------
# Kalshi API
# ---------------------------------------------------------------------------

_kalshi_cache: list[dict] = []
_kalshi_cache_time: float = 0


def fetch_kalshi_markets(sport_key: str) -> list[dict]:
    """Fetch markets from Kalshi for the given sport across all its series."""
    series_list = KALSHI_SERIES.get(sport_key, [])
    if not series_list:
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

    return all_markets


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

            q = pm["market_question"].lower()
            title = pm["event_title"].lower()
            text = f"{q} {title}"

            # BOTH team names must score well (not just one)
            home_score = fuzz.partial_ratio(home, text)
            away_score = fuzz.partial_ratio(away, text)

            # Require both teams to be present (min 70 each)
            if home_score < 70 or away_score < 70:
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
            norm_outcome = normalize_name(outcome_name)

            for pk, pv in best_match["outcomes"].items():
                norm_pk = normalize_name(pk)
                score = fuzz.ratio(norm_outcome, norm_pk)
                if score > 75 or (len(norm_outcome) > 3 and norm_outcome in norm_pk) or (len(norm_pk) > 3 and norm_pk in norm_outcome):
                    poly_prob = pv["implied_prob"]
                    poly_outcome_key = pk
                    break

            # Binary Yes/No: check if outcome name is in the question
            if poly_prob is None and len(best_match["outcomes"]) == 2:
                yes_data = best_match["outcomes"].get("Yes")
                if yes_data and outcome_name.lower() in best_match["market_question"].lower():
                    poly_prob = yes_data["implied_prob"]
                    poly_outcome_key = "Yes"

            if poly_prob is None:
                continue

            # Skip if Polymarket price is 0 or 100 (illiquid/stale)
            if poly_prob <= 0.5 or poly_prob >= 99.5:
                continue

            divergence = odds_prob - poly_prob  # positive = poly is cheap
            abs_div = abs(divergence)

            # Half-Kelly criterion: f* = (b*p - q) / (2*b)
            # where b = net decimal odds, p = true prob (sharp), q = 1-p
            p = odds_prob / 100  # true probability (from sharp book)
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

            outcome_comparisons.append({
                "outcome": outcome_name,
                "outcome_name": outcome_name,  # alias for frontend
                "poly_outcome": poly_outcome_key,
                "sharp_prob": odds_prob,
                "consensus_prob": consensus_prob,
                "poly_prob": poly_prob,
                "poly_price": poly_prob / 100 if poly_prob else 0,  # 0-1 scale for frontend
                "kalshi_prob": kalshi_prob,
                "kalshi_ticker": kalshi_ticker,
                "kalshi_divergence": kalshi_divergence,
                "divergence": round(divergence, 2),
                "divergence_pct": round(divergence, 2),  # alias for frontend
                "abs_divergence": round(abs_div, 2),
                "cheap_on": "Polymarket" if divergence > 0 else "Bookmaker",
                "kelly_pct": kelly_pct,
                "kelly_fraction": kelly_pct / 100 if kelly_pct else 0,  # 0-1 scale for frontend
                "is_signal": abs_div >= DIVERGENCE_THRESHOLD,
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
        true_prob_no_vig = round(best_oc["sharp_prob"] * 100 / total_prob, 2) if total_prob > 0 else best_oc["sharp_prob"]

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
                # Try fuzzy match
                for (sh, sa), sc_data in score_map.items():
                    if fuzz.ratio(home_n, sh) > 80 and fuzz.ratio(away_n, sa) > 80:
                        score = sc_data
                        break
            if not score:
                continue

            # Determine if edge was correct
            outcome_name = normalize_name(edge["outcome"])
            winner_name = normalize_name(score["winner"])
            divergence = edge["divergence"] or 0

            # Edge said "buy on Polymarket" (divergence > 0) = book thinks outcome more likely
            # So the edge is "correct" if that outcome actually won
            if divergence > 0:
                # We recommended this outcome — check if it won
                is_correct = fuzz.ratio(outcome_name, winner_name) > 75
            else:
                # Negative divergence = Polymarket overpriced this outcome
                # "Correct" if this outcome did NOT win
                is_correct = fuzz.ratio(outcome_name, winner_name) <= 75

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
# Line movement: compute trend from recent snapshots
# ---------------------------------------------------------------------------

def _compute_edge_trends(comparisons: list[dict]) -> dict:
    """For each event+outcome, compute trend direction from recent snapshots."""
    trends = {}
    cutoff_2h = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    with _get_db() as conn:
        for comp in comparisons:
            event_name = comp.get("home_team", "") + (" vs " + comp.get("away_team", "") if comp.get("away_team") else "")
            for oc in comp.get("outcomes", []):
                outcome = oc.get("outcome", "")
                key = f"{event_name}|{outcome}"

                # Get recent snapshots for this event+outcome
                rows = conn.execute(
                    "SELECT divergence, snapshot_at FROM sports_market_snapshots WHERE event_name = ? AND outcome = ? AND snapshot_at >= ? ORDER BY snapshot_at",
                    (event_name, outcome, cutoff_24h),
                ).fetchall()

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
        allowed_sports = json.loads(cfg.get("sports", "[]"))
        if allowed_sports and sport not in allowed_sports:
            continue

        # Check cooldown (max 1 alert per 5 min per user)
        last = cfg.get("last_alert_at", "")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
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

        # Send Telegram
        tg_token = cfg.get("telegram_bot_token", "")
        tg_chat = cfg.get("telegram_chat_id", "")
        if tg_token and tg_chat:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={"chat_id": tg_chat, "text": msg, "parse_mode": "HTML"},
                    timeout=10,
                )
            except Exception as e:
                log.warning("Telegram alert error: %s", e)

        # Send webhook
        webhook_url = cfg.get("webhook_url", "")
        if webhook_url:
            try:
                requests.post(
                    webhook_url,
                    json={"text": msg, "signals": user_signals[:5], "sport": sport},
                    timeout=10,
                )
            except Exception as e:
                log.warning("Webhook alert error: %s", e)

        # Update last alert time
        with _get_db() as conn:
            conn.execute(
                "UPDATE sports_alert_config SET last_alert_at = ? WHERE user_id = ?",
                (datetime.now(timezone.utc).isoformat(), cfg["user_id"]),
            )


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
    try:
        resp = requests.get(
            f"{POLYMARKET_HOST}/book",
            params={"token_id": token_id},
            timeout=10,
        )
        resp.raise_for_status()
        book = resp.json()
    except Exception:
        return {"bid_depth": 0, "ask_depth": 0, "mid_price": 0, "executable_size": 0}

    bids = book.get("bids", [])
    asks = book.get("asks", [])

    # Compute depth within 2% of mid price
    best_bid = float(bids[0]["price"]) if bids else 0
    best_ask = float(asks[0]["price"]) if asks else 1
    mid = (best_bid + best_ask) / 2 if (best_bid and best_ask) else 0
    spread = best_ask - best_bid if (best_bid and best_ask) else 0

    bid_depth = sum(float(b["size"]) for b in bids if float(b["price"]) >= mid * 0.98)
    ask_depth = sum(float(a["size"]) for a in asks if float(a["price"]) <= mid * 1.02)

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
            "spread_pct": round((pm["spread"] / max(outcomes[0]["poly_price"], 0.01)) * 100, 2) if outcomes else 0,
            "edge_direction": "-",
            "time_to_event_hours": None,
            "confidence_score": 0,
            "kalshi_event": None,
            "kalshi_volume": 0,
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
        })

    comparisons.sort(key=lambda x: -x["kalshi_volume"])
    return comparisons


_resolve_counter = 0  # run auto-resolve every 6th cycle (~30 min)
_bg_scan_counter = 0  # run background multi-sport scan every 4th cycle (~20 min)


async def data_updater():
    """Background task that polls APIs and updates dashboard_data."""
    global _poly_cache, _poly_cache_time, _resolve_counter, _bg_scan_counter
    while True:
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
            if sport != dashboard_data["active_sport"]:
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
                print(f"Alert send error: {alert_err}", flush=True)

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
            async with _data_lock:
                dashboard_data["error"] = str(e)
            print(f"Update error: {e}", flush=True)

        # Periodic: auto-resolve edges every ~30 min
        _resolve_counter += 1
        if _resolve_counter >= 6:
            _resolve_counter = 0
            try:
                await asyncio.to_thread(_run_score_resolution, sport)
            except Exception as res_err:
                print(f"Auto-resolve error: {res_err}", flush=True)

        # Periodic: background multi-sport scan every ~20 min
        _bg_scan_counter += 1
        if _bg_scan_counter >= 4:
            _bg_scan_counter = 0
            asyncio.create_task(_background_multi_sport_scan())

        # Wait for poll interval OR immediate rescan trigger
        _scan_event.clear()
        try:
            await asyncio.wait_for(_scan_event.wait(), timeout=POLL_INTERVAL)
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
    msg = json.dumps({"type": "update", "data": dashboard_data}, default=str)
    dead = set()
    for ws in list(connected_ws):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    connected_ws -= dead


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
        closed = False
        try:
            os.write(fd, json.dumps(existing, indent=2, default=str).encode())
            os.close(fd)
            closed = True
            os.replace(tmp, str(SIGNALS_FILE))
        except BaseException:
            if not closed:
                os.close(fd)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    asyncio.create_task(data_updater())
    # Backfill historical markets in background thread (non-blocking)
    async def _backfill_wrapper():
        try:
            await asyncio.get_event_loop().run_in_executor(None, backfill_historical_markets)
        except Exception as e:
            print(f"Backfill error: {e}", flush=True)
    asyncio.create_task(_backfill_wrapper())
    # Run initial auto-resolution in background
    async def _initial_resolve():
        try:
            await asyncio.to_thread(_auto_resolve_edges)
        except Exception as e:
            print(f"Initial auto-resolve error: {e}", flush=True)
    asyncio.create_task(_initial_resolve())
    print(f"Sports Dashboard started. Polling {dashboard_data['active_sport']} every {POLL_INTERVAL}s")
    if not ODDS_API_KEY:
        print("WARNING: ODDS_API_KEY not set — bookmaker odds will be unavailable")


# ---------------------------------------------------------------------------
# Auth pages & endpoints
# ---------------------------------------------------------------------------
# Auth is handled by the gateway. These endpoints redirect to it or return
# user info from the gateway-forwarded headers + local profiles.

@app.get("/login")
async def login_page():
    return RedirectResponse("https://narve.ai/login", status_code=302)


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
    if requested_sport and requested_sport != dashboard_data.get("active_sport"):
        return JSONResponse(
            {"status": "switching", "active_sport": dashboard_data.get("active_sport"),
             "requested_sport": requested_sport},
            status_code=202,
        )
    return JSONResponse(dashboard_data)


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
    active = requested_sport if (requested_sport and requested_sport in SPORTS) else dashboard_data["active_sport"]
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

@app.post("/api/trades")
async def create_trade(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    body = await request.json()
    market_name = body.get("market_name", "")
    outcome = body.get("outcome", "")
    entry_price = float(body.get("entry_price", 0))
    amount = float(body.get("amount", 0))
    if not market_name or entry_price <= 0 or amount <= 0:
        return JSONResponse({"error": "market_name, entry_price > 0, and amount > 0 required"}, status_code=400)
    with _get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sports_trades (user_id, market_name, outcome, entry_price, amount) VALUES (?, ?, ?, ?, ?)",
            (user["id"], market_name, outcome, entry_price, amount),
        )
        trade_id = cur.lastrowid
    log_activity(user["id"], "create_trade", f"Trade #{trade_id}: {market_name}")
    return JSONResponse({"status": "ok", "trade_id": trade_id})


@app.get("/api/trades")
async def list_trades(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sports_trades WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
    return JSONResponse({"trades": [dict(r) for r in rows]})


@app.post("/api/trades/{trade_id}/resolve")
async def resolve_trade(trade_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    body = await request.json()
    exit_price = float(body.get("exit_price", 0))
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
        # amount is dollars invested, entry_price is cents; shares = amount / (entry_price/100)
        pnl = round((exit_price - trade["entry_price"]) * trade["amount"] / trade["entry_price"], 2)
        conn.execute(
            "UPDATE sports_trades SET status = 'closed', exit_price = ?, pnl = ?, resolved_at = ? WHERE id = ?",
            (exit_price, pnl, datetime.now(timezone.utc).isoformat(), trade_id),
        )
    log_activity(user["id"], "resolve_trade", f"Trade #{trade_id}: PnL={pnl}")
    return JSONResponse({"status": "ok", "pnl": pnl})


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
    closed = [t for t in trades if t["status"] == "closed"]
    open_trades = [t for t in trades if t["status"] == "open"]
    total_pnl = round(sum(t.get("pnl") or 0 for t in closed), 2)
    wins = sum(1 for t in closed if (t.get("pnl") or 0) > 0)
    win_rate = round(wins / len(closed) * 100, 1) if closed else 0.0
    total_invested = sum(t["amount"] for t in closed) if closed else 0
    roi = round(total_pnl / total_invested * 100, 2) if total_invested > 0 else 0.0
    return JSONResponse({
        "total_pnl": total_pnl,
        "win_rate": win_rate,
        "open_count": len(open_trades),
        "closed_count": len(closed),
        "roi": roi,
    })


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
            widgets = json.loads(widgets)
        if isinstance(data_points, str):
            data_points = json.loads(data_points)
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
    body = await request.json()
    visible_widgets = body.get("visible_widgets", ["stats", "top_opps", "hero", "events"])
    visible_data_points = body.get("visible_data_points", [])
    card_expanded_default = int(body.get("card_expanded_default", False))
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
                try:
                    op = mkt.get("outcomePrices", "0")
                    if isinstance(op, list):
                        final_price = float(op[0]) if op else None
                    else:
                        final_price = float(str(op).strip("[]").split(",")[0])
                except (ValueError, IndexError, TypeError):
                    pass
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
        cfg["sports"] = json.loads(cfg.get("sports", "[]"))
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
    body = await request.json()
    enabled = int(body.get("enabled", 0))
    tg_chat = str(body.get("telegram_chat_id", "")).strip()
    tg_token = str(body.get("telegram_bot_token", "")).strip()
    webhook_url = str(body.get("webhook_url", "")).strip()
    min_edge = float(body.get("min_edge", 5.0))
    alert_sports = body.get("sports", [])

    with _get_db() as conn:
        # If token is masked, keep existing
        if tg_token == "****":
            existing = conn.execute(
                "SELECT telegram_bot_token FROM sports_alert_config WHERE user_id = ?", (user["id"],)
            ).fetchone()
            if existing:
                tg_token = existing[0] or ""
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
    tg_token = cfg.get("telegram_bot_token", "")
    tg_chat = cfg.get("telegram_chat_id", "")
    if tg_token and tg_chat:
        try:
            requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={"chat_id": tg_chat, "text": msg},
                timeout=10,
            )
            sent = True
        except Exception as e:
            return JSONResponse({"error": f"Telegram error: {e}"}, status_code=500)
    webhook_url = cfg.get("webhook_url", "")
    if webhook_url:
        try:
            requests.post(webhook_url, json={"text": msg}, timeout=10)
            sent = True
        except Exception as e:
            return JSONResponse({"error": f"Webhook error: {e}"}, status_code=500)
    if not sent:
        return JSONResponse({"error": "No Telegram or webhook configured"}, status_code=400)
    return JSONResponse({"status": "ok", "message": "Test alert sent"})


# ---------------------------------------------------------------------------
# Match flagging endpoint
# ---------------------------------------------------------------------------

@app.post("/api/flag-match")
async def flag_match(request: Request):
    """Let users report a bad fuzzy match."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    body = await request.json()
    home = body.get("home_team", "")
    away = body.get("away_team", "")
    poly_q = body.get("poly_question", "")
    reason = body.get("reason", "")
    if not home:
        return JSONResponse({"error": "home_team required"}, status_code=400)
    with _get_db() as conn:
        conn.execute(
            "INSERT INTO sports_match_flags (user_id, home_team, away_team, poly_question, reason) VALUES (?, ?, ?, ?, ?)",
            (user["id"], home, away, poly_q, reason),
        )
    log_activity(user["id"], "flag_match", f"{home} vs {away}: {reason}")
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
    html = DASHBOARD_HTML.replace("__USER_THRESHOLD__", str(user_threshold))
    html = html.replace("__USER_SPORT__", _html_mod.escape(user_sport).replace("\\", "\\\\").replace("'", "\\'"))
    html = html.replace("__USERNAME__", _html_mod.escape(user["username"]).replace("\\", "\\\\").replace("'", "\\'"))
    return HTMLResponse(html)


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sharpe — Sports Market Intelligence</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0c0d10;
    --surface: #131417;
    --surface2: #18191e;
    --surface3: #1f2027;
    --border: rgba(255,255,255,0.06);
    --border-light: rgba(255,255,255,0.08);
    --text: #ebebef;
    --text-secondary: #8b8d98;
    --muted: #5c5e6a;
    --accent: #818cf8;
    --accent-dim: rgba(99,102,241,0.10);
    --green: #34d399;
    --green-dim: rgba(52,211,153,0.08);
    --green-mid: rgba(52,211,153,0.14);
    --red: #f87171;
    --red-dim: rgba(248,113,113,0.08);
    --yellow: #fbbf24;
    --yellow-dim: rgba(251,191,36,0.08);
    --blue: #818cf8;
    --blue-dim: rgba(99,102,241,0.08);
    --purple: #a78bfa;
    --purple-dim: rgba(167,139,250,0.08);
    --orange: #fb923c;
    --orange-dim: rgba(251,146,60,0.08);
    --gold: #fbbf24;
    --gold-dim: rgba(251,191,36,0.08);
    --radius: 12px;
    --radius-sm: 8px;
    --radius-xs: 6px;
    --shadow: 0 1px 3px rgba(0,0,0,0.2);
    --shadow-sm: 0 1px 2px rgba(0,0,0,0.15);
    --positive: #34d399;
    --negative: #f87171;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    overflow-x: hidden;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    font-weight: 400;
  }
  a { color: var(--text); text-decoration: none; }
  a:hover { opacity: 0.6; }

  /* ---- Scrollbar ---- */
  ::-webkit-scrollbar { width: 4px; height: 4px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border-light); }

  /* ---- Nav ---- */
  .nav {
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 32px; height: 52px;
    background: var(--surface); border-bottom: 1px solid var(--border);
    position: sticky; top: 0; z-index: 100;
  }
  .nav-left { display: flex; align-items: center; gap: 16px; }
  .nav-logo {
    width: 28px; height: 28px;
    background: var(--text); color: var(--surface);
    display: flex; align-items: center; justify-content: center;
    font-weight: 600; font-size: 14px;
  }
  .nav-brand { font-weight: 600; font-size: 16px; letter-spacing: -0.02em; }
  .nav-status { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--muted); }
  .status-dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--positive); animation: pulse 2s infinite;
  }
  .status-dot.error { background: var(--negative); }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.3; } }
  .nav-right { display: flex; align-items: center; gap: 8px; }
  .nav-link {
    font-size: 12px; color: var(--text-secondary); cursor: pointer;
    padding: 6px 10px; transition: opacity .15s;
    background: none; border: none; font-family: inherit; font-weight: 400;
  }
  .nav-link:hover { opacity: 0.6; }
  .nav-link.admin { display: none; }
  .btn-upgrade {
    background: var(--text); color: var(--surface);
    font-weight: 500; font-size: 11px; letter-spacing: 0.04em;
    text-transform: uppercase;
    padding: 6px 14px; border: none;
    cursor: pointer; display: none; transition: opacity .15s;
  }
  .btn-upgrade:hover { opacity: 0.8; }

  /* ---- Main Tabs ---- */
  .main-tabs {
    display: flex; gap: 0; background: var(--surface);
    border-bottom: 1px solid var(--border); padding: 0 32px;
  }
  .main-tab {
    padding: 12px 20px; font-size: 12px; font-weight: 500;
    color: var(--muted); cursor: pointer; border: none; background: none;
    border-bottom: 1px solid transparent; transition: all .15s;
    font-family: inherit; text-transform: uppercase; letter-spacing: 0.06em;
  }
  .main-tab:hover { color: var(--text-secondary); }
  .main-tab.active { color: var(--text); border-bottom-color: var(--text); }

  /* ---- Sport Tabs ---- */
  .sport-tabs {
    background: var(--surface); padding: 0;
  }
  .category-row {
    display: flex; gap: 0; padding: 0 32px;
    border-bottom: 1px solid var(--border);
  }
  .category-btn {
    padding: 10px 20px; font-size: 12px; font-weight: 500;
    background: none; border: none; border-bottom: 1px solid transparent;
    color: var(--muted); cursor: pointer; transition: all .15s;
    text-transform: uppercase; letter-spacing: 0.06em;
  }
  .category-btn:hover { color: var(--text-secondary); }
  .category-btn.active { color: var(--text); border-bottom-color: var(--text); }
  .subcategory-row {
    display: flex; gap: 0; padding: 0 32px;
    overflow-x: auto; flex-wrap: nowrap;
    border-bottom: 1px solid var(--border);
  }
  .sport-tab {
    padding: 8px 16px; font-size: 12px; font-weight: 400;
    background: none; border: none; border-bottom: 1px solid transparent;
    color: var(--muted); cursor: pointer; white-space: nowrap;
    transition: all .15s;
  }
  .sport-tab:hover { color: var(--text-secondary); }
  .sport-tab.active { color: var(--text); border-bottom-color: var(--text); }

  /* ---- Container ---- */
  .container { max-width: 1100px; margin: 0 auto; padding: 32px 32px 80px; }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  /* ---- Hero Banner ---- */
  .hero {
    background: var(--surface);
    border: 1px solid var(--border);
    padding: 32px; margin-bottom: 32px; position: relative;
  }
  .hero-dismiss {
    position: absolute; top: 12px; right: 16px;
    background: none; border: none; color: var(--muted); cursor: pointer;
    font-size: 18px; line-height: 1;
  }
  .hero-dismiss:hover { color: var(--text); }
  .hero h2 { font-size: 18px; font-weight: 500; margin-bottom: 20px; letter-spacing: -0.01em; }
  .hero-steps { display: flex; gap: 32px; flex-wrap: wrap; }
  .hero-step {
    flex: 1; min-width: 200px; display: flex; gap: 12px; align-items: flex-start;
  }
  .hero-step-num {
    width: 24px; height: 24px;
    background: var(--text); color: var(--surface);
    display: flex; align-items: center; justify-content: center;
    font-weight: 500; font-size: 12px; flex-shrink: 0;
  }
  .hero-step-text { font-size: 13px; color: var(--text-secondary); line-height: 1.6; font-weight: 300; }
  .hero-step-text strong { color: var(--text); font-weight: 500; }

  /* ---- Top Opportunities ---- */
  .top-opps { display: flex; gap: 1px; margin-bottom: 32px; flex-wrap: wrap; background: var(--border); }
  .top-opp {
    flex: 1; min-width: 220px; padding: 20px;
    background: var(--surface); cursor: pointer; transition: opacity .2s;
  }
  .top-opp:hover { opacity: 0.7; }
  .top-opp-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
  .top-opp-edge { font-size: 20px; font-weight: 300; color: var(--positive); }
  .top-opp-team { font-size: 13px; font-weight: 500; color: var(--text); margin-bottom: 4px; }
  .top-opp-sub { font-size: 11px; color: var(--muted); font-weight: 300; }

  /* ---- Stats Row ---- */
  .stats-row { display: grid; grid-template-columns: repeat(6, 1fr); gap: 1px; margin-bottom: 32px; background: var(--border); }
  .stat-card {
    background: var(--surface); padding: 20px; text-align: left;
  }
  .stat-value { font-size: 24px; font-weight: 300; letter-spacing: -0.02em; }
  .stat-label { font-size: 11px; color: var(--muted); margin-top: 4px; text-transform: uppercase; letter-spacing: 0.04em; font-weight: 400; }
  .stat-sub { font-size: 11px; color: var(--muted); margin-top: 4px; font-weight: 300; }

  /* ---- Toolbar ---- */
  .toolbar {
    display: flex; align-items: center; gap: 12px;
    margin-bottom: 24px; flex-wrap: wrap;
  }
  .toolbar input[type="text"] {
    flex: 1; min-width: 200px; padding: 8px 0;
    background: transparent; border: none; border-bottom: 1px solid var(--border);
    color: var(--text); font-size: 13px; outline: none; font-family: inherit; font-weight: 300;
  }
  .toolbar input[type="text"]:focus { border-bottom-color: var(--text); }
  .toolbar input[type="text"]::placeholder { color: var(--muted); }
  .toolbar select {
    padding: 8px 0; background: transparent;
    border: none; border-bottom: 1px solid var(--border);
    color: var(--text); font-size: 13px; font-family: inherit; cursor: pointer; font-weight: 400;
  }
  .toolbar .toggle-wrap {
    display: flex; align-items: center; gap: 6px; font-size: 12px;
    color: var(--text-secondary); cursor: pointer; user-select: none; font-weight: 400;
  }
  .toolbar .toggle-wrap input { accent-color: var(--text); }
  .btn-sm {
    padding: 6px 14px; font-size: 12px; font-weight: 400;
    border: 1px solid var(--border); background: transparent;
    color: var(--text-secondary); cursor: pointer; font-family: inherit;
    transition: all .15s; text-transform: uppercase; letter-spacing: 0.04em;
  }
  .btn-sm:hover { border-color: var(--text); color: var(--text); }

  /* ---- Event Cards ---- */
  .cards-grid { display: flex; flex-direction: column; gap: 1px; background: var(--border); }
  .card {
    background: var(--surface); overflow: hidden;
    animation: fadeIn .3s ease both;
  }
  .card:nth-child(1) { animation-delay: 0s; }
  .card:nth-child(2) { animation-delay: .03s; }
  .card:nth-child(3) { animation-delay: .06s; }
  .card:nth-child(4) { animation-delay: .09s; }
  .card:nth-child(5) { animation-delay: .12s; }
  @keyframes fadeIn { from { opacity:0; } to { opacity:1; } }

  .card-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 16px 20px; cursor: pointer; gap: 12px;
  }
  .card-header:hover { background: var(--surface2); }
  .card-teams { font-size: 14px; font-weight: 500; flex: 1; letter-spacing: -0.01em; }
  .card-teams span.vs { color: var(--muted); font-weight: 300; margin: 0 8px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
  .card-time { font-size: 11px; color: var(--muted); white-space: nowrap; font-weight: 300; }
  .card-meta { display: flex; align-items: center; gap: 10px; }
  .signal-badge {
    font-size: 10px; font-weight: 500; padding: 3px 8px;
    text-transform: uppercase; letter-spacing: 0.06em;
  }
  .signal-badge.buy { background: rgba(22,163,74,0.08); color: var(--positive); }
  .signal-badge.sell { background: var(--red-dim); color: var(--negative); }
  .signal-badge.neutral { background: var(--surface3); color: var(--muted); }
  .confidence-stars { display: inline-flex; gap: 1px; font-size: 12px; }
  .star-filled { color: var(--text); }
  .star-empty { color: var(--border); }

  .card-action-hint {
    padding: 0 20px 12px;
    font-size: 12px; color: var(--positive); font-weight: 400;
  }

  .outcome-chips {
    display: flex; gap: 8px; padding: 0 20px 12px; flex-wrap: wrap;
  }
  .outcome-chip {
    padding: 3px 10px; font-size: 11px; font-weight: 500;
    background: var(--surface2); border: 1px solid var(--border);
    text-transform: uppercase; letter-spacing: 0.02em;
  }
  .outcome-chip.pos { background: rgba(22,163,74,0.06); border-color: rgba(22,163,74,0.2); color: var(--positive); }
  .outcome-chip.neg { background: var(--red-dim); border-color: rgba(220,38,38,0.2); color: var(--negative); }

  /* Card Detail (expandable) */
  .card-detail { display: none; padding: 0 20px 24px; }
  .card-detail.open { display: block; }

  .prob-compare { margin-bottom: 20px; }
  .prob-row { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; font-size: 12px; }
  .prob-label { width: 120px; font-weight: 400; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .prob-bars { flex: 1; display: flex; gap: 3px; align-items: center; }
  .prob-bar { height: 4px; transition: width .3s; }
  .prob-bar.book { background: var(--text); }
  .prob-bar.poly { background: var(--muted); }
  .prob-bar.kalshi { background: var(--text-secondary); }
  .prob-legend { display: flex; gap: 16px; font-size: 10px; color: var(--muted); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.04em; }
  .prob-legend span::before { content: ''; display: inline-block; width: 12px; height: 2px; margin-right: 6px; vertical-align: middle; }
  .prob-legend .leg-book::before { background: var(--text); }
  .prob-legend .leg-poly::before { background: var(--muted); }
  .prob-legend .leg-kalshi::before { background: var(--text-secondary); }
  .btn-action.kalshi-btn { background: var(--text); color: var(--surface); }
  .btn-action.kalshi-btn:hover { opacity: 0.8; }

  /* Market Intel Grid */
  .intel-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 1px; margin-bottom: 20px; background: var(--border);
  }
  .intel-item {
    background: var(--surface); padding: 12px 14px; font-size: 12px;
  }
  .intel-item-label {
    color: var(--muted); font-size: 10px; margin-bottom: 4px;
    display: flex; align-items: center; gap: 4px;
    text-transform: uppercase; letter-spacing: 0.04em; font-weight: 400;
  }
  .intel-item-value { font-weight: 500; font-size: 14px; }
  .info-i {
    display: inline-flex; align-items: center; justify-content: center;
    width: 14px; height: 14px;
    background: var(--surface2); color: var(--muted);
    font-size: 9px; font-weight: 500; cursor: help;
    position: relative;
  }
  .info-i .tooltip {
    display: none; position: absolute; bottom: 120%; left: 50%;
    transform: translateX(-50%); background: var(--text);
    color: var(--surface); border: none;
    padding: 8px 12px; font-size: 11px; font-weight: 300;
    width: 220px; line-height: 1.5;
    z-index: 50; white-space: normal;
  }
  .info-i:hover .tooltip { display: block; }

  /* Outcome Table */
  .outcome-table-wrap { overflow-x: auto; margin-bottom: 20px; }
  .outcome-table {
    width: 100%; border-collapse: collapse; font-size: 12px;
  }
  .outcome-table th {
    text-align: left; padding: 8px 10px; font-weight: 500;
    color: var(--muted); font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.06em; border-bottom: 1px solid var(--border);
  }
  .outcome-table td {
    padding: 10px 10px; border-bottom: 1px solid var(--border); font-weight: 300;
  }
  .outcome-table tr:last-child td { border-bottom: none; }

  /* Bookmaker Breakdown */
  .bookie-toggle {
    font-size: 11px; color: var(--muted); cursor: pointer;
    background: none; border: none; font-family: inherit;
    margin-bottom: 8px; display: flex; align-items: center; gap: 4px;
    text-transform: uppercase; letter-spacing: 0.04em;
  }
  .bookie-toggle:hover { color: var(--text-secondary); }
  .bookie-section { display: none; margin-bottom: 12px; }
  .bookie-section.open { display: block; }
  .bookie-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .bookie-table th {
    text-align: left; padding: 6px 8px; font-weight: 500;
    color: var(--muted); font-size: 10px; border-bottom: 1px solid var(--border);
    text-transform: uppercase; letter-spacing: 0.04em;
  }
  .bookie-table td { padding: 6px 8px; border-bottom: 1px solid var(--border); font-weight: 300; }

  /* Card Actions */
  .card-actions {
    display: flex; gap: 8px; align-items: center; padding-top: 16px;
    border-top: 1px solid var(--border); flex-wrap: wrap;
  }
  .btn-action {
    padding: 7px 14px; font-size: 11px; font-weight: 500;
    border: 1px solid var(--border); background: transparent;
    color: var(--text-secondary); cursor: pointer; font-family: inherit;
    transition: all .15s; display: inline-flex; align-items: center; gap: 6px;
    text-transform: uppercase; letter-spacing: 0.04em;
  }
  .btn-action:hover { border-color: var(--text); color: var(--text); }
  .btn-action.primary {
    background: var(--text); border-color: var(--text); color: var(--surface);
  }
  .btn-action.primary:hover { opacity: 0.8; }
  .btn-watchlist.active { color: var(--text); border-color: var(--text); }

  /* ---- Free Tier Gate ---- */
  .gate-overlay { position: relative; }
  .gate-blur { filter: blur(6px); pointer-events: none; user-select: none; opacity: 0.5; }
  .gate-cta {
    position: absolute; top: 0; left: 0; right: 0; bottom: 0;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    z-index: 10; background: rgba(250,250,250,0.85);
  }
  .gate-cta h3 { font-size: 16px; font-weight: 500; margin-bottom: 16px; }
  .gate-cta .btn-upgrade-big {
    background: var(--text); color: var(--surface);
    font-weight: 500; font-size: 12px; letter-spacing: 0.04em;
    text-transform: uppercase;
    padding: 10px 28px; border: none; cursor: pointer;
  }
  .gate-cta .btn-upgrade-big:hover { opacity: 0.8; }

  /* ---- Profit Tracker ---- */
  .profit-summary { display: grid; grid-template-columns: repeat(5, 1fr); gap: 1px; margin-bottom: 32px; background: var(--border); }
  .profit-card { background: var(--surface); padding: 20px; text-align: left; }
  .profit-value { font-size: 22px; font-weight: 300; }
  .profit-label { font-size: 11px; color: var(--muted); margin-top: 4px; text-transform: uppercase; letter-spacing: 0.04em; }
  .trades-table-wrap { overflow-x: auto; }
  .trades-table { width: 100%; border-collapse: collapse; font-size: 12px; background: var(--surface); }
  .trades-table th { text-align: left; padding: 10px 12px; font-weight: 500; color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; background: var(--surface2); }
  .trades-table td { padding: 10px 12px; border-bottom: 1px solid var(--border); font-weight: 300; }
  .trades-table tr:last-child td { border-bottom: none; }

  /* ---- Watchlist ---- */
  .watchlist-list { display: flex; flex-direction: column; gap: 1px; background: var(--border); }
  .watchlist-item {
    display: flex; justify-content: space-between; align-items: center;
    background: var(--surface); padding: 16px 20px;
  }
  .watchlist-item-info { display: flex; flex-direction: column; gap: 4px; }
  .watchlist-item-name { font-weight: 500; font-size: 14px; }
  .watchlist-item-edge { font-size: 12px; font-weight: 300; }
  .watchlist-remove {
    background: none; border: 1px solid var(--border);
    color: var(--negative); font-size: 11px; padding: 6px 12px;
    cursor: pointer; font-family: inherit; text-transform: uppercase; letter-spacing: 0.04em;
  }
  .watchlist-remove:hover { border-color: var(--negative); }
  .empty-state { text-align: center; padding: 60px 20px; color: var(--muted); font-size: 13px; font-weight: 300; }

  /* ---- History ---- */
  .history-table-wrap { overflow-x: auto; }
  .history-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .history-table th {
    text-align: left; padding: 10px 12px; font-size: 11px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted);
    border-bottom: 1px solid var(--border);
  }
  .history-table td {
    padding: 10px 12px; border-bottom: 1px solid var(--border);
    font-weight: 300;
  }
  .history-table tr:hover td { background: var(--bg); }
  .history-res { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; font-weight: 500; }
  .history-res.yes { color: var(--positive); }
  .history-res.no { color: var(--negative); }

  /* ---- Modals ---- */
  .modal-overlay {
    display: none; position: fixed; inset: 0; z-index: 200;
    background: rgba(250,250,250,0.9);
    align-items: center; justify-content: center;
  }
  .modal-overlay.open { display: flex; }
  .modal {
    background: var(--surface); border: 1px solid var(--border);
    padding: 32px; max-width: 560px; width: 90%; max-height: 85vh; overflow-y: auto;
  }
  .modal h2 { font-size: 18px; font-weight: 500; margin-bottom: 20px; letter-spacing: -0.01em; }
  .modal-close {
    float: right; background: none; border: none;
    color: var(--muted); font-size: 18px; cursor: pointer;
  }
  .modal-close:hover { color: var(--text); }

  /* Customize Modal */
  .customize-section { margin-bottom: 24px; }
  .customize-section h3 { font-size: 11px; font-weight: 500; margin-bottom: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
  .customize-check {
    display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
    font-size: 13px; color: var(--text-secondary); cursor: pointer; font-weight: 300;
  }
  .customize-check input { accent-color: var(--text); }

  /* Upgrade Modal */
  .upgrade-content { text-align: center; }
  .upgrade-price { font-size: 32px; font-weight: 300; color: var(--text); margin: 20px 0; }
  .upgrade-price span { font-size: 14px; font-weight: 400; color: var(--muted); }
  .upgrade-features { text-align: left; margin: 24px 0; }
  .upgrade-row { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid var(--border); font-size: 13px; font-weight: 300; }
  .upgrade-row:last-child { border-bottom: none; }
  .upgrade-check { color: var(--positive); }
  .upgrade-cross { color: var(--muted); }

  /* Glossary */
  .glossary-item { margin-bottom: 16px; }
  .glossary-item dt { font-weight: 500; font-size: 13px; color: var(--text); margin-bottom: 4px; }
  .glossary-item dd { font-size: 13px; color: var(--text-secondary); line-height: 1.6; font-weight: 300; }

  /* Trade Modal Form */
  .trade-form { display: flex; flex-direction: column; gap: 16px; }
  .trade-form label { font-size: 11px; font-weight: 500; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .trade-form input, .trade-form select {
    width: 100%; padding: 8px 0; background: transparent;
    border: none; border-bottom: 1px solid var(--border);
    color: var(--text); font-size: 13px; font-family: inherit; font-weight: 300;
  }
  .trade-form input:focus { border-bottom-color: var(--text); outline: none; }
  .btn-primary {
    padding: 10px 20px; background: var(--text); color: var(--surface);
    font-weight: 500; font-size: 12px; border: none; cursor: pointer;
    font-family: inherit; text-transform: uppercase; letter-spacing: 0.04em;
  }
  .btn-primary:hover { opacity: 0.8; }

  /* ---- Trend Indicators ---- */
  .trend-arrow { font-size: 11px; font-weight: 600; margin-left: 4px; }
  .trend-arrow.widening { color: var(--green); }
  .trend-arrow.narrowing { color: var(--red); }
  .trend-arrow.stable { color: var(--muted); }
  .trend-arrow.new { color: var(--blue); }
  .trend-change { font-size: 10px; color: var(--muted); margin-left: 4px; }
  .sparkline { display: inline-block; vertical-align: middle; margin-left: 6px; }
  .sparkline canvas { display: block; }

  /* ---- Market Type Badge ---- */
  .market-type-badge {
    font-size: 9px; font-weight: 600; padding: 2px 6px;
    text-transform: uppercase; letter-spacing: 0.06em;
    background: var(--surface3); color: var(--muted);
    margin-left: 8px;
  }
  .market-type-badge.spreads { background: var(--purple-dim); color: var(--purple); }
  .market-type-badge.totals { background: var(--orange-dim); color: var(--orange); }
  .market-type-badge.futures { background: var(--gold-dim); color: var(--gold); }

  /* ---- Cross-Sport Ticker ---- */
  .cross-sport-ticker {
    background: var(--surface); border: 1px solid var(--border);
    padding: 16px 20px; margin-bottom: 24px; overflow: hidden;
  }
  .cross-sport-ticker h3 {
    font-size: 11px; font-weight: 500; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 12px;
  }
  .ticker-items { display: flex; gap: 1px; overflow-x: auto; background: var(--border); }
  .ticker-item {
    flex-shrink: 0; min-width: 200px; padding: 12px 16px;
    background: var(--surface); cursor: pointer;
  }
  .ticker-item:hover { background: var(--surface2); }
  .ticker-sport { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .ticker-teams { font-size: 12px; font-weight: 500; margin: 4px 0; }
  .ticker-edge { font-size: 14px; font-weight: 300; color: var(--green); }

  /* ---- Edge Stats Tab ---- */
  .sharpe-chart-wrap {
    background: var(--surface); border: 1px solid var(--border);
    padding: 24px; margin-bottom: 24px;
  }
  .sharpe-chart-wrap h3 {
    font-size: 14px; font-weight: 500; margin-bottom: 16px;
    text-transform: uppercase; letter-spacing: 0.04em;
  }
  .sharpe-big { font-size: 36px; font-weight: 300; margin-bottom: 4px; }
  .sharpe-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .perf-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px; margin-bottom: 24px; background: var(--border); }
  .perf-card { background: var(--surface); padding: 20px; text-align: left; }
  .perf-value { font-size: 22px; font-weight: 300; }
  .perf-label { font-size: 11px; color: var(--muted); margin-top: 4px; text-transform: uppercase; letter-spacing: 0.04em; }

  /* ---- Alerts Tab ---- */
  .alert-form { max-width: 500px; }
  .alert-form .field { margin-bottom: 20px; }
  .alert-form label { display: block; font-size: 11px; font-weight: 500; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 6px; }
  .alert-form input[type="text"], .alert-form input[type="number"] {
    width: 100%; padding: 8px 0; background: transparent;
    border: none; border-bottom: 1px solid var(--border);
    color: var(--text); font-size: 13px; font-family: inherit; font-weight: 300;
  }
  .alert-form input:focus { border-bottom-color: var(--text); outline: none; }
  .alert-toggle { display: flex; align-items: center; gap: 8px; font-size: 13px; cursor: pointer; margin-bottom: 16px; }

  /* ---- Flag Match ---- */
  .btn-flag {
    padding: 5px 10px; font-size: 10px; font-weight: 400;
    border: 1px solid var(--border); background: transparent;
    color: var(--muted); cursor: pointer; font-family: inherit;
    text-transform: uppercase; letter-spacing: 0.04em;
  }
  .btn-flag:hover { border-color: var(--yellow); color: var(--yellow); }

  /* ---- Depth Indicator ---- */
  .depth-badge {
    font-size: 10px; padding: 2px 6px;
    background: var(--surface3); color: var(--text-secondary);
  }
  .depth-badge.deep { background: var(--green-dim); color: var(--green); }
  .depth-badge.thin { background: var(--yellow-dim); color: var(--yellow); }

  /* ---- Responsive (enhanced) ---- */
  @media (max-width: 768px) {
    .nav { padding: 0 12px; height: 48px; }
    .nav-brand { font-size: 14px; }
    .nav-status { display: none; }
    .container { padding: 16px 12px 60px; }
    .stats-row { grid-template-columns: repeat(2, 1fr); }
    .stats-row .stat-card:last-child { grid-column: span 2; }
    .profit-summary { grid-template-columns: repeat(2, 1fr); }
    .profit-summary .profit-card:last-child { grid-column: span 2; }
    .hero-steps { flex-direction: column; }
    .hero { padding: 20px; margin-bottom: 20px; }
    .top-opps { flex-direction: column; }
    .intel-grid { grid-template-columns: repeat(2, 1fr); }
    .toolbar { flex-direction: column; gap: 8px; }
    .toolbar input[type="text"] { width: 100%; }
    .card-header { flex-wrap: wrap; gap: 8px; padding: 12px 14px; }
    .card-teams { font-size: 13px; }
    .card-detail { padding: 0 14px 20px; }
    .card-actions { flex-wrap: wrap; gap: 6px; }
    .btn-action { padding: 6px 10px; font-size: 10px; }
    .main-tabs { padding: 0 12px; overflow-x: auto; -webkit-overflow-scrolling: touch; }
    .main-tab { padding: 10px 14px; font-size: 11px; white-space: nowrap; }
    .category-row { padding: 0 12px; }
    .subcategory-row { padding: 0 12px; }
    .stat-value { font-size: 20px; }
    .stat-card { padding: 14px; }
    .cross-sport-ticker { padding: 12px 14px; margin-bottom: 16px; }
    .perf-grid { grid-template-columns: repeat(2, 1fr); }
    .sharpe-chart-wrap { padding: 16px; }
    .outcome-table { font-size: 11px; }
    .outcome-table th, .outcome-table td { padding: 6px 6px; }
    .prob-row { flex-wrap: wrap; }
    .prob-label { width: 80px; font-size: 11px; }
    .modal { padding: 20px; max-width: 95%; }
  }
  @media (max-width: 480px) {
    .nav-right { gap: 4px; }
    .nav-link { padding: 4px 6px; font-size: 11px; }
    .btn-upgrade { padding: 4px 10px; font-size: 10px; }
    .stats-row { grid-template-columns: 1fr; }
    .stats-row .stat-card:last-child { grid-column: span 1; }
    .profit-summary { grid-template-columns: 1fr; }
    .profit-summary .profit-card:last-child { grid-column: span 1; }
    .intel-grid { grid-template-columns: 1fr; }
    .main-tabs { overflow-x: auto; }
    .nav-right .nav-link span.hide-mobile { display: none; }
    .card-meta { flex-wrap: wrap; gap: 4px; }
    .signal-badge { font-size: 9px; padding: 2px 6px; }
    .outcome-chips { gap: 4px; }
    .outcome-chip { font-size: 10px; padding: 2px 6px; }
    .ticker-item { min-width: 160px; padding: 10px 12px; }
    .perf-grid { grid-template-columns: 1fr; }
    .card-action-hint { font-size: 11px; padding: 0 14px 10px; }
  }
</style>
</head>
<body>

<!-- ===== NAV ===== -->
<nav class="nav">
  <div class="nav-left">
    <div class="nav-logo">S</div>
    <span class="nav-brand">Sharpe</span>
    <div class="nav-status">
      <div class="status-dot" id="statusDot"></div>
      <span id="statusText">Connecting</span>
      <span id="lastUpdate" style="margin-left:8px;font-size:11px;color:var(--muted)"></span>
      <span id="countdown" style="margin-left:4px;font-size:11px;color:var(--muted)"></span>
    </div>
  </div>
  <div class="nav-right">
    <button class="btn-upgrade" id="btnUpgradeNav" onclick="openUpgrade()">Upgrade to Pro</button>
    <button class="nav-link" onclick="openGlossary()">How It Works</button>
    <a class="nav-link" href="/settings"><span class="hide-mobile">Settings </span>&#9881;</a>
    <a class="nav-link admin" id="adminLink" href="/admin">Admin</a>
  </div>
</nav>

<!-- ===== MAIN TABS ===== -->
<div class="main-tabs">
  <button class="main-tab active" onclick="switchTab('dashboard')" id="tabBtnDashboard">Dashboard</button>
  <button class="main-tab" onclick="switchTab('profit')" id="tabBtnProfit">Profit Tracker</button>
  <button class="main-tab" onclick="switchTab('watchlist')" id="tabBtnWatchlist">Watchlist</button>
  <button class="main-tab" onclick="switchTab('edgestats')" id="tabBtnEdgestats">Edge Stats</button>
  <button class="main-tab" onclick="switchTab('alerts')" id="tabBtnAlerts">Alerts</button>
  <button class="main-tab" onclick="switchTab('history')" id="tabBtnHistory">History</button>
</div>

<!-- ===== SPORT TABS ===== -->
<div class="sport-tabs" id="sportTabs"></div>

<!-- ===== CONTENT ===== -->
<div class="container">

  <!-- DASHBOARD TAB -->
  <div class="tab-content active" id="tabDashboard">

    <!-- Hero Banner -->
    <div class="hero" id="heroBanner" style="display:none;">
      <button class="hero-dismiss" onclick="dismissHero()">&times;</button>
      <h2>Find Mispriced Bets Before the Market Corrects</h2>
      <div class="hero-steps">
        <div class="hero-step">
          <div class="hero-step-num">1</div>
          <div class="hero-step-text"><strong>We scan sportsbooks</strong> and aggregate sharp odds from top bookmakers worldwide.</div>
        </div>
        <div class="hero-step">
          <div class="hero-step-num">2</div>
          <div class="hero-step-text"><strong>We compare to Polymarket</strong> prices in real time to find probability divergences.</div>
        </div>
        <div class="hero-step">
          <div class="hero-step-num">3</div>
          <div class="hero-step-text"><strong>You see the edge</strong> — when Polymarket is cheaper, you buy before the price corrects.</div>
        </div>
      </div>
    </div>

    <!-- Cross-Sport Ticker -->
    <div class="cross-sport-ticker" id="crossSportTicker" style="display:none;">
      <h3>Top Edges Across All Sports</h3>
      <div class="ticker-items" id="tickerItems"></div>
    </div>

    <!-- Top Opportunities -->
    <div class="top-opps" id="topOpps"></div>

    <!-- Stats Row -->
    <div class="stats-row">
      <div class="stat-card">
        <div class="stat-value" id="statScanned">-</div>
        <div class="stat-label">Events Scanned</div>
      </div>
      <div class="stat-card">
        <div class="stat-value" id="statPolyListed">-</div>
        <div class="stat-label">Polymarket Listed</div>
      </div>
      <div class="stat-card">
        <div class="stat-value" id="statKalshi" style="color:var(--text)">-</div>
        <div class="stat-label">Kalshi Markets</div>
      </div>
      <div class="stat-card">
        <div class="stat-value" id="statMatched">-</div>
        <div class="stat-label">Matched</div>
      </div>
      <div class="stat-card">
        <div class="stat-value" id="statOpps" style="color:var(--text)">-</div>
        <div class="stat-label">Opportunities</div>
        <div class="stat-sub">&ge; <span id="threshDisplay">__USER_THRESHOLD__</span>% edge</div>
      </div>
      <div class="stat-card">
        <div class="stat-value" id="statBestEdge" style="color:var(--text)">-</div>
        <div class="stat-label">Best Edge</div>
      </div>
    </div>

    <!-- Toolbar -->
    <div class="toolbar">
      <input type="text" id="searchInput" placeholder="Search events..." oninput="render()">
      <select id="sortSelect" onchange="render()">
        <option value="edge">Sort: Edge %</option>
        <option value="confidence">Sort: Confidence</option>
        <option value="time">Sort: Time to Event</option>
        <option value="volume">Sort: Volume</option>
        <option value="kelly">Sort: Kelly Size</option>
        <option value="book_agreement">Sort: Book Agreement</option>
      </select>
      <select id="marketTypeFilter" onchange="render()" style="border:none;border-bottom:1px solid var(--border);background:transparent;font-size:12px;padding:4px 8px;font-family:inherit;color:var(--text);">
        <option value="all">All Markets</option>
        <option value="h2h">Moneyline</option>
        <option value="spreads">Spreads</option>
        <option value="totals">Totals</option>
        <option value="futures">Futures</option>
      </select>
      <label class="toggle-wrap">
        <input type="checkbox" id="signalsToggle" onchange="signalsOnly=this.checked;render()">
        Opportunities Only
      </label>
      <button class="btn-sm" onclick="openCustomize()">Customize</button>
    </div>

    <!-- Cards -->
    <div class="cards-grid" id="cardsGrid"></div>
  </div>

  <!-- PROFIT TRACKER TAB -->
  <div class="tab-content" id="tabProfit">
    <div class="profit-summary" id="profitSummary"></div>
    <div style="display:flex;justify-content:flex-end;margin-bottom:16px;">
      <button class="btn-primary" onclick="openTradeModal()">+ Log Trade</button>
    </div>
    <div class="trades-table-wrap">
      <table class="trades-table" id="tradesTable">
        <thead><tr><th>Market</th><th>Outcome</th><th>Entry</th><th>Amount</th><th>Status</th><th>P&amp;L</th><th>Date</th></tr></thead>
        <tbody id="tradesBody"></tbody>
      </table>
    </div>
    <div class="empty-state" id="tradesEmpty" style="display:none;">No trades logged yet. Start tracking your trades!</div>
  </div>

  <!-- WATCHLIST TAB -->
  <div class="tab-content" id="tabWatchlist">
    <div class="watchlist-list" id="watchlistList"></div>
    <div class="empty-state" id="watchlistEmpty" style="display:none;">Your watchlist is empty. Star events from the dashboard to track them.</div>
  </div>

  <!-- EDGE STATS TAB -->
  <div class="tab-content" id="tabEdgestats">
    <div class="perf-grid" id="perfGrid"></div>
    <div class="sharpe-chart-wrap">
      <h3>Rolling Edge Performance</h3>
      <div style="display:flex;align-items:flex-end;gap:32px;margin-bottom:24px;">
        <div>
          <div class="sharpe-big" id="sharpeValue">-</div>
          <div class="sharpe-label">Sharpe Ratio (Annualized)</div>
        </div>
        <div>
          <div class="sharpe-big" id="overallWinRate" style="font-size:28px;">-</div>
          <div class="sharpe-label">Overall Win Rate</div>
        </div>
      </div>
      <canvas id="sharpeChart" width="800" height="200" style="width:100%;height:200px;"></canvas>
    </div>
    <div class="empty-state" id="edgeStatsEmpty" style="display:none;">Not enough resolved edges yet. Data builds over time as events complete.</div>
  </div>

  <!-- ALERTS TAB -->
  <div class="tab-content" id="tabAlerts">
    <div style="max-width:560px;">
      <h2 style="font-size:18px;font-weight:500;margin-bottom:24px;">Alert Settings</h2>
      <div class="alert-form">
        <label class="alert-toggle">
          <input type="checkbox" id="alertEnabled" style="accent-color:var(--text);">
          Enable Alerts
        </label>
        <div class="field">
          <label>Minimum Edge % (only alert above this)</label>
          <input type="number" id="alertMinEdge" value="5" min="1" max="50" step="0.5">
        </div>
        <div class="field">
          <label>Telegram Bot Token</label>
          <input type="text" id="alertTgToken" placeholder="123456:ABC-DEF...">
        </div>
        <div class="field">
          <label>Telegram Chat ID</label>
          <input type="text" id="alertTgChat" placeholder="e.g. -1001234567890">
        </div>
        <div class="field">
          <label>Webhook URL (optional, e.g. Slack/Discord)</label>
          <input type="text" id="alertWebhook" placeholder="https://hooks.slack.com/...">
        </div>
        <div style="display:flex;gap:8px;margin-top:24px;">
          <button class="btn-primary" onclick="saveAlerts()">Save Alerts</button>
          <button class="btn-sm" onclick="testAlert()">Test Alert</button>
        </div>
        <div id="alertStatus" style="margin-top:12px;font-size:12px;color:var(--muted);"></div>
      </div>
    </div>
  </div>

  <!-- HISTORY TAB (Pro only) -->
  <div class="tab-content" id="tabHistory">
    <div id="historyGate" style="display:none;">
      <div class="empty-state">
        <div style="font-size:14px;font-weight:500;margin-bottom:8px;">Historical data is a Pro feature</div>
        <div style="color:var(--muted);font-size:13px;margin-bottom:16px;">Unlock 1 year of resolved market data, price snapshots, and performance tracking.</div>
        <button class="btn-upgrade" style="display:inline-block;" onclick="openUpgrade()">Upgrade to Pro</button>
      </div>
    </div>
    <div id="historyContent" style="display:none;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
        <div style="font-size:14px;font-weight:500;text-transform:uppercase;letter-spacing:0.04em;">Resolved Markets</div>
        <select id="historySportFilter" onchange="loadHistory()" style="border:none;border-bottom:1px solid var(--border);background:transparent;font-size:12px;padding:4px 8px;font-family:inherit;">
          <option value="">All Sports</option>
        </select>
      </div>
      <div class="history-table-wrap">
        <table class="history-table">
          <thead>
            <tr>
              <th>Event</th>
              <th>Outcome</th>
              <th>Final Price</th>
              <th>Volume</th>
              <th>Resolution</th>
              <th>Date</th>
            </tr>
          </thead>
          <tbody id="historyBody"></tbody>
        </table>
      </div>
      <div class="empty-state" id="historyEmpty" style="display:none;">No historical data available yet. Data accumulates over time.</div>
      <div style="margin-top:32px;">
        <div style="font-size:14px;font-weight:500;text-transform:uppercase;letter-spacing:0.04em;margin-bottom:16px;">Recent Price Snapshots</div>
        <div class="history-table-wrap">
          <table class="history-table">
            <thead>
              <tr>
                <th>Event</th>
                <th>Outcome</th>
                <th>Book</th>
                <th>Poly</th>
                <th>Kalshi</th>
                <th>Divergence</th>
                <th>Time</th>
              </tr>
            </thead>
            <tbody id="snapshotBody"></tbody>
          </table>
        </div>
        <div class="empty-state" id="snapshotEmpty" style="display:none;">No snapshots yet. Price data is captured every update cycle.</div>
      </div>
    </div>
  </div>

</div>

<!-- ===== GLOSSARY MODAL ===== -->
<div class="modal-overlay" id="glossaryModal">
  <div class="modal">
    <button class="modal-close" onclick="closeModal('glossaryModal')">&times;</button>
    <h2>How It Works &mdash; Glossary</h2>
    <dl>
      <div class="glossary-item"><dt>Edge %</dt><dd>The percentage difference between the sportsbook consensus probability and the Polymarket price. A positive edge means Polymarket is cheaper.</dd></div>
      <div class="glossary-item"><dt>Confidence Score (1-5)</dt><dd>How confident the system is in this signal, based on book agreement, volume, spread, and time to event.</dd></div>
      <div class="glossary-item"><dt>Book Agreement (1-5)</dt><dd>How closely the sportsbooks agree with each other. 5 = near-unanimous consensus.</dd></div>
      <div class="glossary-item"><dt>Sharp Book</dt><dd>The bookmaker with the sharpest (most accurate) odds, weighted by market reputation.</dd></div>
      <div class="glossary-item"><dt>Implied Vig</dt><dd>The bookmaker margin (overround) baked into the odds. Lower vig = cleaner probability estimate.</dd></div>
      <div class="glossary-item"><dt>True Prob (No Vig)</dt><dd>The de-vigged consensus probability &mdash; the market's best guess at the true probability.</dd></div>
      <div class="glossary-item"><dt>Book Range</dt><dd>The spread between the highest and lowest bookmaker probabilities for an outcome.</dd></div>
      <div class="glossary-item"><dt>Median Book Prob</dt><dd>The median probability across all bookmakers, more robust to outliers than the mean.</dd></div>
      <div class="glossary-item"><dt>Kelly Bet Size</dt><dd>Suggested bet size as % of bankroll using the Kelly Criterion, based on edge and probability.</dd></div>
      <div class="glossary-item"><dt>Vol/Liquidity Ratio</dt><dd>Polymarket volume divided by liquidity. Higher ratios indicate more active trading relative to available depth.</dd></div>
      <div class="glossary-item"><dt>Spread %</dt><dd>The bid-ask spread on Polymarket as a percentage. Tighter spreads mean lower execution cost.</dd></div>
      <div class="glossary-item"><dt>Edge Direction</dt><dd>BUY if Polymarket is underpriced vs books; SELL if overpriced.</dd></div>
      <div class="glossary-item"><dt>Time to Event</dt><dd>Hours until the event starts. Edges closer to event time are more actionable.</dd></div>
      <div class="glossary-item"><dt>Best/Worst Odds</dt><dd>The best and worst decimal odds offered across bookmakers for the top outcome.</dd></div>
      <div class="glossary-item"><dt>Match Confidence</dt><dd>How well the Polymarket question matched to the sportsbook event (string matching score).</dd></div>
      <div class="glossary-item"><dt>Kalshi Price</dt><dd>The Kalshi prediction market mid-price for an outcome. Kalshi is a US-regulated prediction exchange (CFTC). Comparing both Polymarket and Kalshi gives you two independent market signals.</dd></div>
    </dl>
  </div>
</div>

<!-- ===== CUSTOMIZE MODAL ===== -->
<div class="modal-overlay" id="customizeModal">
  <div class="modal">
    <button class="modal-close" onclick="closeModal('customizeModal')">&times;</button>
    <h2>Customize Dashboard</h2>
    <div class="customize-section">
      <h3>Widgets</h3>
      <label class="customize-check"><input type="checkbox" data-widget="hero" checked> Hero Banner</label>
      <label class="customize-check"><input type="checkbox" data-widget="top_opps" checked> Top Opportunities</label>
      <label class="customize-check"><input type="checkbox" data-widget="stats" checked> Stats Row</label>
      <label class="customize-check"><input type="checkbox" data-widget="events" checked> Event Cards</label>
    </div>
    <div class="customize-section">
      <h3>Card Data Points</h3>
      <label class="customize-check"><input type="checkbox" data-dp="volume" checked> Volume Traded</label>
      <label class="customize-check"><input type="checkbox" data-dp="spread" checked> Bid-Ask Spread</label>
      <label class="customize-check"><input type="checkbox" data-dp="bookmakers" checked> Bookmakers</label>
      <label class="customize-check"><input type="checkbox" data-dp="sharp_book" checked> Sharp Book</label>
      <label class="customize-check"><input type="checkbox" data-dp="price_change" checked> 24h Price Change</label>
      <label class="customize-check"><input type="checkbox" data-dp="match_confidence" checked> Match Confidence</label>
      <label class="customize-check"><input type="checkbox" data-dp="book_agreement"> Book Agreement</label>
      <label class="customize-check"><input type="checkbox" data-dp="book_range"> Book Range</label>
      <label class="customize-check"><input type="checkbox" data-dp="median_book_prob"> Median Book Prob</label>
      <label class="customize-check"><input type="checkbox" data-dp="book_std_dev"> Book Std Dev</label>
      <label class="customize-check"><input type="checkbox" data-dp="implied_vig"> Implied Vig</label>
      <label class="customize-check"><input type="checkbox" data-dp="true_prob_no_vig"> True Prob (No Vig)</label>
      <label class="customize-check"><input type="checkbox" data-dp="best_odds"> Best Odds</label>
      <label class="customize-check"><input type="checkbox" data-dp="worst_odds"> Worst Odds</label>
      <label class="customize-check"><input type="checkbox" data-dp="vol_liquidity_ratio"> Vol/Liquidity Ratio</label>
      <label class="customize-check"><input type="checkbox" data-dp="spread_pct"> Spread %</label>
      <label class="customize-check"><input type="checkbox" data-dp="edge_direction"> Edge Direction</label>
      <label class="customize-check"><input type="checkbox" data-dp="time_to_event"> Time to Event</label>
    </div>
    <div class="customize-section">
      <label class="customize-check"><input type="checkbox" id="expandDefault"> Expand cards by default</label>
    </div>
    <button class="btn-primary" onclick="saveLayout()" style="width:100%;">Save Preferences</button>
  </div>
</div>

<!-- ===== UPGRADE MODAL ===== -->
<div class="modal-overlay" id="upgradeModal">
  <div class="modal">
    <button class="modal-close" onclick="closeModal('upgradeModal')">&times;</button>
    <div class="upgrade-content">
      <h2>Sharpe Pro</h2>
      <div class="upgrade-price">$24.99<span>/mo</span></div>
      <p style="color:var(--text-secondary);font-size:14px;margin-bottom:20px;">Unlock the full power of market intelligence.</p>
      <div class="upgrade-features">
        <div class="upgrade-row"><span>Feature</span><span style="display:flex;gap:40px;"><span>Free</span><span>Pro</span></span></div>
        <div class="upgrade-row"><span>Live edge signals</span><span style="display:flex;gap:40px;"><span>3 cards</span><span class="upgrade-check">Unlimited</span></span></div>
        <div class="upgrade-row"><span>Confidence scores</span><span style="display:flex;gap:40px;"><span class="upgrade-cross">&#x2717;</span><span class="upgrade-check">&#x2713;</span></span></div>
        <div class="upgrade-row"><span>Profit tracker</span><span style="display:flex;gap:40px;"><span class="upgrade-cross">&#x2717;</span><span class="upgrade-check">&#x2713;</span></span></div>
        <div class="upgrade-row"><span>Watchlist</span><span style="display:flex;gap:40px;"><span class="upgrade-cross">&#x2717;</span><span class="upgrade-check">&#x2713;</span></span></div>
        <div class="upgrade-row"><span>Advanced data points</span><span style="display:flex;gap:40px;"><span class="upgrade-cross">&#x2717;</span><span class="upgrade-check">&#x2713;</span></span></div>
        <div class="upgrade-row"><span>Custom layout</span><span style="display:flex;gap:40px;"><span class="upgrade-cross">&#x2717;</span><span class="upgrade-check">&#x2713;</span></span></div>
      </div>
      <a href="mailto:support@sharpe.app?subject=Upgrade%20to%20Pro" class="btn-primary" style="display:inline-block;margin-top:16px;text-decoration:none;">Contact Us to Upgrade</a>
    </div>
  </div>
</div>

<!-- ===== TRADE MODAL ===== -->
<div class="modal-overlay" id="tradeModal">
  <div class="modal">
    <button class="modal-close" onclick="closeModal('tradeModal')">&times;</button>
    <h2>Log Trade</h2>
    <div class="trade-form">
      <div><label>Market</label><input type="text" id="tradeMarket" placeholder="e.g. Lakers vs Celtics"></div>
      <div><label>Outcome</label><input type="text" id="tradeOutcome" placeholder="e.g. Lakers Win"></div>
      <div><label>Entry Price (cents)</label><input type="number" id="tradePrice" min="1" max="99" placeholder="e.g. 45"></div>
      <div><label>Amount ($)</label><input type="number" id="tradeAmount" min="1" placeholder="e.g. 100"></div>
      <button class="btn-primary" onclick="submitTrade()">Log Trade</button>
    </div>
  </div>
</div>

<script>
/* ===== Constants ===== */
const POLL = """ + str(POLL_INTERVAL) + """;
const THRESH = __USER_THRESHOLD__;
const DEFAULT_THRESH = """ + str(DIVERGENCE_THRESHOLD) + """;
const USER_SPORT = '__USER_SPORT__';
const USERNAME = '__USERNAME__';
let data = null, sports = {}, activeSport = '', signalsOnly = false;
let refreshCountdown = POLL, ws = null;
let userLayout = { visible_widgets: ['hero','top_opps','stats','events'], visible_data_points: ['volume','spread','bookmakers','sharp_book','price_change','match_confidence'], card_expanded_default: false };
let userTier = 'free';
let watchlistIds = new Set();

/* ===== Utilities ===== */
function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
function fmt(n, d) { return n != null ? Number(n).toFixed(d === undefined ? 1 : d) : '-'; }
function fmtPct(n) { return n != null ? (n >= 0 ? '+' : '') + Number(n).toFixed(1) + '%' : '-'; }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }
function openModal(id) { document.getElementById(id).classList.add('open'); }

/* ===== Confidence Stars ===== */
function renderConfidenceStars(score) {
  const s = Math.round(Number(score) || 0);
  let h = '<span class="confidence-stars">';
  for (let i = 1; i <= 5; i++) h += i <= s ? '<span class="star-filled">&#9733;</span>' : '<span class="star-empty">&#9734;</span>';
  return h + '</span>';
}

/* ===== WebSocket ===== */
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/ws');
  ws.onopen = () => {
    document.getElementById('statusText').textContent = 'Live';
    document.getElementById('statusDot').classList.remove('error');
  };
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === 'update') {
        // Always update cross-sport edges even if sport doesn't match
        if (msg.data.cross_sport_edges) {
          data = data || {};
          data.cross_sport_edges = msg.data.cross_sport_edges;
        }
        if (msg.data.active_sport && msg.data.active_sport !== activeSport) {
          renderCrossSportTicker();
          return;
        }
        data = msg.data;
        refreshCountdown = POLL;
        render();
      }
    } catch(err) { console.warn('WS parse error:', err); }
  };
  ws.onclose = (e) => {
    if (e.code === 4001) { window.location.href = '/login'; return; }
    document.getElementById('statusText').textContent = 'Reconnecting...';
    document.getElementById('statusDot').classList.add('error');
    setTimeout(connectWS, 5000);
  };
  ws.onerror = () => ws.close();
}

/* ===== Load Sports ===== */
let sportCategories = [];
let activeCategory = 'Sports';

function loadSports() {
  fetch('/api/sports', { credentials: 'same-origin' }).then(r => r.json()).then(resp => {
    sportCategories = resp.categories || [];
    sports = {};
    sportCategories.forEach(cat => {
      cat.sports.forEach(s => { sports[s.key] = s; });
    });
    // Determine active category from active sport
    if (activeSport) {
      for (const cat of sportCategories) {
        if (cat.sports.some(s => s.key === activeSport)) {
          activeCategory = cat.name;
          break;
        }
      }
    }
    renderSportTabs();
  }).catch(() => {});
}

function renderSportTabs() {
  const wrap = document.getElementById('sportTabs');
  wrap.innerHTML = '';
  // Category header row
  const catRow = document.createElement('div');
  catRow.className = 'category-row';
  sportCategories.forEach(cat => {
    const btn = document.createElement('button');
    btn.className = 'category-btn' + (cat.name === activeCategory ? ' active' : '');
    btn.textContent = cat.name;
    btn.onclick = () => {
      activeCategory = cat.name;
      // Switch to first sport in this category
      if (cat.sports.length > 0) {
        switchSport(cat.sports[0].key);
      }
      renderSportTabs();
    };
    catRow.appendChild(btn);
  });
  wrap.appendChild(catRow);
  // Subcategory row for active category
  const activeCat = sportCategories.find(c => c.name === activeCategory);
  if (activeCat) {
    const subRow = document.createElement('div');
    subRow.className = 'subcategory-row';
    activeCat.sports.forEach(s => {
      const btn = document.createElement('button');
      btn.className = 'sport-tab' + (s.key === activeSport ? ' active' : '');
      btn.textContent = s.title;
      btn.onclick = () => switchSport(s.key);
      subRow.appendChild(btn);
    });
    wrap.appendChild(subRow);
  }
}

function switchSport(key) {
  activeSport = key;
  // Update active category
  for (const cat of sportCategories) {
    if (cat.sports.some(s => s.key === key)) {
      activeCategory = cat.name;
      break;
    }
  }
  renderSportTabs();
  fetch('/api/data?sport=' + encodeURIComponent(key), { credentials: 'same-origin' })
    .then(r => r.json()).then(d => { data = d; render(); }).catch(() => {});
}

/* ===== Load Layout ===== */
function loadLayout() {
  fetch('/api/layout', { credentials: 'same-origin' }).then(r => r.ok ? r.json() : null).then(d => {
    if (d) {
      if (d.visible_widgets) userLayout.visible_widgets = d.visible_widgets;
      if (d.visible_data_points) userLayout.visible_data_points = d.visible_data_points;
      if (d.card_expanded_default !== undefined) userLayout.card_expanded_default = d.card_expanded_default;
      applyLayoutToUI();
    }
  }).catch(() => {});
}

function applyLayoutToUI() {
  document.querySelectorAll('[data-widget]').forEach(el => {
    el.querySelector('input').checked = userLayout.visible_widgets.includes(el.querySelector('input').dataset.widget);
  });
  document.querySelectorAll('[data-dp]').forEach(el => {
    el.querySelector('input').checked = userLayout.visible_data_points.includes(el.querySelector('input').dataset.dp);
  });
  document.getElementById('expandDefault').checked = userLayout.card_expanded_default;
}

function saveLayout() {
  const widgets = [];
  document.querySelectorAll('[data-widget] input:checked').forEach(el => widgets.push(el.dataset.widget));
  const dps = [];
  document.querySelectorAll('[data-dp] input:checked').forEach(el => dps.push(el.dataset.dp));
  const expanded = document.getElementById('expandDefault').checked;
  userLayout = { visible_widgets: widgets, visible_data_points: dps, card_expanded_default: expanded };
  fetch('/api/layout', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(userLayout)
  }).then(() => { closeModal('customizeModal'); render(); }).catch(() => {});
}

/* ===== Load Subscription ===== */
function loadSubscription() {
  fetch('/api/subscription', { credentials: 'same-origin' }).then(r => r.ok ? r.json() : null).then(d => {
    if (d && d.tier) {
      userTier = d.tier;
      if (userTier === 'free') {
        document.getElementById('btnUpgradeNav').style.display = 'inline-block';
      }
    }
  }).catch(() => {});
}

/* ===== Load User Info ===== */
function loadUserInfo() {
  fetch('/api/me', { credentials: 'same-origin' }).then(r => r.ok ? r.json() : null).then(d => {
    if (d && d.is_admin) {
      document.getElementById('adminLink').style.display = 'inline-block';
    }
  }).catch(() => {});
}

/* ===== Main Render ===== */
function render() {
  if (!data) return;
  const comps = data.comparisons || [];

  // Stats
  document.getElementById('statScanned').textContent = data.odds_events_count || '-';
  document.getElementById('statPolyListed').textContent = data.poly_events_count || '-';
  document.getElementById('statKalshi').textContent = data.kalshi_markets_count || '-';
  document.getElementById('statMatched').textContent = comps.length;
  const opps = comps.filter(c => c.outcomes && c.outcomes.some(o => Math.abs(o.divergence_pct || 0) >= THRESH));
  document.getElementById('statOpps').textContent = opps.length;
  const edges = comps.flatMap(c => (c.outcomes || []).map(o => Math.abs(o.divergence_pct || 0)));
  document.getElementById('statBestEdge').textContent = edges.length ? fmt(Math.max(...edges)) + '%' : '-';
  document.getElementById('lastUpdate').textContent = data.last_update ? 'Updated ' + new Date(data.last_update).toLocaleTimeString() : '';

  // Top Opportunities
  renderTopOpps(comps);

  // Cards
  renderCards(comps);

  // Widget visibility
  document.getElementById('heroBanner').style.display = userLayout.visible_widgets.includes('hero') && !localStorage.getItem('sharpe_hero_dismissed') ? '' : 'none';
  document.getElementById('topOpps').style.display = userLayout.visible_widgets.includes('top_opps') ? '' : 'none';
  document.querySelector('.stats-row').style.display = userLayout.visible_widgets.includes('stats') ? '' : 'none';

  // Cross-sport ticker
  renderCrossSportTicker();

  // Draw sparklines after DOM is updated
  requestAnimationFrame(drawSparklines);
}

/* ===== Top Opportunities ===== */
function renderTopOpps(comps) {
  const wrap = document.getElementById('topOpps');
  const sorted = comps.filter(c => c.outcomes && c.outcomes.some(o => (o.divergence_pct || 0) >= THRESH))
    .map(c => {
      const best = c.outcomes.reduce((a, b) => (Math.abs(b.divergence_pct||0) > Math.abs(a.divergence_pct||0)) ? b : a, c.outcomes[0]);
      return { ...c, _bestEdge: Math.abs(best.divergence_pct || 0), _bestOutcome: best };
    })
    .sort((a, b) => b._bestEdge - a._bestEdge)
    .slice(0, 3);

  if (!sorted.length) { wrap.innerHTML = ''; return; }
  wrap.innerHTML = sorted.map((c, i) => {
    const dir = (c.edge_direction || (c._bestOutcome.divergence_pct > 0 ? 'BUY' : 'SELL'));
    return '<div class="top-opp" onclick="scrollToCard(' + i + ')">' +
      '<div class="top-opp-header"><span class="top-opp-edge">' + fmtPct(c._bestEdge) + '</span>' + renderConfidenceStars(c.confidence_score) + '</div>' +
      '<div class="top-opp-team">' + esc(c.home_team || '') + ' vs ' + esc(c.away_team || '') + '</div>' +
      '<div class="top-opp-sub">' + esc(c._bestOutcome.outcome_name || '') + ' &mdash; ' + dir + '</div>' +
      '</div>';
  }).join('');
}

function scrollToCard(idx) {
  const cards = document.querySelectorAll('.card');
  if (cards[idx]) cards[idx].scrollIntoView({ behavior: 'smooth', block: 'center' });
}

/* ===== Render Cards ===== */
function renderCards(comps) {
  const grid = document.getElementById('cardsGrid');
  const search = (document.getElementById('searchInput').value || '').toLowerCase();
  const sortBy = document.getElementById('sortSelect').value;

  const marketFilter = document.getElementById('marketTypeFilter').value;
  let filtered = comps.filter(c => {
    if (signalsOnly && !(c.outcomes || []).some(o => Math.abs(o.divergence_pct || 0) >= THRESH)) return false;
    if (marketFilter !== 'all' && (c.market_type || 'h2h') !== marketFilter) return false;
    if (search) {
      const hay = ((c.home_team || '') + ' ' + (c.away_team || '')).toLowerCase();
      if (!hay.includes(search)) return false;
    }
    return true;
  });

  filtered.sort((a, b) => {
    const eA = Math.max(...(a.outcomes||[]).map(o => Math.abs(o.divergence_pct||0)), 0);
    const eB = Math.max(...(b.outcomes||[]).map(o => Math.abs(o.divergence_pct||0)), 0);
    if (sortBy === 'edge') return eB - eA;
    if (sortBy === 'confidence') return (b.confidence_score||0) - (a.confidence_score||0);
    if (sortBy === 'time') return (a.time_to_event_hours||9999) - (b.time_to_event_hours||9999);
    if (sortBy === 'volume') return (b.poly_volume||0) - (a.poly_volume||0);
    if (sortBy === 'kelly') {
      const kA = Math.max(...(a.outcomes||[]).map(o => Math.abs(o.kelly_fraction||0)), 0);
      const kB = Math.max(...(b.outcomes||[]).map(o => Math.abs(o.kelly_fraction||0)), 0);
      return kB - kA;
    }
    if (sortBy === 'book_agreement') return (b.book_agreement||0) - (a.book_agreement||0);
    return eB - eA;
  });

  if (!filtered.length) { grid.innerHTML = '<div class="empty-state">No events match your filters.</div>'; return; }

  const FREE_LIMIT = 3;
  let html = '';
  filtered.forEach((c, idx) => {
    const isFreeGated = userTier === 'free' && idx >= FREE_LIMIT;
    const bestOutcome = (c.outcomes||[]).reduce((a, b) => Math.abs(b.divergence_pct||0) > Math.abs(a.divergence_pct||0) ? b : a, (c.outcomes||[])[0] || {});
    const isSignal = (c.outcomes||[]).some(o => Math.abs(o.divergence_pct||0) >= THRESH);
    const dir = c.edge_direction || (bestOutcome.divergence_pct > 0 ? 'BUY' : 'SELL');
    const badgeCls = isSignal ? (dir === 'BUY' ? 'buy' : 'sell') : 'neutral';
    const expanded = userLayout.card_expanded_default && !isFreeGated;
    const cardId = 'card-' + idx;

    if (isFreeGated && idx === FREE_LIMIT) {
      html += '<div class="gate-overlay"><div class="gate-blur">';
    }

    html += '<div class="card" id="' + cardId + '">';
    // Header
    html += '<div class="card-header" onclick="toggleCard(\'' + cardId + '\')">';
    html += '<div class="card-teams">' + esc(c.home_team || 'Unknown') + '<span class="vs">vs</span>' + esc(c.away_team || '');
    const mtype = c.market_type || 'h2h';
    if (mtype !== 'h2h') html += '<span class="market-type-badge ' + mtype + '">' + mtype + '</span>';
    html += '</div>';
    html += '<div class="card-meta">';
    if (c.time_to_event_hours != null) html += '<span class="card-time">' + (c.time_to_event_hours < 1 ? '<1h' : Math.round(c.time_to_event_hours) + 'h') + '</span>';
    html += '<span class="signal-badge ' + badgeCls + '">' + (isSignal ? dir + ' ' + fmt(Math.abs(bestOutcome.divergence_pct || 0)) + '%' : 'No Edge') + '</span>';
    // Trend arrow
    const bestTrend = bestOutcome.trend || {};
    const trendDir = bestTrend.direction || 'new';
    const trendIcon = trendDir === 'widening' ? '&#9650;' : trendDir === 'narrowing' ? '&#9660;' : trendDir === 'stable' ? '&#8212;' : '&#9679;';
    html += '<span class="trend-arrow ' + trendDir + '" title="Edge ' + trendDir + '">' + trendIcon + '</span>';
    if (bestTrend.change_2h) html += '<span class="trend-change">' + (bestTrend.change_2h > 0 ? '+' : '') + fmt(bestTrend.change_2h, 1) + '/2h</span>';
    html += renderConfidenceStars(c.confidence_score);
    html += '</div></div>';

    // Action hint
    if (isSignal && bestOutcome.outcome_name) {
      const polyPrice = bestOutcome.poly_price != null ? Math.round(bestOutcome.poly_price * 100) : null;
      const kalshiPrice = bestOutcome.kalshi_prob != null ? Math.round(bestOutcome.kalshi_prob) : null;
      let hintPlatforms = '';
      if (polyPrice != null && kalshiPrice != null) hintPlatforms = 'Polymarket (' + polyPrice + 'c) / Kalshi (' + kalshiPrice + 'c)';
      else if (polyPrice != null) hintPlatforms = 'Polymarket (' + polyPrice + 'c)';
      else if (kalshiPrice != null) hintPlatforms = 'Kalshi (' + kalshiPrice + 'c)';
      html += '<div class="card-action-hint">' + esc(bestOutcome.outcome_name) + ' is ' + fmt(Math.abs(bestOutcome.divergence_pct||0)) + '% cheaper on ' + hintPlatforms + ' &mdash; ' + dir + '</div>';
    }

    // Outcome chips
    if (c.outcomes && c.outcomes.length) {
      html += '<div class="outcome-chips">';
      c.outcomes.forEach((o, oi) => {
        const d = o.divergence_pct || 0;
        const cls = Math.abs(d) >= THRESH ? (d > 0 ? 'pos' : 'neg') : '';
        const t = o.trend || {};
        const sparkId = cardId + '-spark-' + oi;
        html += '<span class="outcome-chip ' + cls + '">' + esc(o.outcome_name || '?') + ' ' + fmtPct(d);
        if (t.points && t.points.length > 2) html += '<span class="sparkline"><canvas id="' + sparkId + '" width="40" height="16"></canvas></span>';
        html += '</span>';
      });
      html += '</div>';
    }

    // Detail
    html += '<div class="card-detail' + (expanded ? ' open' : '') + '" id="detail-' + cardId + '">';

    // Probability comparison bars
    if (c.outcomes && c.outcomes.length) {
      html += '<div class="prob-compare">';
      html += '<div class="prob-legend"><span class="leg-book">Book Consensus</span><span class="leg-poly">Polymarket</span><span class="leg-kalshi">Kalshi</span></div>';
      c.outcomes.forEach(o => {
        const bp = (o.sharp_prob || o.consensus_prob || 0) * 100;
        const pp = (o.poly_price || 0) * 100;
        const kp = o.kalshi_prob || 0;
        html += '<div class="prob-row"><div class="prob-label">' + esc(o.outcome_name || '') + '</div><div class="prob-bars">' +
          '<div class="prob-bar book" style="width:' + bp + '%"></div>' +
          '<div class="prob-bar poly" style="width:' + pp + '%"></div>' +
          (kp > 0 ? '<div class="prob-bar kalshi" style="width:' + kp + '%"></div>' : '') +
          '</div><span style="font-size:11px;color:var(--muted);white-space:nowrap">' + fmt(bp,0) + '% / ' + fmt(pp,0) + '%' + (kp > 0 ? ' / ' + fmt(kp,0) + '%' : '') + '</span></div>';
      });
      html += '</div>';
    }

    // Market Intel Grid
    html += buildIntelGrid(c);

    // Outcome Table
    if (c.outcomes && c.outcomes.length) {
      html += '<div class="outcome-table-wrap"><table class="outcome-table"><thead><tr><th>Outcome</th><th>Sharp%</th><th>Consensus%</th><th>Poly%</th><th>Kalshi%</th><th>Edge</th><th>Cheaper On</th><th>Bet Size</th></tr></thead><tbody>';
      c.outcomes.forEach(o => {
        const d = o.divergence_pct || 0;
        const col = Math.abs(d) >= THRESH ? (d > 0 ? 'var(--green)' : 'var(--red)') : 'var(--text)';
        const kp = o.kalshi_prob;
        html += '<tr><td>' + esc(o.outcome_name || '') + '</td><td>' + fmt((o.sharp_prob||0)*100,1) + '%</td><td>' + fmt((o.consensus_prob||0)*100,1) + '%</td><td>' + fmt((o.poly_price||0)*100,1) + '%</td><td style="color:var(--text)">' + (kp != null ? fmt(kp,1) + '%' : '-') + '</td><td style="color:' + col + ';font-weight:600">' + fmtPct(d) + '</td><td>' + (d > 0 ? 'Poly' : d < 0 ? 'Books' : '-') + '</td><td>' + (o.kelly_fraction != null ? fmt(o.kelly_fraction * 100, 1) + '%' : '-') + '</td></tr>';
      });
      html += '</tbody></table></div>';
    }

    // Bookmaker Breakdown
    if (c.bookmaker_breakdown && Object.keys(c.bookmaker_breakdown).length) {
      html += '<button class="bookie-toggle" onclick="event.stopPropagation();this.nextElementSibling.classList.toggle(\'open\')">&#9660; Bookmaker Breakdown (' + Object.keys(c.bookmaker_breakdown).length + ')</button>';
      html += '<div class="bookie-section"><table class="bookie-table"><thead><tr><th>Book</th><th>Outcome</th><th>Prob</th><th>Odds</th></tr></thead><tbody>';
      Object.entries(c.bookmaker_breakdown).forEach(([bk, outcomes]) => {
        (Array.isArray(outcomes) ? outcomes : []).forEach((o, oi) => {
          html += '<tr><td>' + (oi === 0 ? esc(bk) : '') + '</td><td>' + esc(o.outcome || '') + '</td><td>' + fmt((o.implied_prob||0)*100,1) + '%</td><td>' + fmt(o.decimal_odds, 2) + '</td></tr>';
        });
      });
      html += '</tbody></table></div>';
    }

    // Card actions
    const wKey = esc((c.home_team||'') + ' vs ' + (c.away_team||''));
    html += '<div class="card-actions">';
    if (c.poly_url) html += '<a href="' + esc(c.poly_url) + '" target="_blank" class="btn-action primary">Trade on Polymarket</a>';
    if (c.kalshi_event) html += '<a href="https://kalshi.com/events/' + esc(c.kalshi_event) + '" target="_blank" class="btn-action kalshi-btn">Trade on Kalshi</a>';
    html += '<button class="btn-action btn-watchlist' + (watchlistIds.has(wKey) ? ' active' : '') + '" onclick="event.stopPropagation();toggleWatchlist(\'' + wKey.replace(/'/g,"\\'") + '\',\'' + esc(c.home_team||'').replace(/'/g,"\\'") + '\',\'' + esc(c.away_team||'').replace(/'/g,"\\'") + '\')">&#9733; Watchlist</button>';
    html += '<button class="btn-action" onclick="event.stopPropagation();logTrade(\'' + wKey.replace(/'/g,"\\'") + '\',\'' + esc((bestOutcome.outcome_name||'')).replace(/'/g,"\\'") + '\',' + Math.round((bestOutcome.poly_price||0)*100) + ')">Log Trade</button>';
    html += '<button class="btn-flag" onclick="event.stopPropagation();flagMatch(\'' + esc(c.home_team||'').replace(/'/g,"\\'") + '\',\'' + esc(c.away_team||'').replace(/'/g,"\\'") + '\',\'' + esc(c.poly_question||'').replace(/'/g,"\\'") + '\')" title="Report bad match">&#9873; Flag</button>';
    html += '</div>';

    html += '</div>'; // card-detail
    html += '</div>'; // card
  });

  // Close gate overlay
  if (userTier === 'free' && filtered.length > FREE_LIMIT) {
    html += '</div>'; // gate-blur
    html += '<div class="gate-cta"><h3>Upgrade to Pro to see all ' + filtered.length + ' edges</h3><button class="btn-upgrade-big" onclick="openUpgrade()">Unlock All Signals</button></div>';
    html += '</div>'; // gate-overlay
  }

  grid.innerHTML = html;
}

/* ===== Intel Grid Builder ===== */
function buildIntelGrid(c) {
  const dp = userLayout.visible_data_points;
  const items = [];
  const tooltips = {
    volume: 'Total volume traded on Polymarket for this event.',
    spread: 'Bid-ask spread on Polymarket. Lower = tighter market.',
    bookmakers: 'Number of sportsbooks quoting this event.',
    sharp_book: 'The sharpest bookmaker with most accurate odds.',
    price_change: 'Polymarket price change in the last 24 hours.',
    match_confidence: 'How well the Polymarket market matched to book event.',
    book_agreement: 'How closely books agree (1-5). Higher = stronger consensus.',
    book_range: 'Spread between highest and lowest book probabilities.',
    median_book_prob: 'Median probability across all bookmakers.',
    book_std_dev: 'Standard deviation of book probabilities. Lower = tighter.',
    implied_vig: 'Bookmaker margin (overround) in the odds.',
    true_prob_no_vig: 'De-vigged true probability estimate.',
    best_odds: 'Best decimal odds offered across bookmakers.',
    worst_odds: 'Worst decimal odds offered across bookmakers.',
    vol_liquidity_ratio: 'Volume / Liquidity. Higher = more actively traded.',
    spread_pct: 'Polymarket spread as a percentage.',
    edge_direction: 'BUY if Polymarket underpriced, SELL if overpriced.',
    time_to_event: 'Hours until event starts.',
    kalshi_price: 'Kalshi prediction market implied probability for the best outcome.',
    kalshi_volume: 'Total contracts traded on Kalshi for this game.'
  };

  if (dp.includes('volume')) items.push({ label: 'Volume Traded', value: c.poly_volume != null ? '$' + Number(c.poly_volume).toLocaleString() : '-', key: 'volume' });
  if (dp.includes('spread')) items.push({ label: 'Bid-Ask Spread', value: c.poly_spread != null ? fmt(c.poly_spread, 2) : '-', key: 'spread' });
  if (dp.includes('bookmakers')) items.push({ label: 'Bookmakers', value: c.num_bookmakers || '-', key: 'bookmakers' });
  if (dp.includes('sharp_book')) items.push({ label: 'Sharp Book', value: c.sharp_book || '-', key: 'sharp_book' });
  if (dp.includes('price_change')) items.push({ label: '24h Change', value: c.poly_one_day_change != null ? fmtPct(c.poly_one_day_change) : '-', key: 'price_change' });
  if (dp.includes('match_confidence')) items.push({ label: 'Match Confidence', value: c.match_score != null ? fmt(c.match_score, 0) + '%' : '-', key: 'match_confidence' });
  if (dp.includes('book_agreement')) items.push({ label: 'Book Agreement', value: c.book_agreement != null ? renderConfidenceStars(c.book_agreement) : '-', key: 'book_agreement' });
  if (dp.includes('book_range')) items.push({ label: 'Book Range', value: c.book_range != null ? fmt(c.book_range * 100, 1) + '%' : '-', key: 'book_range' });
  if (dp.includes('median_book_prob')) items.push({ label: 'Median Book Prob', value: c.median_book_prob != null ? fmt(c.median_book_prob * 100, 1) + '%' : '-', key: 'median_book_prob' });
  if (dp.includes('book_std_dev')) items.push({ label: 'Book Std Dev', value: c.book_std_dev != null ? fmt(c.book_std_dev * 100, 2) + '%' : '-', key: 'book_std_dev' });
  if (dp.includes('implied_vig')) items.push({ label: 'Implied Vig', value: c.implied_vig != null ? fmt(c.implied_vig * 100, 1) + '%' : '-', key: 'implied_vig' });
  if (dp.includes('true_prob_no_vig')) items.push({ label: 'True Prob (No Vig)', value: c.true_prob_no_vig != null ? fmt(c.true_prob_no_vig * 100, 1) + '%' : '-', key: 'true_prob_no_vig' });
  if (dp.includes('best_odds')) items.push({ label: 'Best Odds', value: c.best_decimal_odds != null ? fmt(c.best_decimal_odds, 2) : '-', key: 'best_odds' });
  if (dp.includes('worst_odds')) items.push({ label: 'Worst Odds', value: c.worst_decimal_odds != null ? fmt(c.worst_decimal_odds, 2) : '-', key: 'worst_odds' });
  if (dp.includes('vol_liquidity_ratio')) items.push({ label: 'Vol/Liq Ratio', value: c.volume_liquidity_ratio != null ? fmt(c.volume_liquidity_ratio, 2) : '-', key: 'vol_liquidity_ratio' });
  if (dp.includes('spread_pct')) items.push({ label: 'Spread %', value: c.spread_pct != null ? fmt(c.spread_pct, 2) + '%' : '-', key: 'spread_pct' });
  if (dp.includes('edge_direction')) items.push({ label: 'Edge Direction', value: c.edge_direction || '-', key: 'edge_direction' });
  if (dp.includes('time_to_event')) items.push({ label: 'Time to Event', value: c.time_to_event_hours != null ? (c.time_to_event_hours < 1 ? '<1h' : Math.round(c.time_to_event_hours) + 'h') : '-', key: 'time_to_event' });
  if (dp.includes('kalshi_price')) {
    const bestO = (c.outcomes||[]).reduce((a,b) => Math.abs(b.divergence_pct||0) > Math.abs(a.divergence_pct||0) ? b : a, (c.outcomes||[])[0]||{});
    items.push({ label: 'Kalshi Price', value: bestO.kalshi_prob != null ? fmt(bestO.kalshi_prob, 1) + '%' : 'N/A', key: 'kalshi_price' });
  }
  if (dp.includes('kalshi_volume')) items.push({ label: 'Kalshi Volume', value: c.kalshi_volume ? Number(c.kalshi_volume).toLocaleString() : '-', key: 'kalshi_volume' });

  if (!items.length) return '';
  let h = '<div class="intel-grid">';
  items.forEach(it => {
    h += '<div class="intel-item"><div class="intel-item-label">' + esc(it.label) + ' <span class="info-i">i<span class="tooltip">' + esc(tooltips[it.key] || '') + '</span></span></div><div class="intel-item-value">' + it.value + '</div></div>';
  });
  return h + '</div>';
}

/* ===== Toggle Card Detail ===== */
function toggleCard(id) {
  const d = document.getElementById('detail-' + id);
  if (d) d.classList.toggle('open');
}

/* ===== Tab Switching ===== */
function switchTab(tab) {
  document.querySelectorAll('.main-tab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.getElementById('tabBtn' + tab.charAt(0).toUpperCase() + tab.slice(1)).classList.add('active');

  if (tab === 'dashboard') { document.getElementById('tabDashboard').classList.add('active'); }
  else if (tab === 'profit') { document.getElementById('tabProfit').classList.add('active'); renderProfitTracker(); }
  else if (tab === 'watchlist') { document.getElementById('tabWatchlist').classList.add('active'); renderWatchlist(); }
  else if (tab === 'edgestats') { document.getElementById('tabEdgestats').classList.add('active'); loadEdgePerformance(); }
  else if (tab === 'alerts') { document.getElementById('tabAlerts').classList.add('active'); loadAlertConfig(); }
  else if (tab === 'history') { document.getElementById('tabHistory').classList.add('active'); initHistory(); }
}

/* ===== Profit Tracker ===== */
function renderProfitTracker() {
  fetch('/api/trades/stats', { credentials: 'same-origin' }).then(r => r.ok ? r.json() : {}).then(stats => {
    const wrap = document.getElementById('profitSummary');
    const pnl = stats.total_pnl || 0;
    const pnlCol = pnl >= 0 ? 'var(--green)' : 'var(--red)';
    wrap.innerHTML =
      '<div class="profit-card"><div class="profit-value" style="color:' + pnlCol + '">$' + fmt(pnl, 2) + '</div><div class="profit-label">Total P&amp;L</div></div>' +
      '<div class="profit-card"><div class="profit-value">' + fmt(stats.win_rate || 0, 0) + '%</div><div class="profit-label">Win Rate</div></div>' +
      '<div class="profit-card"><div class="profit-value">' + (stats.open_trades || 0) + '</div><div class="profit-label">Open Trades</div></div>' +
      '<div class="profit-card"><div class="profit-value">' + (stats.closed_trades || 0) + '</div><div class="profit-label">Closed Trades</div></div>' +
      '<div class="profit-card"><div class="profit-value">' + fmt(stats.roi || 0, 1) + '%</div><div class="profit-label">ROI</div></div>';
  }).catch(() => {});

  fetch('/api/trades', { credentials: 'same-origin' }).then(r => r.ok ? r.json() : []).then(trades => {
    const body = document.getElementById('tradesBody');
    const empty = document.getElementById('tradesEmpty');
    if (!trades.length) { body.innerHTML = ''; empty.style.display = ''; return; }
    empty.style.display = 'none';
    body.innerHTML = trades.map(t => {
      const pnlCol = (t.pnl||0) >= 0 ? 'var(--green)' : 'var(--red)';
      return '<tr><td>' + esc(t.market_name||'') + '</td><td>' + esc(t.outcome||'') + '</td><td>' + fmt(t.entry_price,0) + 'c</td><td>$' + fmt(t.amount,2) + '</td><td>' + esc(t.status||'open') + '</td><td style="color:' + pnlCol + '">$' + fmt(t.pnl||0,2) + '</td><td>' + (t.created_at ? new Date(t.created_at).toLocaleDateString() : '-') + '</td></tr>';
    }).join('');
  }).catch(() => {});
}

/* ===== Watchlist ===== */
function renderWatchlist() {
  fetch('/api/watchlist', { credentials: 'same-origin' }).then(r => r.ok ? r.json() : []).then(items => {
    const wrap = document.getElementById('watchlistList');
    const empty = document.getElementById('watchlistEmpty');
    watchlistIds = new Set(items.map(i => i.market_key || (i.home_team + ' vs ' + i.away_team)));
    if (!items.length) { wrap.innerHTML = ''; empty.style.display = ''; return; }
    empty.style.display = 'none';
    wrap.innerHTML = items.map(i => {
      const edge = i.current_edge != null ? fmtPct(i.current_edge) : '-';
      const edgeCol = (i.current_edge||0) >= THRESH ? 'var(--green)' : 'var(--text-secondary)';
      return '<div class="watchlist-item"><div class="watchlist-item-info"><div class="watchlist-item-name">' + esc(i.market_name || (i.home_team + ' vs ' + i.away_team)) + '</div><div class="watchlist-item-edge" style="color:' + edgeCol + '">Edge: ' + edge + '</div></div><button class="watchlist-remove" onclick="removeWatchlist(' + i.id + ')">Remove</button></div>';
    }).join('');
  }).catch(() => {});
}

function toggleWatchlist(key, home, away) {
  if (userTier === 'free') { openUpgrade(); return; }
  fetch('/api/watchlist', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ market_key: key, home_team: home, away_team: away })
  }).then(r => r.json()).then(() => { renderWatchlist(); render(); }).catch(() => {});
}

function removeWatchlist(id) {
  fetch('/api/watchlist/' + id, { method: 'DELETE', credentials: 'same-origin' })
    .then(() => renderWatchlist()).catch(() => {});
}

/* ===== History (Pro only) ===== */
let historyInitialized = false;
function initHistory() {
  const gate = document.getElementById('historyGate');
  const content = document.getElementById('historyContent');
  if (userTier !== 'pro') {
    gate.style.display = '';
    content.style.display = 'none';
    return;
  }
  gate.style.display = 'none';
  content.style.display = '';
  if (!historyInitialized) {
    // Populate sport filter dropdown
    const sel = document.getElementById('historySportFilter');
    Object.entries(sports).forEach(([k, s]) => {
      const opt = document.createElement('option');
      opt.value = k; opt.textContent = s.title || k;
      sel.appendChild(opt);
    });
    historyInitialized = true;
  }
  loadHistory();
}

function loadHistory() {
  const sport = document.getElementById('historySportFilter').value;
  const qs = sport ? '?sport=' + encodeURIComponent(sport) + '&limit=200' : '?limit=200';
  fetch('/api/history' + qs, { credentials: 'same-origin' }).then(r => {
    if (r.status === 403) { document.getElementById('historyGate').style.display = ''; document.getElementById('historyContent').style.display = 'none'; return null; }
    return r.ok ? r.json() : null;
  }).then(data => {
    if (!data) return;
    renderHistorical(data.historical || []);
    renderSnapshots(data.snapshots || []);
  }).catch(() => {});
}

function renderHistorical(rows) {
  const body = document.getElementById('historyBody');
  const empty = document.getElementById('historyEmpty');
  if (!rows.length) { body.innerHTML = ''; empty.style.display = ''; return; }
  empty.style.display = 'none';
  body.innerHTML = rows.map(r => {
    const res = (r.resolution || '').toLowerCase();
    const resCls = res === 'yes' ? 'yes' : (res === 'no' ? 'no' : '');
    const vol = r.volume ? '$' + fmt(r.volume, 0) : '-';
    const price = r.final_price != null ? fmtPct(r.final_price * 100) : '-';
    const date = r.end_date ? new Date(r.end_date).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }) : '-';
    return '<tr>' +
      '<td>' + esc(r.event_title || '') + '</td>' +
      '<td>' + esc(r.outcome || r.market_question || '') + '</td>' +
      '<td>' + price + '</td>' +
      '<td>' + vol + '</td>' +
      '<td><span class="history-res ' + resCls + '">' + esc(r.resolution || '-') + '</span></td>' +
      '<td>' + date + '</td>' +
    '</tr>';
  }).join('');
}

function renderSnapshots(rows) {
  const body = document.getElementById('snapshotBody');
  const empty = document.getElementById('snapshotEmpty');
  if (!rows.length) { body.innerHTML = ''; empty.style.display = ''; return; }
  empty.style.display = 'none';
  body.innerHTML = rows.map(r => {
    const bp = r.book_prob != null ? fmtPct(r.book_prob) : '-';
    const pp = r.poly_prob != null ? fmtPct(r.poly_prob) : '-';
    const kp = r.kalshi_prob != null ? fmtPct(r.kalshi_prob) : '-';
    const div = r.divergence != null ? fmtPct(r.divergence) : '-';
    const time = r.snapshot_at ? new Date(r.snapshot_at).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '-';
    return '<tr>' +
      '<td>' + esc(r.event_name || '') + '</td>' +
      '<td>' + esc(r.outcome || '') + '</td>' +
      '<td>' + bp + '</td>' +
      '<td>' + pp + '</td>' +
      '<td>' + kp + '</td>' +
      '<td>' + div + '</td>' +
      '<td>' + time + '</td>' +
    '</tr>';
  }).join('');
}

/* ===== Trade Logging ===== */
function logTrade(market, outcome, price) {
  if (userTier === 'free') { openUpgrade(); return; }
  document.getElementById('tradeMarket').value = market || '';
  document.getElementById('tradeOutcome').value = outcome || '';
  document.getElementById('tradePrice').value = price || '';
  document.getElementById('tradeAmount').value = '';
  openModal('tradeModal');
}

function openTradeModal() {
  if (userTier === 'free') { openUpgrade(); return; }
  document.getElementById('tradeMarket').value = '';
  document.getElementById('tradeOutcome').value = '';
  document.getElementById('tradePrice').value = '';
  document.getElementById('tradeAmount').value = '';
  openModal('tradeModal');
}

function submitTrade() {
  const market = document.getElementById('tradeMarket').value.trim();
  const outcome = document.getElementById('tradeOutcome').value.trim();
  const price = parseInt(document.getElementById('tradePrice').value, 10);
  const amount = parseFloat(document.getElementById('tradeAmount').value);
  if (!market || !outcome || !price || !amount) return;
  fetch('/api/trades', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ market_name: market, outcome: outcome, entry_price: price, amount: amount })
  }).then(r => { if (r.ok) { closeModal('tradeModal'); renderProfitTracker(); } }).catch(() => {});
}

/* ===== Modals ===== */
function openGlossary() { openModal('glossaryModal'); }
function openCustomize() { applyLayoutToUI(); openModal('customizeModal'); }
function openUpgrade() { openModal('upgradeModal'); }

function dismissHero() {
  localStorage.setItem('sharpe_hero_dismissed', '1');
  document.getElementById('heroBanner').style.display = 'none';
}

/* ===== Cross-Sport Ticker ===== */
function renderCrossSportTicker() {
  const edges = (data && data.cross_sport_edges) || [];
  const wrap = document.getElementById('crossSportTicker');
  const items = document.getElementById('tickerItems');
  if (!edges.length) { wrap.style.display = 'none'; return; }
  wrap.style.display = '';
  items.innerHTML = edges.slice(0, 10).map(e =>
    '<div class="ticker-item" onclick="switchSport(\'' + (Object.entries(sports).find(([k,s]) => s.title === e.sport_name)?.[0] || '') + '\')">' +
    '<div class="ticker-sport">' + esc(e.sport_name) + '</div>' +
    '<div class="ticker-teams">' + esc(e.home_team) + ' vs ' + esc(e.away_team) + '</div>' +
    '<div class="ticker-edge">' + fmtPct(Math.abs(e.divergence)) + ' &mdash; ' + esc(e.outcome) + '</div>' +
    '</div>'
  ).join('');
}

/* ===== Sparkline Drawing ===== */
function drawSparklines() {
  if (!data || !data.comparisons) return;
  data.comparisons.forEach((c, ci) => {
    (c.outcomes || []).forEach((o, oi) => {
      const t = o.trend || {};
      if (!t.points || t.points.length < 3) return;
      const cvs = document.getElementById('card-' + ci + '-spark-' + oi);
      if (!cvs) return;
      const ctx = cvs.getContext('2d');
      const w = cvs.width, h = cvs.height;
      const pts = t.points;
      const mn = Math.min(...pts), mx = Math.max(...pts);
      const range = mx - mn || 1;
      ctx.clearRect(0, 0, w, h);
      ctx.beginPath();
      ctx.strokeStyle = t.direction === 'widening' ? '#34d399' : t.direction === 'narrowing' ? '#f87171' : '#5c5e6a';
      ctx.lineWidth = 1.5;
      pts.forEach((v, i) => {
        const x = (i / (pts.length - 1)) * w;
        const y = h - ((v - mn) / range) * (h - 4) - 2;
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      });
      ctx.stroke();
    });
  });
}

/* ===== Flag Match ===== */
function flagMatch(home, away, polyQ) {
  const reason = prompt('Why is this match incorrect? (optional)');
  if (reason === null) return;
  fetch('/api/flag-match', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ home_team: home, away_team: away, poly_question: polyQ, reason: reason })
  }).then(r => {
    if (r.ok) alert('Match flagged. Thank you!');
    else alert('Failed to flag match.');
  }).catch(() => alert('Error flagging match.'));
}

/* ===== Edge Performance / Sharpe ===== */
function loadEdgePerformance() {
  fetch('/api/edge-performance', { credentials: 'same-origin' }).then(r => r.ok ? r.json() : null).then(d => {
    if (!d) return;
    const empty = document.getElementById('edgeStatsEmpty');
    if (!d.weekly || !d.weekly.length) { empty.style.display = ''; return; }
    empty.style.display = 'none';

    // Performance grid
    const grid = document.getElementById('perfGrid');
    const s = d.sharpe_ratio || 0;
    const sCol = s >= 1 ? 'var(--green)' : s >= 0 ? 'var(--text)' : 'var(--red)';
    grid.innerHTML =
      '<div class="perf-card"><div class="perf-value" style="color:' + sCol + '">' + fmt(s, 2) + '</div><div class="perf-label">Sharpe Ratio</div></div>' +
      '<div class="perf-card"><div class="perf-value">' + fmt(d.overall_win_rate, 1) + '%</div><div class="perf-label">Win Rate</div></div>' +
      '<div class="perf-card"><div class="perf-value">' + (d.total_resolved || 0) + '</div><div class="perf-label">Resolved Edges</div></div>' +
      '<div class="perf-card"><div class="perf-value">' + (d.total_correct || 0) + '</div><div class="perf-label">Correct Calls</div></div>';

    document.getElementById('sharpeValue').textContent = fmt(s, 2);
    document.getElementById('sharpeValue').style.color = sCol;
    document.getElementById('overallWinRate').textContent = fmt(d.overall_win_rate, 1) + '%';

    // Draw chart
    drawSharpeChart(d.weekly);
  }).catch(() => {});
}

function drawSharpeChart(weekly) {
  const cvs = document.getElementById('sharpeChart');
  if (!cvs || !weekly.length) return;
  const ctx = cvs.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  cvs.width = cvs.offsetWidth * dpr;
  cvs.height = 200 * dpr;
  ctx.scale(dpr, dpr);
  const w = cvs.offsetWidth, h = 200;
  const pad = { top: 20, right: 20, bottom: 30, left: 50 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  // Data
  const winRates = weekly.map(w => w.win_rate);
  const mn = Math.min(...winRates, 0);
  const mx = Math.max(...winRates, 100);
  const range = mx - mn || 1;

  // Background
  ctx.fillStyle = '#131417';
  ctx.fillRect(0, 0, w, h);

  // Grid lines
  ctx.strokeStyle = 'rgba(255,255,255,0.04)';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad.top + (plotH / 4) * i;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(w - pad.right, y); ctx.stroke();
    ctx.fillStyle = '#5c5e6a'; ctx.font = '10px Inter'; ctx.textAlign = 'right';
    ctx.fillText(fmt(mx - (range / 4) * i, 0) + '%', pad.left - 8, y + 3);
  }

  // 50% reference line
  const y50 = pad.top + plotH * (1 - (50 - mn) / range);
  ctx.strokeStyle = 'rgba(255,255,255,0.1)';
  ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(pad.left, y50); ctx.lineTo(w - pad.right, y50); ctx.stroke();
  ctx.setLineDash([]);

  // Win rate line
  ctx.beginPath();
  ctx.strokeStyle = '#34d399';
  ctx.lineWidth = 2;
  weekly.forEach((wk, i) => {
    const x = pad.left + (i / Math.max(weekly.length - 1, 1)) * plotW;
    const y = pad.top + plotH * (1 - (wk.win_rate - mn) / range);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Points
  weekly.forEach((wk, i) => {
    const x = pad.left + (i / Math.max(weekly.length - 1, 1)) * plotW;
    const y = pad.top + plotH * (1 - (wk.win_rate - mn) / range);
    ctx.beginPath();
    ctx.arc(x, y, 3, 0, Math.PI * 2);
    ctx.fillStyle = wk.win_rate >= 50 ? '#34d399' : '#f87171';
    ctx.fill();
  });

  // X-axis labels
  ctx.fillStyle = '#5c5e6a'; ctx.font = '9px Inter'; ctx.textAlign = 'center';
  const step = Math.max(1, Math.floor(weekly.length / 8));
  weekly.forEach((wk, i) => {
    if (i % step !== 0 && i !== weekly.length - 1) return;
    const x = pad.left + (i / Math.max(weekly.length - 1, 1)) * plotW;
    ctx.fillText(wk.week || '', x, h - 8);
  });
}

/* ===== Alerts Config ===== */
function loadAlertConfig() {
  fetch('/api/alerts', { credentials: 'same-origin' }).then(r => r.ok ? r.json() : null).then(d => {
    if (!d) return;
    document.getElementById('alertEnabled').checked = !!d.enabled;
    document.getElementById('alertMinEdge').value = d.min_edge || 5;
    document.getElementById('alertTgToken').value = d.telegram_bot_token || '';
    document.getElementById('alertTgChat').value = d.telegram_chat_id || '';
    document.getElementById('alertWebhook').value = d.webhook_url || '';
  }).catch(() => {});
}

function saveAlerts() {
  const body = {
    enabled: document.getElementById('alertEnabled').checked ? 1 : 0,
    min_edge: parseFloat(document.getElementById('alertMinEdge').value) || 5,
    telegram_bot_token: document.getElementById('alertTgToken').value.trim(),
    telegram_chat_id: document.getElementById('alertTgChat').value.trim(),
    webhook_url: document.getElementById('alertWebhook').value.trim(),
    sports: [],
  };
  fetch('/api/alerts', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(r => {
    const el = document.getElementById('alertStatus');
    if (r.ok) el.textContent = 'Saved!'; else el.textContent = 'Error saving.';
    setTimeout(() => el.textContent = '', 3000);
  }).catch(() => {});
}

function testAlert() {
  fetch('/api/alerts/test', { method: 'POST', credentials: 'same-origin' })
    .then(r => r.json()).then(d => {
      const el = document.getElementById('alertStatus');
      if (d.status === 'ok') el.textContent = 'Test alert sent!';
      else el.textContent = d.error || 'Error';
      setTimeout(() => el.textContent = '', 5000);
    }).catch(() => {});
}

/* ===== Countdown Timer ===== */
setInterval(() => {
  if (refreshCountdown > 0) refreshCountdown--;
  const el = document.getElementById('countdown');
  if (el) el.textContent = '(next scan in ' + refreshCountdown + 's)';
}, 1000);

/* ===== Init ===== */
activeSport = USER_SPORT || '';
if (!localStorage.getItem('sharpe_hero_dismissed')) {
  document.getElementById('heroBanner').style.display = '';
}
connectWS();
loadSports();
loadLayout();
loadSubscription();
loadUserInfo();

fetch('/api/data' + (activeSport ? '?sport=' + encodeURIComponent(activeSport) : ''), { credentials: 'same-origin' })
  .then(r => r.json()).then(d => { data = d; render(); }).catch(() => {});
</script>
</body>
</html>"""

# (Auth HTML removed — all auth handled by gateway via narve.ai/login)


_REMOVED_AUTH_HTML = """removed
  .auth-logo {
    display: flex;
    align-items: center;
    gap: 10px;
    font-weight: 600;
    font-size: 1.2em;
    letter-spacing: -0.02em;
    margin-bottom: 8px;
    justify-content: center;
  }
  .auth-logo-icon {
    width: 32px; height: 32px;
    background: linear-gradient(135deg, #6366f1, #8b5cf6); color: #fff;
    display: flex; align-items: center; justify-content: center;
    font-size: 15px; font-weight: 600;
    border-radius: 8px;
  }
  .auth-tagline {
    text-align: center;
    color: #8b8d98;
    font-size: 13px;
    font-weight: 300;
    margin-bottom: 40px;
  }
  .auth-card {
    background: #131417;
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 12px;
    padding: 32px;
  }
  .auth-tabs {
    display: flex;
    gap: 0;
    margin-bottom: 28px;
    border-bottom: 1px solid rgba(255,255,255,0.06);
  }
  .auth-tab {
    flex: 1;
    padding: 12px;
    text-align: center;
    font-weight: 500;
    font-size: 12px;
    cursor: pointer;
    color: #5c5e6a;
    border-bottom: 2px solid transparent;
    transition: all 0.15s;
    background: none;
    border-top: none; border-left: none; border-right: none;
    font-family: inherit;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .auth-tab:hover { color: #8b8d98; }
  .auth-tab.active {
    color: #ebebef;
    border-bottom-color: #6366f1;
  }
  .auth-form { display: none; }
  .auth-form.active { display: block; }
  .form-group {
    margin-bottom: 20px;
  }
  .form-group label {
    display: block;
    font-size: 10px;
    font-weight: 500;
    color: #5c5e6a;
    margin-bottom: 8px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .form-group input {
    width: 100%;
    padding: 10px 14px;
    background: #0c0d10;
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 8px;
    color: #ebebef;
    font-family: inherit;
    font-size: 14px;
    font-weight: 400;
    transition: border-color 0.15s;
  }
  .form-group input:focus {
    outline: none;
    border-color: #6366f1;
    box-shadow: 0 0 0 3px rgba(99,102,241,0.10);
  }
  .form-group input::placeholder { color: #5c5e6a; }
  .auth-btn {
    width: 100%;
    padding: 12px;
    background: linear-gradient(135deg, #6366f1, #8b5cf6);
    color: #fff;
    border: none;
    border-radius: 8px;
    font-family: inherit;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: opacity 0.15s, transform 0.15s;
    margin-top: 8px;
    letter-spacing: 0.02em;
  }
  .auth-btn:hover { opacity: 0.9; transform: translateY(-1px); }
  .auth-btn:disabled { opacity: 0.3; cursor: not-allowed; transform: none; }
  .auth-error {
    background: rgba(248,113,113,0.06);
    border: 1px solid rgba(248,113,113,0.15);
    border-radius: 8px;
    color: #f87171;
    padding: 10px 14px;
    font-size: 12px;
    font-weight: 400;
    margin-bottom: 16px;
    display: none;
  }
  .auth-footer {
    text-align: center;
    margin-top: 28px;
    color: #5c5e6a;
    font-size: 11px;
    font-weight: 300;
  }
</style>
</head>
<body>
<div class="auth-container">
  <div class="auth-logo">
    <div class="auth-logo-icon">S</div>
    <span>Sharpe</span>
  </div>
  <div class="auth-tagline">Find mispriced bets before the market corrects</div>

  <div class="auth-card">
    <div class="auth-tabs">
      <button class="auth-tab active" onclick="showTab('login')">Sign In</button>
      <button class="auth-tab" onclick="showTab('register')">Create Account</button>
    </div>

    <div id="authError" class="auth-error"></div>

    <!-- Login Form -->
    <form class="auth-form active" id="loginForm" onsubmit="handleLogin(event)">
      <div class="form-group">
        <label>Username or Email</label>
        <input type="text" id="loginEmail" placeholder="username or you@example.com" required />
      </div>
      <div class="form-group">
        <label>Password</label>
        <input type="password" id="loginPassword" placeholder="Your password" required />
      </div>
      <button class="auth-btn" type="submit">Sign In</button>
    </form>

    <!-- Register Form -->
    <form class="auth-form" id="registerForm" onsubmit="handleRegister(event)">
      <div class="form-group">
        <label>Username</label>
        <input type="text" id="regUsername" placeholder="Pick a username" required />
      </div>
      <div class="form-group">
        <label>Email</label>
        <input type="email" id="regEmail" placeholder="you@example.com" required />
      </div>
      <div class="form-group">
        <label>Password</label>
        <input type="password" id="regPassword" placeholder="At least 6 characters" required minlength="6" />
      </div>
      <button class="auth-btn" type="submit">Create Account</button>
    </form>
  </div>

  <div class="auth-footer">
    Sharpe is an informational tool only. Not financial advice.
  </div>
</div>

<script>
function showTab(tab) {
  document.querySelectorAll('.auth-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.auth-form').forEach(f => f.classList.remove('active'));
  if (tab === 'login') {
    document.querySelector('.auth-tab:first-child').classList.add('active');
    document.getElementById('loginForm').classList.add('active');
  } else {
    document.querySelector('.auth-tab:last-child').classList.add('active');
    document.getElementById('registerForm').classList.add('active');
  }
  document.getElementById('authError').style.display = 'none';
}

function showError(msg) {
  const el = document.getElementById('authError');
  el.textContent = msg;
  el.style.display = 'block';
}

async function handleLogin(e) {
  e.preventDefault();
  const btn = e.target.querySelector('.auth-btn');
  btn.disabled = true;
  btn.textContent = 'Signing in...';
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        email: document.getElementById('loginEmail').value,
        password: document.getElementById('loginPassword').value,
      })
    });
    const d = await r.json();
    if (r.ok) {
      window.location.href = '/';
    } else {
      showError(d.error || 'Login failed');
    }
  } catch(err) {
    showError('Connection error. Please try again.');
  }
  btn.disabled = false;
  btn.textContent = 'Sign In';
}

async function handleRegister(e) {
  e.preventDefault();
  const btn = e.target.querySelector('.auth-btn');
  btn.disabled = true;
  btn.textContent = 'Creating account...';
  try {
    const r = await fetch('/api/register', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        username: document.getElementById('regUsername').value,
        email: document.getElementById('regEmail').value,
        password: document.getElementById('regPassword').value,
      })
    });
    const d = await r.json();
    if (r.ok) {
      window.location.href = '/';
    } else {
      showError(d.error || 'Registration failed');
    }
  } catch(err) {
    showError('Connection error. Please try again.');
  }
  btn.disabled = false;
  btn.textContent = 'Create Account';
}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Users HTML (admin-only user directory)
# ---------------------------------------------------------------------------

USERS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sharpe — Users</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root { --bg: #0a0a0f; --surface: #13131a; --border: #1e1e2e; --text: #e4e4e7; --muted: #71717a; --green: #22c55e; --red: #ef4444; --blue: #3b82f6; --accent: #a78bfa; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }
  .top-bar { display: flex; align-items: center; justify-content: space-between; padding: 16px 24px; background: var(--surface); border-bottom: 1px solid var(--border); }
  .top-bar h1 { font-size: 20px; font-weight: 700; }
  .top-bar h1 span { color: var(--accent); }
  .nav-links { display: flex; gap: 12px; }
  .nav-links a { color: var(--muted); text-decoration: none; font-size: 13px; padding: 6px 12px; border-radius: 6px; transition: all .2s; }
  .nav-links a:hover, .nav-links a.active { color: var(--text); background: var(--border); }
  .container { max-width: 1200px; margin: 24px auto; padding: 0 24px; }
  .stats-row { display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }
  .stat-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px 20px; flex: 1; min-width: 120px; }
  .stat-val { font-size: 28px; font-weight: 800; }
  .stat-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-top: 4px; }
  .search-bar { margin-bottom: 16px; }
  .search-bar input { width: 100%; padding: 10px 14px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; color: var(--text); font-size: 14px; outline: none; }
  .search-bar input:focus { border-color: var(--accent); }
  .users-table { width: 100%; border-collapse: collapse; background: var(--surface); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
  .users-table th { background: var(--border); padding: 10px 14px; text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); cursor: pointer; user-select: none; white-space: nowrap; }
  .users-table th:hover { color: var(--text); }
  .users-table td { padding: 10px 14px; border-top: 1px solid var(--border); font-size: 13px; white-space: nowrap; }
  .users-table tr:hover td { background: rgba(167,139,250,0.05); }
  .tier-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; text-transform: uppercase; }
  .tier-badge.pro { background: rgba(34,197,94,0.15); color: var(--green); }
  .tier-badge.free { background: rgba(113,113,122,0.15); color: var(--muted); }
  .tier-badge.admin { background: rgba(167,139,250,0.15); color: var(--accent); }
  .btn-tier { padding: 4px 10px; border-radius: 4px; border: 1px solid var(--border); background: transparent; color: var(--text); font-size: 11px; cursor: pointer; margin-left: 4px; }
  .btn-tier:hover { background: var(--border); }
  .btn-tier.upgrade { border-color: var(--green); color: var(--green); }
  .btn-tier.downgrade { border-color: var(--red); color: var(--red); }
  .export-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
  .export-btn { padding: 8px 16px; background: var(--accent); color: #000; border: none; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; }
  .export-btn:hover { opacity: 0.9; }
  .user-count { font-size: 13px; color: var(--muted); }
  @media (max-width: 768px) {
    .users-table { font-size: 12px; display: block; overflow-x: auto; }
    .stats-row { gap: 8px; }
    .stat-card { min-width: 80px; padding: 10px 12px; }
    .stat-val { font-size: 22px; }
  }
</style>
</head>
<body>
<div class="top-bar">
  <h1><span>Sharpe</span> Users</h1>
  <div class="nav-links">
    <a href="/">Dashboard</a>
    <a href="/users" class="active">Users</a>
    <a href="/admin">Admin</a>
    <a href="/settings">Settings</a>
  </div>
</div>

<div class="container">
  <div class="stats-row">
    <div class="stat-card"><div class="stat-val" id="totalUsers">-</div><div class="stat-label">Total Users</div></div>
    <div class="stat-card"><div class="stat-val" id="proUsers" style="color:var(--text)">-</div><div class="stat-label">Pro Users</div></div>
    <div class="stat-card"><div class="stat-val" id="freeUsers">-</div><div class="stat-label">Free Users</div></div>
    <div class="stat-card"><div class="stat-val" id="activeToday" style="color:var(--blue)">-</div><div class="stat-label">Active Today</div></div>
    <div class="stat-card"><div class="stat-val" id="totalLogins">-</div><div class="stat-label">Total Logins</div></div>
  </div>

  <div class="export-bar">
    <span class="user-count" id="userCount"></span>
    <div>
      <button class="export-btn" onclick="exportCSV()">Export CSV</button>
    </div>
  </div>

  <div class="search-bar">
    <input type="text" id="searchInput" placeholder="Search by username, email, tier..." oninput="renderTable()" />
  </div>

  <table class="users-table" id="usersTable">
    <thead>
      <tr>
        <th onclick="sortTable('id')">ID</th>
        <th onclick="sortTable('username')">Username</th>
        <th onclick="sortTable('email')">Email</th>
        <th onclick="sortTable('tier')">Tier</th>
        <th onclick="sortTable('created_at')">Signed Up</th>
        <th onclick="sortTable('last_login')">Last Login</th>
        <th onclick="sortTable('login_count')">Logins</th>
        <th onclick="sortTable('default_sport')">Sport</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody id="usersBody"></tbody>
  </table>
</div>

<script>
let users = [];
let sortCol = 'created_at';
let sortDir = -1;

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

async function loadUsers() {
  try {
    const r = await fetch('/api/admin/users', { credentials: 'same-origin' });
    if (!r.ok) { if (r.status === 401) window.location.href = '/login'; return; }
    const d = await r.json();
    users = d.users || [];

    const today = new Date().toISOString().slice(0, 10);
    const pro = users.filter(u => (u.tier || 'free') === 'pro').length;
    document.getElementById('totalUsers').textContent = users.length;
    document.getElementById('proUsers').textContent = pro;
    document.getElementById('freeUsers').textContent = users.length - pro;
    document.getElementById('activeToday').textContent = users.filter(u => u.last_login && u.last_login.slice(0,10) === today).length;
    document.getElementById('totalLogins').textContent = users.reduce((a, u) => a + (u.login_count || 0), 0);

    renderTable();
  } catch(e) { console.error(e); }
}

function renderTable() {
  const search = (document.getElementById('searchInput').value || '').toLowerCase();
  let filtered = users.filter(u => {
    if (!search) return true;
    return (u.username||'').toLowerCase().includes(search) ||
           (u.email||'').toLowerCase().includes(search) ||
           (u.tier||'').toLowerCase().includes(search) ||
           (u.default_sport||'').toLowerCase().includes(search);
  });

  filtered.sort((a, b) => {
    let va = a[sortCol], vb = b[sortCol];
    if (va == null) va = '';
    if (vb == null) vb = '';
    if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * sortDir;
    return String(va).localeCompare(String(vb)) * sortDir;
  });

  document.getElementById('userCount').textContent = filtered.length + ' of ' + users.length + ' users';

  const tbody = document.getElementById('usersBody');
  tbody.innerHTML = filtered.map(u => {
    const tier = u.tier || 'free';
    const tierCls = u.is_admin ? 'admin' : tier;
    const tierLabel = u.is_admin ? 'ADMIN' : tier.toUpperCase();
    const created = u.created_at ? new Date(u.created_at).toLocaleDateString() : '-';
    const lastLogin = u.last_login ? new Date(u.last_login).toLocaleString() : 'Never';
    const toggleTier = tier === 'pro' ? 'free' : 'pro';
    const toggleCls = tier === 'pro' ? 'downgrade' : 'upgrade';
    const toggleLabel = tier === 'pro' ? 'Downgrade' : 'Upgrade';
    return '<tr>' +
      '<td>' + u.id + '</td>' +
      '<td><strong>' + esc(u.username || '-') + '</strong></td>' +
      '<td>' + esc(u.email || '-') + '</td>' +
      '<td><span class="tier-badge ' + tierCls + '">' + tierLabel + '</span></td>' +
      '<td>' + created + '</td>' +
      '<td>' + lastLogin + '</td>' +
      '<td>' + (u.login_count || 0) + '</td>' +
      '<td>' + esc(u.default_sport || '-') + '</td>' +
      '<td><button class="btn-tier ' + toggleCls + '" onclick="setTier(' + u.id + ',\\'' + toggleTier + '\\')">' + toggleLabel + '</button></td>' +
      '</tr>';
  }).join('');
}

function sortTable(col) {
  if (sortCol === col) sortDir *= -1;
  else { sortCol = col; sortDir = 1; }
  renderTable();
}

async function setTier(userId, tier) {
  if (!confirm('Set user ' + userId + ' to ' + tier + '?')) return;
  const r = await fetch('/api/admin/set-tier', {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ user_id: userId, tier: tier })
  });
  if (r.ok) loadUsers();
  else alert('Failed to update tier');
}

function exportCSV() {
  const headers = ['ID','Username','Email','Tier','Admin','Signed Up','Last Login','Logins','Sport','Threshold'];
  const rows = users.map(u => [
    u.id, u.username||'', u.email||'', u.tier||'free', u.is_admin?'Yes':'No',
    u.created_at||'', u.last_login||'', u.login_count||0, u.default_sport||'', u.divergence_threshold||5
  ]);
  let csv = headers.join(',') + '\\n' + rows.map(r => r.map(v => '"' + String(v).replace(/"/g,'""') + '"').join(',')).join('\\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'sharpe_users_' + new Date().toISOString().slice(0,10) + '.csv';
  a.click();
}

loadUsers();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Settings HTML
# ---------------------------------------------------------------------------

SETTINGS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sharpe — Settings</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #08090e;
    --surface: #12141c;
    --surface2: #1a1d2b;
    --border: #2a2d42;
    --border-light: #363a52;
    --text: #f0f0f8;
    --text-secondary: #a0a3b8;
    --muted: #6b6f8a;
    --green: #22c55e;
    --green-dim: rgba(34,197,94,0.12);
    --blue: #3b82f6;
    --blue-dim: rgba(59,130,246,0.12);
    --purple: #8b5cf6;
    --red: #ef4444;
    --radius: 12px;
    --radius-sm: 8px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
  }
  .settings-nav {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 16px 24px;
    background: rgba(18,20,28,0.85);
    border-bottom: 1px solid var(--border);
    backdrop-filter: blur(12px);
    position: sticky;
    top: 0;
    z-index: 100;
  }
  .settings-nav-logo {
    display: flex;
    align-items: center;
    gap: 10px;
    font-weight: 800;
    font-size: 1.2em;
    letter-spacing: -0.5px;
    text-decoration: none;
    color: var(--text);
  }
  .settings-nav-logo-icon {
    width: 32px; height: 32px;
    background: linear-gradient(135deg, var(--green), var(--blue));
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    font-size: 16px;
  }
  .settings-nav-links {
    display: flex;
    gap: 16px;
    align-items: center;
  }
  .settings-nav-links a {
    color: var(--text-secondary);
    text-decoration: none;
    font-size: 0.85em;
    font-weight: 500;
    transition: color 0.2s;
  }
  .settings-nav-links a:hover { color: var(--text); }
  .settings-nav-links a.active { color: var(--blue); }

  .settings-container {
    max-width: 640px;
    margin: 0 auto;
    padding: 32px 20px;
  }
  .settings-header {
    margin-bottom: 32px;
  }
  .settings-header h1 {
    font-size: 1.5em;
    font-weight: 800;
    letter-spacing: -0.5px;
    margin-bottom: 4px;
  }
  .settings-header p {
    color: var(--text-secondary);
    font-size: 0.88em;
  }

  .settings-section {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 24px;
    margin-bottom: 20px;
  }
  .settings-section-title {
    font-size: 0.72em;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--muted);
    margin-bottom: 20px;
  }
  .setting-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 14px 0;
    border-bottom: 1px solid var(--border);
  }
  .setting-row:last-child { border-bottom: none; }
  .setting-info {
    flex: 1;
  }
  .setting-name {
    font-weight: 600;
    font-size: 0.9em;
    margin-bottom: 2px;
  }
  .setting-desc {
    font-size: 0.78em;
    color: var(--muted);
  }
  .setting-control {
    flex-shrink: 0;
    margin-left: 20px;
  }
  .setting-control select, .setting-control input[type="number"] {
    padding: 8px 12px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    color: var(--text);
    font-family: inherit;
    font-size: 0.85em;
    min-width: 140px;
  }
  .setting-control select:focus, .setting-control input:focus {
    outline: none;
    border-color: var(--blue);
  }

  /* Toggle switch */
  .toggle {
    position: relative;
    width: 44px;
    height: 24px;
  }
  .toggle input { opacity: 0; width: 0; height: 0; }
  .toggle-slider {
    position: absolute;
    inset: 0;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 24px;
    cursor: pointer;
    transition: all 0.2s;
  }
  .toggle-slider::before {
    content: '';
    position: absolute;
    left: 3px;
    top: 3px;
    width: 16px;
    height: 16px;
    background: var(--muted);
    border-radius: 50%;
    transition: all 0.2s;
  }
  .toggle input:checked + .toggle-slider {
    background: var(--blue-dim);
    border-color: var(--blue);
  }
  .toggle input:checked + .toggle-slider::before {
    transform: translateX(20px);
    background: var(--blue);
  }

  .save-bar {
    position: sticky;
    bottom: 20px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px 24px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    box-shadow: 0 -4px 24px rgba(0,0,0,0.3);
    opacity: 0;
    transform: translateY(20px);
    transition: all 0.3s;
    pointer-events: none;
  }
  .save-bar.visible {
    opacity: 1;
    transform: translateY(0);
    pointer-events: all;
  }
  .save-bar-text {
    font-size: 0.85em;
    color: var(--text-secondary);
  }
  .save-btn {
    padding: 10px 24px;
    background: var(--green);
    color: #000;
    border: none;
    border-radius: var(--radius-sm);
    font-family: inherit;
    font-size: 0.85em;
    font-weight: 700;
    cursor: pointer;
    transition: all 0.2s;
  }
  .save-btn:hover { opacity: 0.9; }

  .user-section {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 16px 0;
  }
  .user-avatar {
    width: 48px;
    height: 48px;
    border-radius: 50%;
    background: linear-gradient(135deg, var(--blue), var(--purple));
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 800;
    font-size: 1.1em;
  }
  .user-info { flex: 1; }
  .user-name { font-weight: 700; font-size: 1em; }
  .user-email { font-size: 0.82em; color: var(--muted); }

  .logout-btn {
    padding: 8px 16px;
    background: transparent;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    color: var(--red);
    font-family: inherit;
    font-size: 0.82em;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;
  }
  .logout-btn:hover { background: rgba(239,68,68,0.1); border-color: var(--red); }
</style>
</head>
<body>

<nav class="settings-nav">
  <a class="settings-nav-logo" href="/">
    <div class="settings-nav-logo-icon">S</div>
    <span>Sharpe</span>
  </a>
  <div class="settings-nav-links">
    <a href="/">Dashboard</a>
    <a href="/settings" class="active">Settings</a>
  </div>
</nav>

<div class="settings-container">
  <div class="settings-header">
    <h1>Settings</h1>
    <p>Customize your Sharpe experience</p>
  </div>

  <!-- Account -->
  <div class="settings-section">
    <div class="settings-section-title">Account</div>
    <div class="user-section">
      <div class="user-avatar" id="userAvatar">?</div>
      <div class="user-info">
        <div class="user-name" id="userName">Loading...</div>
        <div class="user-email" id="userEmail"></div>
      </div>
      <a class="logout-btn" href="/api/logout">Sign Out</a>
    </div>
  </div>

  <!-- Trading Preferences -->
  <div class="settings-section">
    <div class="settings-section-title">Trading Preferences</div>

    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-name">Default Sport</div>
        <div class="setting-desc">Which sport to load when you open Sharpe</div>
      </div>
      <div class="setting-control">
        <select id="defaultSport">
          <option value="basketball_nba">NBA</option>
          <option value="americanfootball_nfl">NFL</option>
          <option value="icehockey_nhl">NHL</option>
          <option value="baseball_mlb">MLB</option>
          <option value="soccer_epl">EPL</option>
          <option value="soccer_spain_la_liga">La Liga</option>
          <option value="soccer_germany_bundesliga">Bundesliga</option>
          <option value="soccer_italy_serie_a">Serie A</option>
          <option value="soccer_france_ligue_one">Ligue 1</option>
          <option value="soccer_uefa_champs_league">Champions League</option>
          <option value="soccer_uefa_europa_league">Europa League</option>
          <option value="mma_mixed_martial_arts">MMA</option>
        </select>
      </div>
    </div>

    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-name">Edge Threshold</div>
        <div class="setting-desc">Minimum edge % to flag as an opportunity (default: 5%)</div>
      </div>
      <div class="setting-control">
        <input type="number" id="threshold" min="1" max="50" step="0.5" value="5" style="width:80px" />
      </div>
    </div>

    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-name">Opportunity Alerts</div>
        <div class="setting-desc">Get notified when new edges are found</div>
      </div>
      <div class="setting-control">
        <label class="toggle">
          <input type="checkbox" id="notifications" checked />
          <span class="toggle-slider"></span>
        </label>
      </div>
    </div>
  </div>

  <!-- Appearance -->
  <div class="settings-section">
    <div class="settings-section-title">Appearance</div>

    <div class="setting-row">
      <div class="setting-info">
        <div class="setting-name">Theme</div>
        <div class="setting-desc">Choose your preferred color scheme</div>
      </div>
      <div class="setting-control">
        <select id="theme">
          <option value="dark">Dark</option>
          <option value="light" disabled>Light (Coming Soon)</option>
        </select>
      </div>
    </div>
  </div>

  <!-- Save bar -->
  <div class="save-bar" id="saveBar">
    <span class="save-bar-text">You have unsaved changes</span>
    <button class="save-btn" onclick="saveSettings()">Save Changes</button>
  </div>
</div>

<script>
let originalSettings = {};
let currentSettings = {};

async function loadSettings() {
  try {
    const r = await fetch('/api/me');
    if (!r.ok) { window.location.href = '/login'; return; }
    const d = await r.json();

    document.getElementById('userName').textContent = d.username;
    document.getElementById('userEmail').textContent = d.email;
    document.getElementById('userAvatar').textContent = d.username.charAt(0).toUpperCase();

    const s = d.settings;
    document.getElementById('defaultSport').value = s.default_sport;
    document.getElementById('threshold').value = s.divergence_threshold;
    document.getElementById('notifications').checked = !!s.notifications_enabled;
    document.getElementById('theme').value = s.theme || 'dark';

    originalSettings = getFormSettings();
  } catch(e) {
    console.error(e);
  }
}

function getFormSettings() {
  return {
    default_sport: document.getElementById('defaultSport').value,
    divergence_threshold: parseFloat(document.getElementById('threshold').value),
    notifications_enabled: document.getElementById('notifications').checked ? 1 : 0,
    theme: document.getElementById('theme').value,
  };
}

function checkChanges() {
  currentSettings = getFormSettings();
  const changed = JSON.stringify(currentSettings) !== JSON.stringify(originalSettings);
  document.getElementById('saveBar').classList.toggle('visible', changed);
}

async function saveSettings() {
  const btn = document.querySelector('.save-btn');
  btn.textContent = 'Saving...';
  try {
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(getFormSettings())
    });
    if (r.ok) {
      originalSettings = getFormSettings();
      document.getElementById('saveBar').classList.remove('visible');
      btn.textContent = 'Saved!';
      setTimeout(() => { btn.textContent = 'Save Changes'; }, 1500);
    }
  } catch(e) {
    btn.textContent = 'Error — try again';
    setTimeout(() => { btn.textContent = 'Save Changes'; }, 2000);
  }
}

// Listen for changes
document.querySelectorAll('select, input').forEach(el => {
  el.addEventListener('change', checkChanges);
  el.addEventListener('input', checkChanges);
});

loadSettings();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Admin HTML
# ---------------------------------------------------------------------------

ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sharpe — Admin</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #08090e;
    --surface: #12141c;
    --surface2: #1a1d2b;
    --surface3: #222538;
    --border: #2a2d42;
    --border-light: #363a52;
    --text: #f0f0f8;
    --text-secondary: #a0a3b8;
    --muted: #6b6f8a;
    --green: #22c55e;
    --green-dim: rgba(34,197,94,0.12);
    --blue: #3b82f6;
    --blue-dim: rgba(59,130,246,0.12);
    --purple: #8b5cf6;
    --purple-dim: rgba(139,92,246,0.12);
    --red: #ef4444;
    --red-dim: rgba(239,68,68,0.12);
    --yellow: #eab308;
    --yellow-dim: rgba(234,179,8,0.12);
    --orange: #f97316;
    --radius: 12px;
    --radius-sm: 8px;
    --shadow: 0 4px 24px rgba(0,0,0,0.3);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
    line-height: 1.5;
  }

  /* Nav */
  .admin-nav {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 16px 24px;
    background: rgba(18,20,28,0.85);
    border-bottom: 1px solid var(--border);
    backdrop-filter: blur(12px);
    position: sticky;
    top: 0;
    z-index: 100;
  }
  .admin-nav-logo {
    display: flex;
    align-items: center;
    gap: 10px;
    font-weight: 800;
    font-size: 1.2em;
    letter-spacing: -0.5px;
    text-decoration: none;
    color: var(--text);
  }
  .admin-nav-logo-icon {
    width: 32px; height: 32px;
    background: linear-gradient(135deg, var(--green), var(--blue));
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    font-size: 16px; color: #fff; font-weight: 800;
  }
  .admin-badge {
    font-size: 0.6em;
    font-weight: 700;
    background: var(--purple);
    color: #fff;
    padding: 2px 8px;
    border-radius: 100px;
    text-transform: uppercase;
    letter-spacing: 1px;
  }
  .admin-nav-links {
    display: flex;
    gap: 16px;
    align-items: center;
  }
  .admin-nav-links a {
    color: var(--text-secondary);
    text-decoration: none;
    font-size: 0.85em;
    font-weight: 500;
    transition: color 0.2s;
  }
  .admin-nav-links a:hover { color: var(--text); }
  .admin-nav-links a.active { color: var(--purple); }

  /* Container */
  .admin-container {
    max-width: 1400px;
    margin: 0 auto;
    padding: 24px 20px;
  }
  .admin-header {
    margin-bottom: 24px;
  }
  .admin-header h1 {
    font-size: 1.5em;
    font-weight: 800;
    letter-spacing: -0.5px;
  }
  .admin-header p {
    color: var(--text-secondary);
    font-size: 0.88em;
    margin-top: 2px;
  }

  /* Stat cards */
  .admin-stats {
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 14px;
    margin-bottom: 20px;
  }
  .admin-stats-4 {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 14px;
    margin-bottom: 20px;
  }
  @media (max-width: 1100px) {
    .admin-stats { grid-template-columns: repeat(3, 1fr); }
    .admin-stats-4 { grid-template-columns: repeat(2, 1fr); }
  }
  @media (max-width: 700px) {
    .admin-stats { grid-template-columns: repeat(2, 1fr); }
    .admin-stats-4 { grid-template-columns: repeat(2, 1fr); }
  }
  .admin-stat {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px;
    transition: border-color 0.2s, transform 0.15s;
  }
  .admin-stat:hover { border-color: var(--border-light); transform: translateY(-1px); }
  .admin-stat-label {
    font-size: 0.7em;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--muted);
    margin-bottom: 6px;
  }
  .admin-stat-value {
    font-size: 1.9em;
    font-weight: 800;
    letter-spacing: -1px;
  }
  .admin-stat-sub {
    font-size: 0.75em;
    color: var(--muted);
    margin-top: 2px;
  }

  /* Sections */
  .admin-section {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    margin-bottom: 20px;
  }
  .admin-section-header {
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }
  .admin-section-title {
    font-size: 0.9em;
    font-weight: 700;
  }
  .admin-section-count {
    font-size: 0.75em;
    color: var(--muted);
    background: var(--surface2);
    padding: 2px 10px;
    border-radius: 100px;
  }

  /* Search */
  .search-box {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 8px 14px;
    color: var(--text);
    font-family: inherit;
    font-size: 0.82em;
    outline: none;
    width: 240px;
    transition: border-color 0.2s;
  }
  .search-box::placeholder { color: var(--muted); }
  .search-box:focus { border-color: var(--blue); }

  /* Table */
  .admin-table-wrap {
    overflow-x: auto;
  }
  .admin-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.82em;
  }
  .admin-table th {
    text-align: left;
    font-size: 0.7em;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--muted);
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    background: var(--surface2);
    cursor: pointer;
    user-select: none;
    white-space: nowrap;
    transition: color 0.2s;
  }
  .admin-table th:hover { color: var(--text-secondary); }
  .admin-table th .sort-arrow { margin-left: 4px; font-size: 0.9em; opacity: 0.4; }
  .admin-table th.sorted .sort-arrow { opacity: 1; color: var(--blue); }
  .admin-table td {
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  .admin-table tr:last-child td { border-bottom: none; }
  .admin-table tr:hover td { background: rgba(255,255,255,0.015); }

  .user-avatar-sm {
    width: 30px;
    height: 30px;
    border-radius: 50%;
    background: linear-gradient(135deg, var(--blue), var(--purple));
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-weight: 800;
    font-size: 0.7em;
    margin-right: 10px;
    vertical-align: middle;
    flex-shrink: 0;
    color: #fff;
  }
  .user-name-cell {
    display: flex;
    align-items: center;
    min-width: 200px;
  }
  .user-name-info { display: flex; flex-direction: column; }
  .user-email { font-size: 0.85em; color: var(--muted); }

  /* Badges */
  .badge-pro {
    font-size: 0.65em;
    font-weight: 700;
    background: var(--green-dim);
    color: var(--green);
    padding: 3px 8px;
    border-radius: 4px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .badge-free {
    font-size: 0.65em;
    font-weight: 700;
    background: var(--surface3);
    color: var(--muted);
    padding: 3px 8px;
    border-radius: 4px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .badge-admin {
    font-size: 0.65em;
    font-weight: 700;
    background: var(--purple-dim);
    color: var(--purple);
    padding: 2px 6px;
    border-radius: 4px;
    margin-left: 6px;
  }
  .badge-new {
    font-size: 0.65em;
    font-weight: 700;
    background: var(--green-dim);
    color: var(--green);
    padding: 2px 6px;
    border-radius: 4px;
    margin-left: 6px;
  }

  /* Buttons */
  .btn-sm {
    font-family: inherit;
    font-size: 0.72em;
    font-weight: 600;
    padding: 5px 12px;
    border-radius: 6px;
    border: none;
    cursor: pointer;
    transition: opacity 0.2s, transform 0.1s;
    white-space: nowrap;
  }
  .btn-sm:hover { opacity: 0.85; }
  .btn-sm:active { transform: scale(0.97); }
  .btn-sm:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-upgrade {
    background: var(--green);
    color: #fff;
  }
  .btn-downgrade {
    background: var(--red);
    color: #fff;
  }

  /* Two column bottom layout */
  .admin-bottom-grid {
    display: grid;
    grid-template-columns: 1fr 380px;
    gap: 20px;
  }
  @media (max-width: 1000px) {
    .admin-bottom-grid { grid-template-columns: 1fr; }
  }

  /* Activity feed */
  .activity-feed {
    max-height: 520px;
    overflow-y: auto;
  }
  .activity-feed::-webkit-scrollbar { width: 4px; }
  .activity-feed::-webkit-scrollbar-track { background: transparent; }
  .activity-feed::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
  .activity-item {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    gap: 10px;
    align-items: flex-start;
  }
  .activity-item:last-child { border-bottom: none; }
  .activity-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-top: 6px;
    flex-shrink: 0;
  }
  .activity-dot.login { background: var(--blue); }
  .activity-dot.register { background: var(--green); }
  .activity-dot.settings { background: var(--yellow); }
  .activity-dot.other { background: var(--purple); }
  .activity-text {
    font-size: 0.82em;
    flex: 1;
    color: var(--text-secondary);
  }
  .activity-text strong { color: var(--text); }
  .activity-time {
    font-size: 0.72em;
    color: var(--muted);
    white-space: nowrap;
  }

  /* Signup chart */
  .chart-container {
    padding: 16px 20px;
    height: 140px;
    display: flex;
    align-items: flex-end;
    gap: 3px;
  }
  .chart-bar {
    flex: 1;
    background: var(--blue);
    border-radius: 3px 3px 0 0;
    min-height: 2px;
    position: relative;
    transition: all 0.3s;
    cursor: default;
  }
  .chart-bar:hover {
    background: var(--purple);
  }
  .chart-bar:hover::after {
    content: attr(data-label);
    position: absolute;
    bottom: calc(100% + 6px);
    left: 50%;
    transform: translateX(-50%);
    background: var(--surface3);
    border: 1px solid var(--border-light);
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 10px;
    white-space: nowrap;
    z-index: 10;
    color: var(--text);
    pointer-events: none;
  }

  /* Edge performance */
  .edge-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 14px;
    padding: 20px;
  }
  @media (max-width: 700px) {
    .edge-grid { grid-template-columns: repeat(2, 1fr); }
  }
  .edge-item {
    text-align: center;
  }
  .edge-item-value {
    font-size: 1.6em;
    font-weight: 800;
    letter-spacing: -0.5px;
  }
  .edge-item-label {
    font-size: 0.72em;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-top: 2px;
  }

  /* Loading */
  .loading-center {
    text-align: center;
    padding: 60px 20px;
    color: var(--muted);
  }
  .loading-spinner {
    width: 28px; height: 28px;
    border: 3px solid var(--border);
    border-top-color: var(--blue);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin: 0 auto 12px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .mono { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.9em; }
</style>
</head>
<body>

<nav class="admin-nav">
  <a class="admin-nav-logo" href="/">
    <div class="admin-nav-logo-icon">S</div>
    <span>Sharpe</span>
    <span class="admin-badge">Admin</span>
  </a>
  <div class="admin-nav-links">
    <a href="/">Dashboard</a>
    <a href="/settings">Settings</a>
    <a href="/admin" class="active">Admin</a>
  </div>
</nav>

<div class="admin-container">
  <div class="admin-header">
    <h1>Admin Dashboard</h1>
    <p>Revenue, user management, and platform analytics</p>
  </div>

  <!-- Revenue & Growth Cards (6) -->
  <div class="admin-stats" id="revenueCards">
    <div class="admin-stat">
      <div class="admin-stat-label">MRR</div>
      <div class="admin-stat-value" style="color:var(--text)" id="statMrr">--</div>
      <div class="admin-stat-sub">Monthly recurring revenue</div>
    </div>
    <div class="admin-stat">
      <div class="admin-stat-label">Total Users</div>
      <div class="admin-stat-value" id="statTotalUsers">--</div>
      <div class="admin-stat-sub">Registered accounts</div>
    </div>
    <div class="admin-stat">
      <div class="admin-stat-label">Pro Users</div>
      <div class="admin-stat-value" style="color:var(--purple)" id="statProUsers">--</div>
      <div class="admin-stat-sub">Paying subscribers</div>
    </div>
    <div class="admin-stat">
      <div class="admin-stat-label">Free Users</div>
      <div class="admin-stat-value" id="statFreeUsers">--</div>
      <div class="admin-stat-sub">Free tier</div>
    </div>
    <div class="admin-stat">
      <div class="admin-stat-label">Conversion Rate</div>
      <div class="admin-stat-value" style="color:var(--orange)" id="statConversion">--</div>
      <div class="admin-stat-sub">Free to Pro</div>
    </div>
    <div class="admin-stat">
      <div class="admin-stat-label">Active Today</div>
      <div class="admin-stat-value" style="color:var(--text)" id="statActiveToday">--</div>
      <div class="admin-stat-sub">Logged in today</div>
    </div>
  </div>

  <!-- Engagement Cards (4) -->
  <div class="admin-stats-4" id="engagementCards">
    <div class="admin-stat">
      <div class="admin-stat-label">DAU</div>
      <div class="admin-stat-value" style="color:var(--blue)" id="statDau">--</div>
      <div class="admin-stat-sub">Daily active users</div>
    </div>
    <div class="admin-stat">
      <div class="admin-stat-label">WAU</div>
      <div class="admin-stat-value" style="color:var(--blue)" id="statWau">--</div>
      <div class="admin-stat-sub">Weekly active users</div>
    </div>
    <div class="admin-stat">
      <div class="admin-stat-label">MAU</div>
      <div class="admin-stat-value" style="color:var(--blue)" id="statMau">--</div>
      <div class="admin-stat-sub">Monthly active users</div>
    </div>
    <div class="admin-stat">
      <div class="admin-stat-label">Avg Logins/User</div>
      <div class="admin-stat-value" style="color:var(--purple)" id="statAvgLogins">--</div>
      <div class="admin-stat-sub">Engagement rate</div>
    </div>
  </div>

  <!-- Signup Chart -->
  <div class="admin-section">
    <div class="admin-section-header">
      <span class="admin-section-title">Signups (Last 30 Days)</span>
      <span class="admin-section-count" id="signupTotal">0 total</span>
    </div>
    <div class="chart-container" id="signupChart">
      <div class="loading-center" style="width:100%"><div class="loading-spinner"></div></div>
    </div>
  </div>

  <!-- Edge Performance -->
  <div class="admin-section">
    <div class="admin-section-header">
      <span class="admin-section-title">Edge Performance</span>
    </div>
    <div class="edge-grid" id="edgeStats">
      <div class="edge-item">
        <div class="edge-item-value" id="edgeTotal">--</div>
        <div class="edge-item-label">Total Edges</div>
      </div>
      <div class="edge-item">
        <div class="edge-item-value" style="color:var(--text)" id="edgeCorrect">--</div>
        <div class="edge-item-label">Correct</div>
      </div>
      <div class="edge-item">
        <div class="edge-item-value" style="color:var(--red)" id="edgeIncorrect">--</div>
        <div class="edge-item-label">Incorrect</div>
      </div>
      <div class="edge-item">
        <div class="edge-item-value" style="color:var(--blue)" id="edgeWinRate">--</div>
        <div class="edge-item-label">Win Rate</div>
      </div>
    </div>
    <div style="padding:0 20px 16px;text-align:center">
      <span style="font-size:0.78em;color:var(--muted)">Pending: <span id="edgePending">--</span></span>
    </div>
  </div>

  <!-- Users Table + Activity Feed side by side -->
  <div class="admin-bottom-grid">
    <div class="admin-section" style="margin-bottom:0">
      <div class="admin-section-header">
        <div style="display:flex;align-items:center;gap:10px">
          <span class="admin-section-title">Users</span>
          <span class="admin-section-count" id="userCount">0</span>
        </div>
        <input type="text" class="search-box" placeholder="Search users..." id="userSearch" oninput="searchUsers(this.value)">
      </div>
      <div class="admin-table-wrap">
        <table class="admin-table" id="usersTable">
          <thead>
            <tr>
              <th data-sort="username" onclick="sortTable('username')">User <span class="sort-arrow">&#8597;</span></th>
              <th data-sort="tier" onclick="sortTable('tier')">Tier <span class="sort-arrow">&#8597;</span></th>
              <th data-sort="created_at" onclick="sortTable('created_at')">Joined <span class="sort-arrow">&#8597;</span></th>
              <th data-sort="last_login" onclick="sortTable('last_login')">Last Login <span class="sort-arrow">&#8597;</span></th>
              <th data-sort="login_count" onclick="sortTable('login_count')">Logins <span class="sort-arrow">&#8597;</span></th>
              <th data-sort="default_sport" onclick="sortTable('default_sport')">Sport <span class="sort-arrow">&#8597;</span></th>
              <th>Threshold</th>
              <th>Referral</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="usersBody">
            <tr><td colspan="9" class="loading-center"><div class="loading-spinner"></div>Loading...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="admin-section" style="margin-bottom:0">
      <div class="admin-section-header">
        <span class="admin-section-title">Recent Activity</span>
      </div>
      <div class="activity-feed" id="activityFeed">
        <div class="loading-center"><div class="loading-spinner"></div>Loading...</div>
      </div>
    </div>
  </div>
</div>

<script>
const SPORT_NAMES = {
  basketball_nba: 'NBA', americanfootball_nfl: 'NFL', icehockey_nhl: 'NHL',
  baseball_mlb: 'MLB', soccer_epl: 'EPL', soccer_spain_la_liga: 'La Liga',
  soccer_germany_bundesliga: 'Bundesliga', soccer_italy_serie_a: 'Serie A',
  soccer_france_ligue_one: 'Ligue 1', soccer_uefa_champs_league: 'UCL',
  soccer_uefa_europa_league: 'UEL', mma_mixed_martial_arts: 'MMA'
};

let allUsers = [];
let currentSort = { key: 'created_at', dir: 'desc' };

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function timeAgo(dateStr) {
  if (!dateStr) return 'Never';
  const d = new Date(dateStr);
  const now = new Date();
  const diff = (now - d) / 1000;
  if (diff < 60) return 'Just now';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  if (diff < 604800) return Math.floor(diff / 86400) + 'd ago';
  return d.toLocaleDateString();
}

function formatDate(dateStr) {
  if (!dateStr) return '--';
  const d = new Date(dateStr);
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
}

function renderUsers(users) {
  const tbody = document.getElementById('usersBody');
  if (!users.length) {
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:40px">No users found</td></tr>';
    return;
  }
  tbody.innerHTML = users.map(u => {
    const initial = (u.username || '?').charAt(0).toUpperCase();
    const isNew = (new Date() - new Date(u.created_at)) < 86400000;
    const isPro = u.tier === 'pro';
    const tierBadge = isPro
      ? '<span class="badge-pro">PRO</span>'
      : '<span class="badge-free">FREE</span>';
    const actionBtn = isPro
      ? '<button class="btn-sm btn-downgrade" onclick="setTier(' + u.id + ', \'free\')">Downgrade</button>'
      : '<button class="btn-sm btn-upgrade" onclick="setTier(' + u.id + ', \'pro\')">Upgrade to Pro</button>';
    return '<tr>' +
      '<td><div class="user-name-cell">' +
        '<div class="user-avatar-sm">' + esc(initial) + '</div>' +
        '<div class="user-name-info">' +
          '<span>' + esc(u.username) +
            (u.is_admin ? '<span class="badge-admin">Admin</span>' : '') +
            (isNew ? '<span class="badge-new">New</span>' : '') +
          '</span>' +
          '<span class="user-email">' + esc(u.email) + '</span>' +
        '</div>' +
      '</div></td>' +
      '<td>' + tierBadge + '</td>' +
      '<td>' + formatDate(u.created_at) + '</td>' +
      '<td>' + (u.last_login ? timeAgo(u.last_login) : '<span style="color:var(--muted)">Never</span>') + '</td>' +
      '<td style="font-weight:700">' + (u.login_count || 0) + '</td>' +
      '<td><span style="color:var(--blue)">' + esc(SPORT_NAMES[u.default_sport] || u.default_sport || '--') + '</span></td>' +
      '<td>' + (u.divergence_threshold || 5) + '%</td>' +
      '<td><span class="mono">' + esc(u.referral_code || '--') + '</span></td>' +
      '<td>' + actionBtn + '</td>' +
    '</tr>';
  }).join('');
}

function searchUsers(query) {
  const q = query.toLowerCase().trim();
  if (!q) {
    renderUsers(allUsers);
    return;
  }
  const filtered = allUsers.filter(u =>
    (u.username || '').toLowerCase().includes(q) ||
    (u.email || '').toLowerCase().includes(q)
  );
  renderUsers(filtered);
}

function sortTable(key) {
  if (currentSort.key === key) {
    currentSort.dir = currentSort.dir === 'asc' ? 'desc' : 'asc';
  } else {
    currentSort.key = key;
    currentSort.dir = 'asc';
  }
  // Update header UI
  document.querySelectorAll('.admin-table th').forEach(th => th.classList.remove('sorted'));
  const activeTh = document.querySelector('.admin-table th[data-sort="' + key + '"]');
  if (activeTh) activeTh.classList.add('sorted');

  allUsers.sort((a, b) => {
    let va = a[key] || '';
    let vb = b[key] || '';
    if (key === 'login_count') { va = Number(va) || 0; vb = Number(vb) || 0; }
    if (typeof va === 'string') va = va.toLowerCase();
    if (typeof vb === 'string') vb = vb.toLowerCase();
    if (va < vb) return currentSort.dir === 'asc' ? -1 : 1;
    if (va > vb) return currentSort.dir === 'asc' ? 1 : -1;
    return 0;
  });

  const q = document.getElementById('userSearch').value;
  searchUsers(q);
}

async function setTier(userId, tier) {
  try {
    const r = await fetch('/api/admin/set-tier', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId, tier: tier })
    });
    if (!r.ok) {
      alert('Failed to update tier');
      return;
    }
    await loadAdmin();
  } catch (e) {
    console.error(e);
    alert('Error updating tier');
  }
}

async function loadAdmin() {
  try {
    const r = await fetch('/api/admin/stats');
    if (r.status === 403) {
      document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;background:#08090e"><h1 style="color:#ef4444;font-family:Inter,sans-serif">Access Denied</h1></div>';
      return;
    }
    if (!r.ok) { window.location.href = '/login'; return; }
    const d = await r.json();

    // Revenue & Growth cards
    document.getElementById('statMrr').textContent = '$' + (Number(d.mrr) || 0).toFixed(2);
    document.getElementById('statTotalUsers').textContent = d.total_users || 0;
    document.getElementById('statProUsers').textContent = d.pro_users || 0;
    document.getElementById('statFreeUsers').textContent = d.free_users || 0;
    document.getElementById('statConversion').textContent = (Number(d.conversion_rate) || 0).toFixed(1) + '%';
    document.getElementById('statActiveToday').textContent = d.active_today || 0;

    // Engagement cards
    document.getElementById('statDau').textContent = d.dau || 0;
    document.getElementById('statWau').textContent = d.wau || 0;
    document.getElementById('statMau').textContent = d.mau || 0;
    const avgLogins = d.total_users > 0 ? (d.total_logins / d.total_users).toFixed(1) : '0';
    document.getElementById('statAvgLogins').textContent = avgLogins;

    // Edge Performance
    const es = d.edge_stats || {};
    document.getElementById('edgeTotal').textContent = es.total || 0;
    document.getElementById('edgeCorrect').textContent = es.correct || 0;
    document.getElementById('edgeIncorrect').textContent = es.incorrect || 0;
    document.getElementById('edgeWinRate').textContent = (Number(es.win_rate) || 0).toFixed(1) + '%';
    document.getElementById('edgePending').textContent = es.pending || 0;

    // Users
    allUsers = d.users || [];
    document.getElementById('userCount').textContent = allUsers.length;
    const q = document.getElementById('userSearch').value;
    if (q) { searchUsers(q); } else { renderUsers(allUsers); }

    // Activity feed
    const feed = document.getElementById('activityFeed');
    const activity = d.activity || [];
    if (!activity.length) {
      feed.innerHTML = '<div style="padding:40px;text-align:center;color:var(--muted)">No activity yet</div>';
    } else {
      feed.innerHTML = activity.map(a => {
        const dotClass = a.action === 'login' ? 'login'
          : a.action === 'register' ? 'register'
          : a.action === 'settings' ? 'settings'
          : 'other';
        const actionText = a.action === 'login' ? 'logged in'
          : a.action === 'register' ? 'created account'
          : a.action === 'settings' ? 'updated settings'
          : esc(a.action);
        return '<div class="activity-item">' +
          '<div class="activity-dot ' + dotClass + '"></div>' +
          '<div class="activity-text"><strong>' + esc(a.username) + '</strong> ' + actionText + (a.detail ? ' -- ' + esc(a.detail) : '') + '</div>' +
          '<div class="activity-time">' + timeAgo(a.created_at) + '</div>' +
        '</div>';
      }).join('');
    }

    // Signup chart
    const chart = document.getElementById('signupChart');
    const signups = d.signups_by_day || [];
    if (signups.length) {
      const totalSignups = signups.reduce((s, x) => s + x.count, 0);
      document.getElementById('signupTotal').textContent = totalSignups + ' total';
      const maxCount = Math.max(...signups.map(s => s.count));
      chart.innerHTML = signups.map(s => {
        const pct = maxCount > 0 ? (s.count / maxCount * 100) : 0;
        return '<div class="chart-bar" style="height:' + Math.max(pct, 3) + '%" data-label="' + esc(s.day) + ': ' + s.count + ' signup' + (s.count !== 1 ? 's' : '') + '"></div>';
      }).join('');
    } else {
      chart.innerHTML = '<div style="text-align:center;color:var(--muted);width:100%;padding:20px">No signups in last 30 days</div>';
      document.getElementById('signupTotal').textContent = '0 total';
    }

  } catch (e) {
    console.error('Admin load error:', e);
  }
}

loadAdmin();
// Auto-refresh every 30s
setInterval(loadAdmin, 30000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8888, log_level="info")

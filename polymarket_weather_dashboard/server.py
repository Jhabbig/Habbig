#!/usr/bin/env python3
"""Polymarket Weather Dashboard — Flask backend."""

from __future__ import annotations

import functools
import hmac
import json
import logging
import math
import os
import re
import sqlite3
import statistics
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
from flask import Flask, jsonify, request, send_from_directory, make_response
from scipy.stats import norm

import weather_calibration as _wcal
import weather_pure as _wpure

app = Flask(__name__, static_folder="static")
_flask_secret = os.environ.get("FLASK_SECRET")
if not _flask_secret:
    import secrets as _sec
    _flask_secret = _sec.token_urlsafe(32)
    logging.warning("FLASK_SECRET not set — using random key (sessions won't persist across restarts)")
app.secret_key = _flask_secret

# Gzip compression — cuts 3.5MB market JSON to ~500KB over the wire
try:
    from flask_compress import Compress
    Compress(app)
except Exception:
    import gzip as _gzip
    @app.after_request
    def _gzip_response(response):
        try:
            accept = request.headers.get("Accept-Encoding", "")
            if "gzip" not in accept.lower():
                return response
            if response.status_code < 200 or response.status_code >= 300:
                return response
            if response.direct_passthrough or "Content-Encoding" in response.headers:
                return response
            data = response.get_data()
            if len(data) < 500:
                return response
            ct = (response.content_type or "").lower()
            if not any(t in ct for t in ("json", "javascript", "text", "html", "xml", "css")):
                return response
            compressed = _gzip.compress(data)
            response.set_data(compressed)
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Content-Length"] = str(len(compressed))
            response.headers["Vary"] = "Accept-Encoding"
        except Exception:
            pass
        return response

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# Prevent browsers/service workers from caching API responses
@app.after_request
def _api_no_cache(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


# ─── Database (SQLite) ────────────────────────────────────────────────────────

DB_PATH = Path(__file__).parent / "data.db"
_db_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS weather_signals_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id   TEXT,
    question    TEXT,
    category    TEXT,
    yes_price   REAL,
    model_prob  REAL,
    edge        REAL,
    action      TEXT DEFAULT 'auto',
    timestamp   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS weather_resolutions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT UNIQUE,
    actual_outcome  TEXT,
    payout          REAL,
    resolved_at     TEXT
);

CREATE TABLE IF NOT EXISTS weather_price_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id   TEXT NOT NULL,
    source      TEXT DEFAULT 'polymarket',
    question    TEXT,
    city        TEXT,
    target_date TEXT,
    yes_price   REAL,
    model_prob  REAL,
    edge        REAL,
    volume      REAL DEFAULT 0,
    timestamp   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(market_id, timestamp)
);

CREATE TABLE IF NOT EXISTS weather_alert_settings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT UNIQUE NOT NULL,
    edge_threshold  REAL DEFAULT 0.08,
    categories      TEXT DEFAULT '[]',
    push_enabled    INTEGER DEFAULT 0,
    email           TEXT
);

CREATE TABLE IF NOT EXISTS weather_user_prefs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT UNIQUE NOT NULL,
    settings    TEXT DEFAULT '{}',
    favorites   TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS weather_user_activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    action      TEXT,
    detail      TEXT,
    timestamp   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS profiles (
    id          TEXT PRIMARY KEY,
    username    TEXT,
    email       TEXT,
    is_admin    INTEGER DEFAULT 0,
    created_at  TEXT
);

-- Per-model forecast accuracy tracking. We log every forecast made and pair
-- it with the observed value once the target date passes, then compute
-- per-(model, station) bias to apply as a correction to future consensus.
CREATE TABLE IF NOT EXISTS forecast_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    station      TEXT NOT NULL,        -- ICAO code (e.g. KSEA)
    model        TEXT NOT NULL,        -- e.g. gfs_seamless, ecmwf_ifs025, nws
    target_date  TEXT NOT NULL,        -- YYYY-MM-DD the forecast is FOR
    made_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    forecast_high REAL NOT NULL,       -- predicted high (°F)
    observed_high REAL,                -- actual observed high once available
    paired_at    TEXT,                 -- when observed was joined in
    UNIQUE(station, model, target_date, made_at)
);
CREATE INDEX IF NOT EXISTS idx_fc_hist_station_model ON forecast_history(station, model);
CREATE INDEX IF NOT EXISTS idx_fc_hist_target ON forecast_history(target_date);

-- Daily ensemble spread snapshots so we can show how consensus is
-- evolving (tightening or widening) over the days leading up to resolution.
CREATE TABLE IF NOT EXISTS forecast_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    station      TEXT NOT NULL,
    target_date  TEXT NOT NULL,
    taken_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    mean         REAL NOT NULL,
    std          REAL NOT NULL,
    min          REAL,
    max          REAL,
    source_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fc_snap_lookup ON forecast_snapshots(station, target_date, taken_at);

-- Intraday running-max tracker. Polled every 5 minutes from METAR, this
-- table stores the highest temperature observed so far TODAY at each station.
-- The real alpha: at 2pm if the running max already exceeds the market's
-- threshold, the market should resolve YES with near-certainty.
CREATE TABLE IF NOT EXISTS intraday_max (
    icao         TEXT NOT NULL,
    obs_date     TEXT NOT NULL,           -- YYYY-MM-DD (local date at station)
    running_max  REAL NOT NULL,           -- highest temp_f observed so far today
    last_obs_f   REAL,                    -- most recent temp_f
    obs_count    INTEGER DEFAULT 1,       -- number of METAR checks today
    first_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (icao, obs_date)
);
"""


@contextmanager
def _get_conn(readonly=False):
    """Yield a SQLite connection with WAL mode and row_factory.

    WAL mode supports concurrent readers, so the lock is only held during
    writes (commit/rollback) to avoid blocking read-heavy web requests
    behind the snapshot thread.

    Pass readonly=True for SELECT-only queries to skip lock acquisition and commit.
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        if not readonly:
            with _db_lock:
                conn.commit()
    except Exception:
        if not readonly:
            with _db_lock:
                conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if they don't exist."""
    try:
        with _get_conn() as conn:
            conn.executescript(_SCHEMA)
        logger.info("SQLite database OK (%s)", DB_PATH)
    except Exception as e:
        logger.warning("SQLite init failed: %s", e)


DEFAULT_USER_SETTINGS = {
    "theme": "dark",
    "simplified_headlines": False,
    "plain_english": False,
    "plain_verdicts": False,
    "hide_expired": True,
    "forecast_only": False,
    "collapse_models": True,
    "edge_threshold": 8,
    "watched_cities": [],
}

def _is_behind_gateway() -> bool:
    """Check at call time (not import time) so env changes take effect."""
    return bool(os.environ.get("GATEWAY_SSO_SECRET"))

_BEHIND_GATEWAY = _is_behind_gateway()  # initial check for startup warning
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"
if not _BEHIND_GATEWAY and not _DEV_MODE:
    logging.warning("GATEWAY_SSO_SECRET not set and DEV_MODE not enabled — weather dashboard will reject unauthenticated requests")


def _get_user_from_request() -> Optional[dict]:
    """Extract authenticated user from gateway SSO headers.

    The weather dashboard is served behind the gateway. When the request
    carries the shared secret ``X-Gateway-Secret`` matching
    ``GATEWAY_SSO_SECRET``, we trust the user_id from ``X-Gateway-User-Id``
    directly as a UUID string. The gateway has already authenticated
    and subscription-checked them.
    """
    _sso_secret = os.environ.get("GATEWAY_SSO_SECRET")
    if _sso_secret and hmac.compare_digest(request.headers.get("X-Gateway-Secret", ""), _sso_secret):
        gw_id = request.headers.get("X-Gateway-User-Id")
        gw_email = request.headers.get("X-Gateway-User-Email")
        if gw_id:
            # Look up the profile from SQLite for admin status etc.
            try:
                with _get_conn(readonly=True) as conn:
                    row = conn.execute(
                        "SELECT * FROM profiles WHERE id = ? LIMIT 1", (gw_id,)
                    ).fetchone()
                    if row:
                        profile = dict(row)
                        return {
                            "id": profile["id"],
                            "username": profile.get("username", ""),
                            "email": profile.get("email", gw_email or ""),
                            "is_admin": profile.get("is_admin", 0),
                            "_gateway_sso": True,
                        }
            except Exception:
                pass
            # No profile found -- return synthetic user; gateway already authed them
            return {
                "id": gw_id,
                "username": (gw_email or "").split("@")[0],
                "email": gw_email or "",
                "is_admin": 0,
                "_gateway_sso": True,
            }
    return None


def require_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        user = _get_user_from_request()
        if not user:
            if not _is_behind_gateway():
                user = {"id": "local", "username": "local", "email": "", "is_admin": 1}
            else:
                return jsonify({"error": "unauthorized"}), 401
        request.user = user
        return f(*args, **kwargs)
    return wrapper


def require_admin(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        user = _get_user_from_request()
        if not user:
            if not _is_behind_gateway():
                user = {"id": "local", "username": "local", "email": "", "is_admin": 1}
            else:
                return jsonify({"error": "forbidden"}), 403
        if _is_behind_gateway() and not user.get("is_admin"):
            return jsonify({"error": "forbidden"}), 403
        request.user = user
        return f(*args, **kwargs)
    return wrapper


def log_activity(user_id: str, action: str, detail: str = None):
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO weather_user_activity (user_id, action, detail) VALUES (?, ?, ?)",
                (user_id, action, detail),
            )
    except Exception:
        pass


init_db()


# ─── Background-thread health tracking ────────────────────────────────────────
#
# Each background loop registers itself here on startup and records the
# outcome of every iteration. /api/healthz exposes the snapshot so an
# operator can see at a glance whether any loop has silently died.
_thread_health: dict[str, dict] = {}
_thread_health_lock = threading.Lock()


def _register_thread(name: str, interval_seconds: int) -> None:
    with _thread_health_lock:
        _thread_health[name] = {
            "name": name,
            "interval_seconds": interval_seconds,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "last_attempt_at": None,
            "last_success_at": None,
            "last_error": None,
            "consecutive_failures": 0,
            "total_runs": 0,
            "total_failures": 0,
        }


def _record_run(name: str, ok: bool, error: Optional[str] = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _thread_health_lock:
        h = _thread_health.setdefault(name, {
            "name": name, "interval_seconds": None, "started_at": now,
            "consecutive_failures": 0, "total_runs": 0, "total_failures": 0,
        })
        h["last_attempt_at"] = now
        h["total_runs"] += 1
        if ok:
            h["last_success_at"] = now
            h["consecutive_failures"] = 0
            h["last_error"] = None
        else:
            h["consecutive_failures"] += 1
            h["total_failures"] += 1
            h["last_error"] = error


def _thread_health_snapshot() -> dict:
    """Return a copy with derived staleness info for /api/healthz."""
    now = datetime.now(timezone.utc)
    with _thread_health_lock:
        out = {}
        for name, h in _thread_health.items():
            entry = dict(h)
            stale = None
            last = entry.get("last_success_at")
            interval = entry.get("interval_seconds") or 0
            if last:
                try:
                    last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                    elapsed = (now - last_dt).total_seconds()
                    entry["seconds_since_last_success"] = round(elapsed)
                    # Stale if we missed >2 intervals plus a 60s grace
                    if interval and elapsed > (2 * interval + 60):
                        stale = True
                    else:
                        stale = False
                except Exception:
                    pass
            entry["stale"] = stale
            out[name] = entry
        return out


# ─── Cache ─────────────────────────────────────────────────────────────────────

# OrderedDict so we get LRU eviction instead of "wipe everything when full".
# The previous .clear() pattern produced periodic thundering-herd cache misses
# every time the dict crossed the size threshold and triggered a global refetch
# storm against the upstream weather APIs.
from collections import OrderedDict as _OrderedDict
_cache: "_OrderedDict[str, dict]" = _OrderedDict()
_cache_lock = threading.Lock()
_CACHE_MAX_SIZE = 1000
_user_prefs_cache: dict = {}  # Fallback in-memory cache for user settings/favorites
_user_prefs_lock = threading.Lock()
_USER_PREFS_CACHE_MAX_SIZE = 1000
CACHE_TTL = 300  # 5 minutes — frontend polls every 4 min to stay ahead


def cache_get(key: str, ttl: int = None):
    with _cache_lock:
        entry = _cache.get(key)
        effective_ttl = ttl if ttl is not None else CACHE_TTL
        if entry and time.time() - entry["ts"] < effective_ttl:
            _cache.move_to_end(key)
            return entry["data"]
        if entry:
            _cache.pop(key, None)
        return None


def cache_set(key: str, data):
    with _cache_lock:
        _cache[key] = {"data": data, "ts": time.time()}
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX_SIZE:
            _cache.popitem(last=False)


# ─── Station Mapping ───────────────────────────────────────────────────────────

STATION_MAP = {
    "new york":      (40.7772, -73.8726, "KLGA", "LaGuardia Airport, NY"),
    "nyc":           (40.7772, -73.8726, "KLGA", "LaGuardia Airport, NY"),
    "chicago":       (41.9742, -87.9073, "KORD", "O'Hare International, IL"),
    "dallas":        (32.8471, -96.8518, "KDAL", "Dallas Love Field, TX"),
    "miami":         (25.7959, -80.2870, "KMIA", "Miami International, FL"),
    "los angeles":   (33.9425, -118.4081, "KLAX", "LAX, CA"),
    "la":            (33.9425, -118.4081, "KLAX", "LAX, CA"),
    "london":        (51.5053, -0.0553, "EGLC", "London City Airport"),
    "paris":         (48.7233, 2.3794, "LFPO", "Paris-Orly"),
    "tokyo":         (35.5533, 139.7811, "RJTT", "Haneda Airport"),
    "seoul":         (37.5586, 126.7906, "RKSS", "Gimpo International"),
    "sydney":        (-33.9461, 151.1772, "YSSY", "Sydney Airport"),
    "atlanta":       (33.6407, -84.4277, "KATL", "Hartsfield-Jackson, GA"),
    "austin":        (30.1945, -97.6699, "KAUS", "Austin-Bergstrom, TX"),
    "houston":       (29.6454, -95.2789, "KHOU", "William P. Hobby Airport, TX"),
    "denver":        (39.7169, -104.7529, "KBKF", "Buckley Space Force Base, CO"),
    "san francisco": (37.6213, -122.3790, "KSFO", "SFO, CA"),
    "seattle":       (47.4502, -122.3088, "KSEA", "Sea-Tac, WA"),
    "toronto":       (43.6772, -79.6306, "CYYZ", "Pearson International"),
    "munich":        (48.3537, 11.7750, "EDDM", "Munich Airport"),
    "milan":         (45.6306, 8.7281, "LIMC", "Malpensa Airport"),
    "madrid":        (40.4719, -3.5626, "LEMD", "Barajas Airport"),
    "warsaw":        (52.1657, 20.9671, "EPWA", "Chopin Airport"),
    "moscow":        (55.4100, 37.9023, "UUDD", "Domodedovo Airport"),
    "istanbul":      (40.9829, 28.8103, "LTFM", "Istanbul Airport"),
    "ankara":        (40.1281, 32.9951, "LTAC", "Esenboga Airport"),
    "tel aviv":      (32.0114, 34.8867, "LLBG", "Ben Gurion Airport"),
    "hong kong":     (22.3080, 113.9185, "VHHH", "Hong Kong International"),
    "shanghai":      (31.1443, 121.8083, "ZSPD", "Pudong International"),
    "beijing":       (40.0799, 116.5849, "ZBAA", "Beijing Capital International"),
    "shenzhen":      (22.6393, 113.8107, "ZGSZ", "Bao'an International"),
    "chongqing":     (29.7192, 106.6417, "ZUCK", "Jiangbei International"),
    "wuhan":         (30.7838, 114.2081, "ZHHH", "Tianhe International"),
    "chengdu":       (30.5785, 103.9471, "ZUUU", "Shuangliu International"),
    "taipei":        (25.0777, 121.2328, "RCTP", "Taoyuan International"),
    "singapore":     (1.3644, 103.9915, "WSSS", "Changi Airport"),
    "lucknow":       (26.7606, 80.8893, "VILK", "Chaudhary Charan Singh"),
    "wellington":    (-41.3272, 174.8053, "NZWN", "Wellington Airport"),
    "buenos aires":  (-34.5592, -58.4156, "SAEZ", "Ezeiza International"),
    "sao paulo":     (-23.4356, -46.4731, "SBGR", "Guarulhos International"),
    "mexico city":   (19.4363, -99.0721, "MMMX", "Benito Juárez International"),
    "busan":         (35.1796, 128.9382, "RKPK", "Gimhae International"),
    "amsterdam":     (52.3105, 4.7683, "EHAM", "Amsterdam Schiphol"),
    "helsinki":       (60.3172, 24.9633, "EFHK", "Helsinki Vantaa Airport"),
    "panama city":   (9.0714, -79.3835, "MPMG", "Marcos A. Gelabert International"),
    "kuala lumpur":   (2.7456, 101.7099, "WMKK", "Kuala Lumpur International"),
    "jakarta":       (-6.2666, 106.8910, "WIHH", "Halim Perdanakusuma International"),
}

CITY_ALIASES = {
    "new york city": "new york", "manhattan": "new york",
    "chi-town": "chicago", "l.a.": "la", "l.a": "la",
    "dfw": "dallas", "fort worth": "dallas",
    "sf": "san francisco", "são paulo": "sao paulo",
    "são paulo": "sao paulo", "cdmx": "mexico city",
}

# ─── Kalshi API ───────────────────────────────────────────────────────────────

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Maps Kalshi series tickers to our city keys
KALSHI_SERIES = {
    "KXHIGHNY":  "new york",
    "KXHIGHCHI": "chicago",
    "KXHIGHMIA": "miami",
    "KXHIGHLAX": "los angeles",
    "KXHIGHDEN": "denver",
}

# Additional climate/disaster series
KALSHI_EXTRA_SERIES = [
    "KXWARMING", "KXERUPTSUPER", "KXEARTHQUAKECALIFORNIA", "KXEARTHQUAKEJAPAN",
]


def fetch_kalshi_weather_markets() -> list[dict]:
    """Fetch weather markets from Kalshi API (no auth needed)."""
    cached = cache_get("kalshi_markets")
    if cached is not None:
        return cached

    from concurrent.futures import ThreadPoolExecutor

    all_markets: list[dict] = []

    def _fetch_series(series_ticker: str) -> list[dict]:
        markets = []
        cursor = None
        for _ in range(5):  # max 5 pages per series
            params = {"series_ticker": series_ticker, "status": "open", "limit": "200"}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = requests.get(f"{KALSHI_BASE}/markets", params=params, timeout=15)
                if resp.status_code != 200:
                    break
                data = resp.json()
                batch = data.get("markets", [])
                if not batch:
                    break
                markets.extend(batch)
                cursor = data.get("cursor")
                if not cursor:
                    break
            except Exception as e:
                logger.error("Kalshi fetch %s error: %s", series_ticker, e)
                break
        return markets

    all_series = list(KALSHI_SERIES.keys()) + KALSHI_EXTRA_SERIES
    with ThreadPoolExecutor(max_workers=6) as pool:
        for batch in pool.map(_fetch_series, all_series):
            all_markets.extend(batch)

    logger.info("Fetched %d markets from Kalshi API", len(all_markets))
    cache_set("kalshi_markets", all_markets)
    return all_markets


def _safe_float(value, default: float = 0.0) -> float:
    """Coerce arbitrary API payloads to float without crashing on junk.

    Kalshi/Polymarket occasionally return None, "", "NaN" or other non-numeric
    placeholders. Bare ``float(x)`` raises and previously aborted parsing of
    the entire batch — one bad market killed every sibling. This helper
    isolates that failure mode.
    """
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(result) or math.isinf(result):
        return default
    return result


def _parse_kalshi_market(m: dict) -> Optional[dict]:
    """Parse a Kalshi market into the same structure as Polymarket."""
    ticker = m.get("ticker", "")
    title = m.get("title", "")
    if not title:
        return None

    # Price: yes_bid is what you can buy YES at, yes_ask is what you can sell at
    # Use last_price or midpoint. _safe_float handles "NaN"/None/missing fields.
    yes_bid = _safe_float(m.get("yes_bid_dollars"))
    yes_ask = _safe_float(m.get("yes_ask_dollars"))
    last_price = _safe_float(m.get("last_price_dollars"))
    yes_price = last_price if last_price > 0 else ((yes_bid + yes_ask) / 2 if yes_bid and yes_ask else None)
    no_price = round(1.0 - yes_price, 4) if yes_price is not None else None

    # Parse city from series ticker
    event_ticker = m.get("event_ticker", "")
    series_ticker = ""
    for st in KALSHI_SERIES:
        if ticker.startswith(st) or event_ticker.startswith(st):
            series_ticker = st
            break

    city = KALSHI_SERIES.get(series_ticker)

    # Parse temperature from strike info
    temp_info = {"temp_lower": None, "temp_upper": None, "threshold": None,
                 "is_over": None, "unit": "F"}
    strike_type = m.get("strike_type", "")
    floor_strike = m.get("floor_strike")
    cap_strike = m.get("cap_strike")

    if strike_type == "greater" and floor_strike is not None:
        temp_info["threshold"] = _safe_float(floor_strike)
        temp_info["is_over"] = True
    elif strike_type == "less" and cap_strike is not None:
        temp_info["threshold"] = _safe_float(cap_strike)
        temp_info["is_over"] = False
    elif strike_type == "between" and floor_strike is not None and cap_strike is not None:
        temp_info["temp_lower"] = _safe_float(floor_strike)
        temp_info["temp_upper"] = _safe_float(cap_strike)

    has_temp = temp_info["threshold"] is not None or temp_info["temp_lower"] is not None

    # If no strike info, try parsing from title
    if not has_temp:
        temp_info = parse_temperature(title)
        has_temp = temp_info["threshold"] is not None or temp_info["temp_lower"] is not None

    # If no city from series, try parsing from title
    if not city:
        city = parse_city(title)

    # Parse date from event_ticker (e.g., KXHIGHNY-26APR04 → 2026-04-04)
    target_date = None
    date_match = re.search(r'-(\d{2})([A-Z]{3})(\d{2})', event_ticker)
    if date_match:
        yr, mon_str, day = date_match.groups()
        months = {"JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05", "JUN": "06",
                  "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12"}
        mon = months.get(mon_str)
        if mon:
            target_date = f"20{yr}-{mon}-{day}"

    if not target_date:
        target_date = parse_date(title)

    station = None
    if city:
        s = STATION_MAP.get(city)
        if s:
            station = {"lat": s[0], "lon": s[1], "icao": s[2], "name": s[3]}

    # Category
    title_lower = title.lower()
    if "temp" in title_lower or "high" in title_lower or "low" in title_lower:
        category = "temperature"
    elif "earthquake" in title_lower:
        category = "earthquake"
    elif "erupt" in title_lower or "volcano" in title_lower:
        category = "volcano"
    elif "warming" in title_lower or "climate" in title_lower:
        category = "climate_record"
    else:
        category = "other"

    # Volume — coerce defensively because Kalshi occasionally returns
    # non-numeric placeholders that would crash the whole batch.
    volume_fp = m.get("volume_fp") or m.get("volume") or 0
    open_interest = m.get("open_interest_fp") or 0

    return {
        "id": f"kalshi_{ticker}",
        "question": title,
        "slug": ticker,
        "event_title": event_ticker,
        "tags": ["kalshi", "weather"],
        "yes_price": yes_price,
        "no_price": no_price,
        "volume": str(_safe_float(volume_fp)),
        "liquidity": str(_safe_float(open_interest)),
        "end_date": m.get("close_time") or m.get("expiration_time"),
        "city": city,
        "target_date": target_date,
        "temp_info": temp_info if has_temp else None,
        "station": station,
        "forecast": None,
        "model_prob": None,
        "edge": None,
        "edge_pct": None,
        "has_forecast": False,
        "weather_prediction": None,
        "category": category,
        "resolution_station": None,
        "resolution_icao": None,
        "resolution_source": "NWS Climatological Report",
        "source": "kalshi",
        "kalshi_ticker": ticker,
    }


# ─── Gamma API ─────────────────────────────────────────────────────────────────

GAMMA_BASE = "https://gamma-api.polymarket.com"

# Tags that are genuinely weather/climate (not sports teams named "Hurricanes")
WEATHER_TAGS = {
    "weather", "climate", "climate & weather", "climate change", "global temp",
    "natural disaster", "natural disasters", "climate & science",
}

# Keywords in event titles that indicate weather (not in market questions, to avoid sports)
EVENT_KEYWORDS = [
    "temperature", "highest temp", "hottest year", "hottest month", "coldest",
    "heat wave", "°f", "°c", "precipitation", "rainfall", "snowfall",
    "hurricane", "tropical storm", "arctic", "sea ice",
    "earthquake", "tornado", "volcano", "eruption", "meteor",
    "warmest", "climate record",
]

# Reject events with these keywords (sports, politics, unrelated)
REJECT_KEYWORDS = [
    "nhl", "nba", "nfl", "mlb", "mls", "rugby", "grand prix", "formula 1",
    "f1", "boxing", "fight", "vs.", "champion", "playoff", "standings",
    "election", "president", "ceasefire", "ukraine", "nato", "coup",
    "treaty", "peace deal", "sovereignty", "referendum", "military",
    "mayor", "governor", "senate", "congress", "parliament",
    "ipo", "stock", "bitcoin", "crypto", "token", "launch",
    "ligue 1", "premier league", "la liga", "bundesliga", "serie a",
    "head-to-head", "podium", "relegat",
    "spacex", "starship", "ticker", "moon landing", "tesla", "xai",
    "ackman", "merger", "public ticker",
]


def _fetch_events_by_tag(tag_slug: str, seen_ids: set, all_markets: list, lock=None) -> None:
    """Fetch events for a single tag_slug, appending new markets to all_markets."""
    offset = 0
    for _ in range(10):
        try:
            resp = requests.get(f"{GAMMA_BASE}/events",
                                params={"tag_slug": tag_slug, "closed": "false",
                                        "limit": "100", "offset": str(offset)},
                                timeout=15)
            if resp.status_code != 200:
                break
            events = resp.json()
            if not events:
                break
            for event in events:
                title = (event.get("title", "") or "")
                title_lower = title.lower()
                if any(k in title_lower for k in REJECT_KEYWORDS):
                    continue
                tags = event.get("tags", [])
                for m in event.get("markets", []):
                    mid = m.get("conditionId") or m.get("id", "")
                    mq = (m.get("question", "") or "").lower()
                    if any(k in mq for k in ["win the", "finish in", "score", "goal", "assist"]):
                        continue
                    if lock:
                        with lock:
                            if mid and mid not in seen_ids:
                                seen_ids.add(mid)
                                m["_event_title"] = title
                                m["_event_tags"] = [t.get("label", "") for t in tags if isinstance(t, dict)]
                                all_markets.append(m)
                    else:
                        if mid and mid not in seen_ids:
                            seen_ids.add(mid)
                            m["_event_title"] = title
                            m["_event_tags"] = [t.get("label", "") for t in tags if isinstance(t, dict)]
                            all_markets.append(m)
            offset += 100
        except Exception as e:
            logger.error("Gamma tag_slug=%s error at offset %d: %s", tag_slug, offset, e)
            break


def fetch_all_weather_markets() -> list[dict]:
    """Fetch weather markets using targeted tag queries (fast)."""
    cached = cache_get("weather_markets")
    if cached is not None:
        return cached

    all_markets: list[dict] = []
    seen_ids: set[str] = set()
    import threading as _th
    _market_lock = _th.Lock()

    # Fetch via targeted tag_slug queries — much faster than paginating all events
    from concurrent.futures import ThreadPoolExecutor
    tag_slugs = ["temperature", "weather", "climate-change", "natural-disasters"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_fetch_events_by_tag, slug, seen_ids, all_markets, _market_lock) for slug in tag_slugs]
        for f in futures:
            f.result()

    logger.info("Fetched %d weather markets from Gamma API", len(all_markets))
    cache_set("weather_markets", all_markets)
    return all_markets


# ─── Parsing ───────────────────────────────────────────────────────────────────

def parse_city(title: str) -> Optional[str]:
    title_lower = title.lower()
    all_keys = list(STATION_MAP.keys()) + list(CITY_ALIASES.keys())
    all_keys.sort(key=len, reverse=True)
    for city in all_keys:
        # Word boundary check to avoid "dallas" matching in "vandals"
        if re.search(r'\b' + re.escape(city) + r'\b', title_lower):
            return CITY_ALIASES.get(city, city)
    return None


def parse_temperature(title: str) -> dict:
    result = {"temp_lower": None, "temp_upper": None, "threshold": None,
              "is_over": None, "unit": "F"}
    tl = title.lower()

    # Skip non-temperature markets that have numbers (earthquake magnitude, tornado counts, etc.)
    if any(k in tl for k in ["earthquake", "magnitude", "tornado", "hurricane", "landfall",
                              "sea ice", "arctic", "volcano", "eruption", "meteor",
                              "measles", "cases", "pandemic",
                              "snowfall", "rainfall", "inches", "precipitation", "rain", "snow"]):
        return result

    # Celsius patterns (global temp markets use C)
    celsius_range = re.search(r'between\s*([\d.]+)\s*[º°]?\s*c?\s*and\s*([\d.]+)\s*[º°]?\s*c', tl)
    if celsius_range:
        result["temp_lower"] = float(celsius_range.group(1))
        result["temp_upper"] = float(celsius_range.group(2))
        result["unit"] = "C"
        return result

    celsius_over = re.search(r'(?:more than|above|over|exceed|at least|greater than)\s*([\d.]+)\s*[º°]\s*c', tl)
    if celsius_over:
        result["threshold"] = float(celsius_over.group(1))
        result["is_over"] = True
        result["unit"] = "C"
        return result

    celsius_under = re.search(r'(?:less than|below|under)\s*([\d.]+)\s*[º°]\s*c', tl)
    if celsius_under:
        result["threshold"] = float(celsius_under.group(1))
        result["is_over"] = False
        result["unit"] = "C"
        return result

    # Also catch "1pt20c" style from Polymarket slugs embedded in titles
    pt_range = re.search(r'between\s*(\d+)pt(\d+)[º°]?c?\s*and\s*(\d+)pt(\d+)[º°]?c', tl)
    if pt_range:
        result["temp_lower"] = float(f"{pt_range.group(1)}.{pt_range.group(2)}")
        result["temp_upper"] = float(f"{pt_range.group(3)}.{pt_range.group(4)}")
        result["unit"] = "C"
        return result

    # Fahrenheit patterns
    for pat in [r'(\d+)\s*°?\s*f?\s*or\s*(?:higher|more|above)',
                r'(?:above|over|exceed|at\s+least)\s*(\d+)\s*°?\s*f',
                r'(\d+)\s*°?\s*f?\s*\+', r'≥\s*(\d+)']:
        m = re.search(pat, tl)
        if m:
            result["threshold"] = float(m.group(1))
            result["is_over"] = True
            return result

    for pat in [r'(\d+)\s*°?\s*f?\s*or\s*(?:lower|less|below)',
                r'(?:below|under)\s*(\d+)\s*°?\s*f', r'≤\s*(\d+)']:
        m = re.search(pat, tl)
        if m:
            result["threshold"] = float(m.group(1))
            result["is_over"] = False
            return result

    for pat in [r'(\d+)\s*[-–]\s*(\d+)\s*°?\s*f',
                r'between\s*(\d+)\s*(?:°?\s*f?)?\s*and\s*(\d+)\s*°?\s*f']:
        m = re.search(pat, tl)
        if m:
            result["temp_lower"] = float(m.group(1))
            result["temp_upper"] = float(m.group(2))
            return result

    single = re.search(r'(\d+)\s*°\s*f', tl)
    if single:
        result["threshold"] = float(single.group(1))
        result["is_over"] = True
        return result

    return result


def parse_date(title: str) -> Optional[str]:
    month_map = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
        "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    tl = title.lower()
    for pat in [r'\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})\b',
                r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2})\b']:
        m = re.search(pat, tl)
        if m:
            month = month_map[m.group(1)]
            day = int(m.group(2))
            year = datetime.now(timezone.utc).year
            try:
                dt = datetime(year, month, day, tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - dt).days > 30:
                    dt = datetime(year + 1, month, day, tzinfo=timezone.utc)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

    # Just month name (for monthly markets like "March 2026 Temperature")
    # Only match full month names (>3 chars) to avoid false positives ("mar" in "market")
    # "may" is included with word boundary since \bmay\b won't match "maybe"/"mayor"
    full_months = {k: v for k, v in month_map.items() if len(k) > 3 or k == "may"}
    for month_name, month_num in full_months.items():
        if re.search(r'\b' + month_name + r'\b', tl):
            year_m = re.search(r'(20\d{2})', title)
            year = int(year_m.group(1)) if year_m else datetime.now(timezone.utc).year
            return f"{year}-{month_num:02d}-15"  # mid-month

    iso = re.search(r'(\d{4})-(\d{2})-(\d{2})', title)
    if iso:
        return f"{iso.group(1)}-{iso.group(2)}-{iso.group(3)}"
    return None


# ─── Weather Forecasts ─────────────────────────────────────────────────────────

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
DETERMINISTIC_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

# Open-Meteo rate-limit cooldown: when we see a 429, set this to "now + 60s"
# and skip outbound fetches until it expires. Prevents cascading retries from
# making the problem worse and starving the weather_history endpoint.
_open_meteo_cooldown_until: float = 0.0
_open_meteo_cooldown_lock = threading.Lock()


def _open_meteo_in_cooldown() -> bool:
    with _open_meteo_cooldown_lock:
        return time.time() < _open_meteo_cooldown_until


def _open_meteo_trip_cooldown(seconds: int = 60) -> None:
    global _open_meteo_cooldown_until
    with _open_meteo_cooldown_lock:
        _open_meteo_cooldown_until = time.time() + seconds
    logger.warning("Open-Meteo rate-limited, cooling down for %ds", seconds)


def _fetch_ensemble_model(lat: float, lon: float, date_str: str, model: str) -> Optional[dict]:
    """Fetch ensemble forecast for a single model. Returns dict with mean/std/min/max/ensemble or None."""
    if _open_meteo_in_cooldown():
        return None
    try:
        resp = requests.get(ENSEMBLE_URL, params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit",
            "start_date": date_str, "end_date": date_str,
            "models": model,
        }, timeout=10)
        if resp.status_code == 429:
            _open_meteo_trip_cooldown()
            return None
        if resp.status_code == 200:
            daily = resp.json().get("daily", {})
            temps: list[float] = []
            for key, vals in daily.items():
                if key.startswith("temperature_2m_max") and vals:
                    for v in vals:
                        if v is not None:
                            temps.append(float(v))
            if temps:
                return {
                    "ensemble": temps,
                    "mean": round(statistics.mean(temps), 1),
                    "std": round(max(statistics.stdev(temps), 2.0) if len(temps) > 1 else 3.0, 1),
                    "min": round(min(temps), 1),
                    "max": round(max(temps), 1),
                    "source": model,
                    "members": len(temps),
                }
    except Exception as e:
        logger.warning("Ensemble fetch failed for %s: %s", model, e)
    return None


WEATHER_MODELS = {
    "gfs_seamless":   {"name": "GFS",   "org": "NOAA (USA)",         "members": 31},
    "ecmwf_ifs025":   {"name": "ECMWF", "org": "ECMWF (Europe)",     "members": 51},
    "icon_seamless":  {"name": "ICON",  "org": "DWD (Germany)",      "members": 40},
    "gem_global":     {"name": "GEM",   "org": "ECCC (Canada)",      "members": 21},
    "ukmo_seamless":  {"name": "UKMO",  "org": "Met Office (UK)",    "members": 18},
    "jma_seamless":   {"name": "JMA",   "org": "JMA (Japan)",        "members": 51},
    "metno_seamless": {"name": "MET.no","org": "MET Norway (Nordic)", "members": 30},
    "bom_access_global_ensemble": {"name": "BOM", "org": "BOM (Australia)", "members": 18},
}

# Which model Polymarket primarily resolves from (Weather Underground uses NWS/GFS for US)
RESOLUTION_MODEL = "gfs_seamless"


def fetch_multi_model_forecast(lat: float, lon: float, date_str: str,
                               station: Optional[str] = None) -> Optional[dict]:
    """Fetch forecasts from all available weather models + NWS + climatology in parallel.

    If `station` (a STATION_MAP key) is supplied, also applies per-model bias
    correction from `forecast_history`, inflates sigma based on lead time,
    snapshots the consensus to `forecast_snapshots`, and logs each model's
    forecast for future bias-pairing.
    """
    cache_key = f"multifc_{lat:.4f}_{lon:.4f}_{date_str}_{station or ''}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    from concurrent.futures import ThreadPoolExecutor, as_completed

    models_data = {}

    def _fetch_one(model_id):
        return model_id, _fetch_ensemble_model(lat, lon, date_str, model_id)

    def _fetch_nws():
        return "nws", fetch_nws_forecast(lat, lon, date_str)

    def _fetch_climo():
        return "climatology", fetch_climatology(lat, lon, date_str)

    # Launch all fetches in parallel: ensemble models + NWS + climatology
    with ThreadPoolExecutor(max_workers=len(WEATHER_MODELS) + 2) as pool:
        futures = {pool.submit(_fetch_one, m): m for m in WEATHER_MODELS.keys()}
        futures[pool.submit(_fetch_nws)] = "nws"
        futures[pool.submit(_fetch_climo)] = "climatology"

        for future in as_completed(futures):
            source_id = futures[future]
            try:
                model_id, result = future.result()
                if result:
                    if model_id in WEATHER_MODELS:
                        info = WEATHER_MODELS[model_id]
                        result["model_name"] = info["name"]
                        result["org"] = info["org"]
                        result["is_resolution_model"] = (model_id == RESOLUTION_MODEL)
                    models_data[model_id] = result
            except Exception as e:
                logger.warning("Forecast source %s failed: %s", source_id, e)

    if not models_data:
        return None

    # Separate forecast models from climatology for consensus
    forecast_models = {k: v for k, v in models_data.items() if k != "climatology"}
    if not forecast_models:
        return None

    # Apply per-model bias correction (forecast - observed) when we have data.
    # `bias > 0` means the model runs hot relative to obs at this station.
    biases: dict[str, float] = {}
    if station:
        try:
            biases = get_model_biases(station)
        except Exception as e:
            logger.warning("get_model_biases failed for %s: %s", station, e)
            biases = {}
    for mid, m in forecast_models.items():
        b = biases.get(mid)
        if b is not None:
            m["raw_mean"] = m["mean"]
            m["bias_correction"] = -round(b, 2)
            m["mean"] = round(m["mean"] - b, 1)

    # Member-weighted consensus (more ensemble members = more reliable)
    total_weight = 0
    weighted_mean = 0.0
    weighted_std = 0.0
    all_temps = []
    for m in forecast_models.values():
        w = m.get("members", 1) or 0
        if w <= 0:
            continue
        weighted_mean += m["mean"] * w
        weighted_std += m["std"] * w
        total_weight += w
        all_temps.extend(m["ensemble"])

    # If every model contributed zero weight (edge case where ensembles are
    # missing the `members` count or it was set to 0) we cannot fabricate a
    # forecast — return None so the dashboard shows "unavailable" rather than
    # silently displaying 0°F as the consensus.
    if total_weight <= 0:
        return None

    consensus_mean = round(weighted_mean / total_weight, 1)
    consensus_std = round(weighted_std / total_weight, 1) if weighted_std else 3.0

    # If climatology is available, use it as a Bayesian prior to shrink
    # extreme forecasts toward the historical norm (10% weight)
    climo = models_data.get("climatology")
    if climo and climo.get("mean") is not None:
        climo_weight = 0.10
        consensus_mean = round(
            consensus_mean * (1 - climo_weight) + climo["mean"] * climo_weight, 1
        )

    # Empirical sigma floor: ensemble spread is famously under-dispersive,
    # so we take max(ensemble_std, residual_std) before inflating for lead.
    residual_floor = None
    residuals_data: dict = {}
    if station:
        try:
            residuals_data = get_model_residuals(station)
            residual_floor = _wcal.consensus_sigma_floor(residuals_data)
        except Exception:
            residual_floor = None
    if residual_floor is not None and residual_floor > consensus_std:
        consensus_std = round(residual_floor, 1)

    # Inflate sigma based on lead time to resolution (skill decay).
    # When a station is supplied we use a curve fit from its own pairing
    # history; otherwise the hand-tuned default applies.
    lead_mult = lead_time_sigma_inflation(date_str, station=station)
    raw_consensus_std = consensus_std
    consensus_std = round(consensus_std * lead_mult, 1)

    source_count = len(forecast_models)
    source_label = f"{source_count} models"
    if "nws" in forecast_models:
        source_label += " + NWS"
    if climo:
        source_label += " + climo"
    if biases:
        source_label += " + bias-corrected"

    result = {
        "mean": consensus_mean,
        "std": consensus_std,
        "min": round(min(all_temps), 1) if all_temps else consensus_mean,
        "max": round(max(all_temps), 1) if all_temps else consensus_mean,
        "ensemble": all_temps,
        "source": source_label,
        "models": models_data,
        "lead_time_mult": round(lead_mult, 2),
        "raw_std": raw_consensus_std,
        "bias_corrected": bool(biases),
        "n_bias_models": len(biases),
        "empirical_sigma_floor": round(residual_floor, 2) if residual_floor else None,
        "n_residual_models": len(residuals_data) if residuals_data else 0,
    }

    # Snapshot the consensus and log per-model forecasts for future pairing
    if station:
        try:
            snapshot_forecast(station, date_str, consensus_mean, consensus_std,
                              result["min"], result["max"], source_count)
            for mid, m in forecast_models.items():
                log_forecast_for_bias(station, mid, date_str, m.get("raw_mean", m["mean"]))
        except Exception as e:
            logger.warning("Snapshot/log failed for %s: %s", station, e)

    cache_set(cache_key, result)
    return result


def fetch_nws_forecast(lat: float, lon: float, date_str: str) -> Optional[dict]:
    """Fetch NWS (National Weather Service) gridpoint forecast for US locations.

    Returns a dict with mean/std/min/max matching the ensemble model format,
    or None for non-US locations or on failure.
    """
    cache_key = f"nws_{lat}_{lon}_{date_str}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        # Step 1: Get the grid endpoint for this lat/lon
        points_resp = requests.get(
            f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}",
            headers={"User-Agent": "NoRainDashboard/1.0", "Accept": "application/geo+json"},
            timeout=10,
        )
        if points_resp.status_code != 200:
            return None
        forecast_url = points_resp.json().get("properties", {}).get("forecast")
        if not forecast_url:
            return None

        # Step 2: Get the forecast
        fc_resp = requests.get(
            forecast_url,
            headers={"User-Agent": "NoRainDashboard/1.0", "Accept": "application/geo+json"},
            timeout=10,
        )
        if fc_resp.status_code != 200:
            return None

        periods = fc_resp.json().get("properties", {}).get("periods", [])
        target_dt = datetime.strptime(date_str, "%Y-%m-%d").date()

        # Find daytime period matching the target date
        for period in periods:
            period_start = period.get("startTime", "")[:10]
            if period_start == date_str and period.get("isDaytime", False):
                temp_f = period.get("temperature")
                unit = period.get("temperatureUnit", "F")
                if temp_f is None:
                    return None
                if unit == "C":
                    temp_f = _c_to_f(temp_f)
                # NWS gives a single deterministic value; use typical NWS error margin
                result = {
                    "mean": round(float(temp_f), 1),
                    "std": 2.5,  # NWS typical 1-day MAE ~2-3°F
                    "min": round(float(temp_f) - 4.0, 1),
                    "max": round(float(temp_f) + 4.0, 1),
                    "ensemble": [float(temp_f)],
                    "source": "nws",
                    "model_name": "NWS",
                    "org": "NWS (USA)",
                    "members": 1,
                    "is_resolution_model": True,  # NWS is what Polymarket resolves from
                }
                cache_set(cache_key, result)
                return result
    except Exception as e:
        logger.warning("NWS forecast fetch failed for (%s, %s): %s", lat, lon, e)
    return None


def fetch_climatology(lat: float, lon: float, date_str: str) -> Optional[dict]:
    """Fetch historical climatology from Open-Meteo for calibration.

    Returns the average high temperature and std for this day-of-year
    over the past 30 years. Uses ONE ranged query and filters client-side
    to avoid rate-limiting.
    """
    cache_key = f"climo_{lat}_{lon}_{date_str}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    if _open_meteo_in_cooldown():
        return None

    try:
        target = datetime.strptime(date_str, "%Y-%m-%d")
        start_year = target.year - 30
        end_year = target.year - 1
        target_md = target.strftime("%m-%d")

        # ONE ranged query covering 30 years — Open-Meteo accepts this fine
        # and we filter to the target month-day client-side.
        resp = requests.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit",
                "start_date": f"{start_year}-01-01",
                "end_date": f"{end_year}-12-31",
            },
            timeout=20,
        )
        if resp.status_code == 429:
            _open_meteo_trip_cooldown()
            return None
        if resp.status_code != 200:
            return None

        daily = resp.json().get("daily", {})
        dates = daily.get("time", [])
        highs = daily.get("temperature_2m_max", [])

        years_data = []
        for d, h in zip(dates, highs):
            if h is None:
                continue
            # d is "YYYY-MM-DD"; we want the same MM-DD as target
            if len(d) >= 10 and d[5:10] == target_md:
                years_data.append(float(h))

        if len(years_data) < 5:
            return None

        climo_mean = round(statistics.mean(years_data), 1)
        climo_std = round(statistics.stdev(years_data), 1) if len(years_data) > 1 else 5.0

        result = {
            "mean": climo_mean,
            "std": climo_std,
            "min": round(min(years_data), 1),
            "max": round(max(years_data), 1),
            "ensemble": years_data,
            "source": "climatology",
            "model_name": "Climatology",
            "org": "30yr Historical Average",
            "members": len(years_data),
            "is_resolution_model": False,
        }
        cache_set(cache_key, result)
        return result
    except Exception as e:
        logger.warning("Climatology fetch failed for (%s, %s, %s): %s", lat, lon, date_str, e)
    return None


# ─── Live observations (METAR) ─────────────────────────────────────────────────

def fetch_metar(icao: str) -> Optional[dict]:
    """Fetch the latest METAR observation for an airport.

    Returns a dict with current conditions at the actual resolution
    station — temperature, wind, visibility, etc. This is the strongest
    signal for any market resolving in the next few hours.
    """
    if not icao:
        return None
    cache_key = f"metar_{icao}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    # aviationweather.gov returns JSON when format=json. Free, no auth.
    try:
        resp = requests.get(
            "https://aviationweather.gov/api/data/metar",
            params={"ids": icao, "format": "json", "hours": 2},
            timeout=10,
            headers={"User-Agent": "NoRainDashboard/1.0"},
        )
        if resp.status_code != 200:
            return None
        rows = resp.json()
        if not rows:
            return None
        # Most recent observation first
        latest = rows[0]
        temp_c = latest.get("temp")
        dewp_c = latest.get("dewp")
        wind_dir = latest.get("wdir")
        wind_speed = latest.get("wspd")  # knots
        wind_gust = latest.get("wgst")
        wx = latest.get("wxString") or ""
        clouds = latest.get("clouds") or []
        obs_time = latest.get("obsTime")
        result = {
            "icao": icao,
            "temp_f": round(temp_c * 9 / 5 + 32, 1) if temp_c is not None else None,
            "dewpoint_f": round(dewp_c * 9 / 5 + 32, 1) if dewp_c is not None else None,
            "wind_dir": wind_dir,
            "wind_mph": round(wind_speed * 1.15078, 1) if wind_speed is not None else None,
            "wind_gust_mph": round(wind_gust * 1.15078, 1) if wind_gust is not None else None,
            "weather": wx,
            "cloud_layers": [
                {"cover": c.get("cover"), "base_ft": c.get("base")}
                for c in clouds[:3]
            ],
            "obs_time": obs_time,
            "raw": latest.get("rawOb", ""),
        }
        cache_set(cache_key, result)
        return result
    except Exception as e:
        logger.warning("METAR fetch for %s failed: %s", icao, e)
    return None


# ─── Hourly forecast at the resolution station ─────────────────────────────────

def fetch_hourly_at_station(lat: float, lon: float, date_str: str) -> Optional[dict]:
    """Fetch hourly forecast for the target date and compute the local-day
    max from those hourly values. More accurate than the daily aggregate
    because it lets us pick the maximum at the actual reporting hours.
    """
    cache_key = f"hourly_{lat}_{lon}_{date_str}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    if _open_meteo_in_cooldown():
        return None

    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "hourly": "temperature_2m,cloud_cover,wind_speed_10m,wind_direction_10m,relative_humidity_2m",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "start_date": date_str, "end_date": date_str,
                "timezone": "auto",
            },
            timeout=12,
        )
        if resp.status_code == 429:
            _open_meteo_trip_cooldown()
            return None
        if resp.status_code != 200:
            return None
        hourly = resp.json().get("hourly", {})
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        clouds = hourly.get("cloud_cover", [])
        winds = hourly.get("wind_speed_10m", [])
        if not temps:
            return None
        valid = [(t, c, w, h) for t, c, w, h in zip(temps, clouds, winds, times) if t is not None]
        if not valid:
            return None
        max_temp = max(v[0] for v in valid)
        peak = next((v for v in valid if v[0] == max_temp), valid[0])
        result = {
            "max_f": round(max_temp, 1),
            "peak_hour": peak[3],
            "cloud_at_peak": peak[1],
            "wind_at_peak_mph": peak[2],
            "hours": len(valid),
        }
        cache_set(cache_key, result)
        return result
    except Exception as e:
        logger.warning("Hourly fetch failed for (%s, %s, %s): %s", lat, lon, date_str, e)
    return None


# ─── Per-model bias tracking ───────────────────────────────────────────────────

def log_forecast_for_bias(station: str, model: str, target_date: str, forecast_high: float):
    """Record a forecast we made so we can later pair it with the observed
    value and compute the model's per-station bias. No-op on duplicate."""
    if not station or not model or forecast_high is None:
        return
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO forecast_history (station, model, target_date, forecast_high) VALUES (?, ?, ?, ?)",
                (station, model, target_date, float(forecast_high)),
            )
    except Exception:
        pass


def pair_forecasts_with_observed(station: str, lat: float, lon: float, max_days_back: int = 14):
    """Look for unpaired forecasts whose target_date has passed and join in
    the observed high from Open-Meteo's archive. Called periodically.
    """
    if _open_meteo_in_cooldown():
        return 0
    try:
        with _get_conn(readonly=True) as conn:
            rows = conn.execute(
                """SELECT DISTINCT target_date FROM forecast_history
                   WHERE station = ? AND observed_high IS NULL
                     AND target_date < date('now')
                     AND target_date >= date('now', ?)""",
                (station, f"-{max_days_back} days"),
            ).fetchall()
        if not rows:
            return 0
        # Range query for all unpaired dates
        targets = sorted({r["target_date"] for r in rows})
        resp = requests.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": lat, "longitude": lon,
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit",
                "start_date": targets[0],
                "end_date": targets[-1],
            },
            timeout=20,
        )
        if resp.status_code == 429:
            _open_meteo_trip_cooldown()
            return 0
        if resp.status_code != 200:
            return 0
        daily = resp.json().get("daily", {})
        dates = daily.get("time", [])
        highs = daily.get("temperature_2m_max", [])
        observed = {d: h for d, h in zip(dates, highs) if h is not None}
        n = 0
        with _get_conn() as conn:
            for tdate, ohigh in observed.items():
                conn.execute(
                    """UPDATE forecast_history
                       SET observed_high = ?, paired_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                       WHERE station = ? AND target_date = ? AND observed_high IS NULL""",
                    (float(ohigh), station, tdate),
                )
                n += 1
        return n
    except Exception as e:
        logger.warning("Bias pairing failed for %s: %s", station, e)
        return 0


def get_model_biases(station: str, lookback_days: int = 30) -> dict[str, float]:
    """Return per-model mean error (forecast - observed) over the last N days.
    Positive bias means the model runs hot; subtract from forecasts to correct.
    """
    cache_key = f"bias_{station}_{lookback_days}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    biases: dict[str, float] = {}
    try:
        with _get_conn(readonly=True) as conn:
            rows = conn.execute(
                """SELECT model, AVG(forecast_high - observed_high) AS bias, COUNT(*) AS n
                   FROM forecast_history
                   WHERE station = ? AND observed_high IS NOT NULL
                     AND target_date >= date('now', ?)
                   GROUP BY model
                   HAVING n >= 5""",
                (station, f"-{lookback_days} days"),
            ).fetchall()
        for r in rows:
            biases[r["model"]] = round(float(r["bias"]), 2)
    except Exception as e:
        logger.warning("get_model_biases failed for %s: %s", station, e)
    cache_set(cache_key, biases)
    return biases


def get_model_residuals(station: str, lookback_days: int = 60) -> dict:
    """Return per-model {bias, residual_std, n} from forecast_history.

    Residual std is the empirical 1-sigma error of the model relative to
    the observed high. The dashboard uses this as a sigma floor — the raw
    ensemble spread is famously under-dispersive, and treating the larger
    of (ensemble_std, residual_std) as the true sigma fixes the most
    common over-confident-tail failure mode.
    """
    cache_key = f"residuals_{station}_{lookback_days}"
    cached = cache_get(cache_key, ttl=3600)
    if cached is not None:
        return cached
    out: dict = {}
    try:
        with _get_conn(readonly=True) as conn:
            rows = conn.execute(
                """SELECT model, forecast_high, observed_high
                   FROM forecast_history
                   WHERE station = ? AND observed_high IS NOT NULL
                     AND target_date >= date('now', ?)""",
                (station, f"-{lookback_days} days"),
            ).fetchall()
        out = _wcal.fit_residual_std([dict(r) for r in rows])
    except Exception as e:
        logger.warning("get_model_residuals failed for %s: %s", station, e)
    cache_set(cache_key, out)
    return out


_leadtime_curve_lock = threading.Lock()


def fit_leadtime_curve_for_station(station: str, lookback_days: int = 90) -> dict:
    """Fit sigma(lead_days) for one station from its own forecast_history.

    The fit replaces the hand-tuned `1.0 + 0.12 * sqrt(days)` constant. We
    cache the result for an hour because the underlying data only updates
    on the bias-pairing pass (every 6h)."""
    cache_key = f"leadtime_curve_{station}_{lookback_days}"
    cached = cache_get(cache_key, ttl=3600)
    if cached is not None:
        return cached
    fit: dict
    try:
        with _get_conn(readonly=True) as conn:
            rows = conn.execute(
                """SELECT
                       CAST(julianday(target_date) - julianday(date(made_at)) AS INTEGER) AS lead_days,
                       (forecast_high - observed_high) AS residual
                   FROM forecast_history
                   WHERE station = ? AND observed_high IS NOT NULL
                     AND target_date >= date('now', ?)""",
                (station, f"-{lookback_days} days"),
            ).fetchall()
        fit = _wcal.fit_leadtime_sigma_curve([dict(r) for r in rows])
    except Exception as e:
        logger.warning("fit_leadtime_curve_for_station failed for %s: %s", station, e)
        fit = {"k": 0.12, "intercept": 1.0, "by_lead": {}, "n": 0, "source": "default"}
    cache_set(cache_key, fit)
    return fit


# ─── Forecast snapshots (spread trend) ─────────────────────────────────────────

def snapshot_forecast(station: str, target_date: str, mean: float, std: float,
                      min_t: float, max_t: float, source_count: int):
    """Record an ensemble snapshot to track how consensus evolves."""
    if not station or mean is None:
        return
    try:
        with _get_conn() as conn:
            conn.execute(
                """INSERT INTO forecast_snapshots (station, target_date, mean, std, min, max, source_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (station, target_date, float(mean), float(std), float(min_t), float(max_t), int(source_count)),
            )
    except Exception:
        pass


def get_spread_trend(station: str, target_date: str, max_points: int = 10) -> list[dict]:
    """Return the most recent N snapshots for this (station, target) pair,
    oldest first, so the frontend can render a spread sparkline.
    """
    try:
        with _get_conn(readonly=True) as conn:
            rows = conn.execute(
                """SELECT taken_at, mean, std, min, max
                   FROM forecast_snapshots
                   WHERE station = ? AND target_date = ?
                   ORDER BY taken_at DESC LIMIT ?""",
                (station, target_date, max_points),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]
    except Exception:
        return []


# ─── ENSO / regime context ─────────────────────────────────────────────────────

def fetch_enso_state() -> Optional[dict]:
    """Fetch the latest ENSO ONI (Oceanic Niño Index) value from NOAA CPC.
    Returns the current 3-month-running mean SST anomaly and a classification.
    Cached for 24 hours since this is monthly data.

    Tries multiple NOAA endpoints since the "origin.*" host is unreliable.
    Parses both the SEAS-YR-TOTAL-ANOM (oni.ascii.txt) and YEAR-MON-...-ANOM
    (detrend.nino34) formats.
    """
    cache_key = "enso_state"
    cached = cache_get(cache_key, ttl=86400)  # 24h — monthly data
    if cached is not None:
        return cached

    candidates = [
        ("https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt", "oni"),
        ("https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/ensostuff/detrend.nino34.ascii.txt", "nino34"),
        ("https://origin.cpc.ncep.noaa.gov/products/analysis_monitoring/ensostuff/detrend.nino34.ascii.txt", "nino34"),
    ]
    text = None
    fmt = None
    for url, kind in candidates:
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200 and resp.text:
                text = resp.text
                fmt = kind
                break
        except Exception as e:
            logger.warning("ENSO source %s failed: %s", url, e)
            continue
    if not text:
        return None
    try:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if len(lines) < 4:
            return None
        anom = None
        month_label = None
        if fmt == "oni":
            # Format: "SEAS YR TOTAL ANOM" (e.g. "DJF 2026  27.20  0.45")
            for line in reversed(lines):
                parts = line.split()
                if len(parts) >= 4 and parts[1].isdigit():
                    try:
                        anom = float(parts[3])
                        month_label = f"{parts[1]}-{parts[0]}"
                    except ValueError:
                        continue
                    break
        else:
            # Format: "YEAR MON ... ANOM"
            for line in reversed(lines):
                parts = line.split()
                if len(parts) >= 5 and parts[0].isdigit():
                    try:
                        anom = float(parts[-1])
                        month_label = f"{parts[0]}-{int(parts[1]):02d}"
                    except ValueError:
                        continue
                    break
        if anom is None:
            return None

        if anom >= 0.5:
            phase = "El Niño"
            adjust = "+0.3°F bias warm in equatorial Pacific influence zones (S US, W coast)"
        elif anom <= -0.5:
            phase = "La Niña"
            adjust = "Pacific NW runs cool/wet, SW runs dry"
        else:
            phase = "Neutral"
            adjust = "No strong ENSO signal"
        result = {
            "phase": phase,
            "anomaly_c": round(anom, 2),
            "month": month_label,
            "adjustment_hint": adjust,
        }
        # Cache using proper LRU cache_set (ENSO data updates weekly, so normal TTL is fine)
        cache_set(cache_key, result)
        return result
    except Exception as e:
        logger.warning("ENSO parse failed: %s", e)
    return None


# ─── Teleconnection indices (AO / NAO / PDO) ───────────────────────────────────

def fetch_teleconnections() -> Optional[dict]:
    """Pull the latest AO, NAO, and PDO indices from NOAA. These are
    longer-cycle large-scale climate signals. Cached for 24h.
    """
    cache_key = "teleconnections"
    cached = cache_get(cache_key, ttl=86400)  # 24h — monthly data
    if cached is not None:
        return cached
    out = {}
    sources = {
        "ao": "https://www.cpc.ncep.noaa.gov/products/precip/CWlink/daily_ao_index/monthly.ao.index.b50.current.ascii",
        "nao": "https://www.cpc.ncep.noaa.gov/products/precip/CWlink/pna/norm.nao.monthly.b5001.current.ascii",
    }
    for name, url in sources.items():
        try:
            r = requests.get(url, timeout=12)
            if r.status_code != 200:
                continue
            lines = [l.strip() for l in r.text.splitlines() if l.strip()]
            last_val = None
            last_year = None
            last_month = None
            for line in lines[-12:]:
                parts = line.split()
                if len(parts) >= 3 and parts[0].isdigit():
                    try:
                        last_year = int(parts[0])
                        last_month = int(parts[1])
                        last_val = float(parts[2])
                    except ValueError:
                        continue
            if last_val is not None:
                out[name] = {
                    "value": round(last_val, 2),
                    "month": f"{last_year}-{last_month:02d}",
                    "phase": "+" if last_val > 0 else "-",
                }
        except Exception as e:
            logger.warning("Teleconnection %s fetch failed: %s", name, e)
    if not out:
        return None
    cache_set(cache_key, out)
    return out


# ─── Marine layer / coastal awareness ──────────────────────────────────────────

# Cities where marine layer or onshore/offshore flow significantly affects
# the forecast. The bearing is the direction TO the ocean from the city
# (so onshore wind = from this direction).
COASTAL_CITIES = {
    "los angeles":   {"ocean_bearing": 220, "type": "marine_layer"},
    "san francisco": {"ocean_bearing": 270, "type": "marine_layer"},
    "seattle":       {"ocean_bearing": 270, "type": "marine_layer"},
    "miami":         {"ocean_bearing": 90,  "type": "tropical"},
    "new york":      {"ocean_bearing": 135, "type": "atlantic"},
    "sydney":        {"ocean_bearing": 90,  "type": "marine_layer"},
    "tel aviv":      {"ocean_bearing": 270, "type": "mediterranean"},
    "tokyo":         {"ocean_bearing": 135, "type": "pacific"},
    "hong kong":     {"ocean_bearing": 180, "type": "tropical"},
    "singapore":     {"ocean_bearing": 180, "type": "tropical"},
    "wellington":    {"ocean_bearing": 180, "type": "marine_layer"},
}


def coastal_flow_assessment(city_key: str, wind_dir: Optional[float]) -> Optional[dict]:
    """Given a city and current wind direction (degrees), classify whether
    flow is onshore (cooling/moistening) or offshore (warming/drying)."""
    if wind_dir is None:
        return None
    info = COASTAL_CITIES.get(city_key)
    if not info:
        return None
    ocean = info["ocean_bearing"]
    diff = abs(((wind_dir - ocean + 180) % 360) - 180)
    if diff <= 60:
        flow = "onshore"
        hint = "marine air, cooler highs" if info["type"] == "marine_layer" else "humid maritime air"
    elif diff >= 120:
        flow = "offshore"
        hint = "warmer/drier (compressional warming)"
    else:
        flow = "alongshore"
        hint = "neutral coastal flow"
    return {"flow": flow, "type": info["type"], "hint": hint, "wind_dir": wind_dir}


# ─── NWS narrative parsing for fronts/pressure ─────────────────────────────────

_FRONT_KEYWORDS = re.compile(
    r"(cold front|warm front|stationary front|occluded front|trough|ridge|"
    r"high pressure|low pressure|frontal passage|frontal boundary)",
    re.IGNORECASE,
)


def fetch_nws_synoptic(lat: float, lon: float) -> Optional[dict]:
    """Fetch the NWS narrative forecast and extract synoptic features
    (fronts, pressure systems) by simple keyword scanning. US only.
    """
    cache_key = f"nws_synop_{lat}_{lon}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        points_resp = requests.get(
            f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}",
            headers={"User-Agent": "NoRainDashboard/1.0", "Accept": "application/geo+json"},
            timeout=10,
        )
        if points_resp.status_code != 200:
            return None
        forecast_url = points_resp.json().get("properties", {}).get("forecast")
        if not forecast_url:
            return None
        fc = requests.get(
            forecast_url,
            headers={"User-Agent": "NoRainDashboard/1.0", "Accept": "application/geo+json"},
            timeout=10,
        )
        if fc.status_code != 200:
            return None
        periods = fc.json().get("properties", {}).get("periods", [])
        events = []
        for p in periods[:6]:  # next 3 days, day+night
            text = p.get("detailedForecast", "") or ""
            matches = set(m.lower() for m in _FRONT_KEYWORDS.findall(text))
            if matches:
                events.append({
                    "when": p.get("name", ""),
                    "features": sorted(matches),
                    "narrative": text[:200],
                })
        result = {"events": events, "had_data": bool(periods)}
        cache_set(cache_key, result)
        return result
    except Exception as e:
        logger.warning("NWS synoptic fetch failed: %s", e)
    return None


# ─── Lead-time uncertainty ────────────────────────────────────────────────────

def lead_time_sigma_inflation(target_date: str, station: Optional[str] = None) -> float:
    """Return a multiplier for the forecast sigma based on days until
    resolution. When a station is supplied and we have enough historical
    pairings, we fit the curve from data; otherwise fall back to the
    hand-tuned `1.0 + 0.12 * sqrt(days)`. Cap at 3x in either case.
    """
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
        today = datetime.now(timezone.utc).date()
        days = max(0, (target - today).days)
    except Exception:
        return 1.0
    if station:
        try:
            curve = fit_leadtime_curve_for_station(station)
            if curve and curve.get("source") == "fitted":
                return _wcal.leadtime_multiplier(curve, days)
        except Exception:
            pass
    return min(3.0, 1.0 + 0.12 * math.sqrt(days))


# ─── Persistence + analog forecasts ────────────────────────────────────────────

def persistence_forecast(lat: float, lon: float, days_back: int = 1) -> Optional[float]:
    """Naive persistence: use the observed high from N days ago. Beats
    most models in stable regimes."""
    if _open_meteo_in_cooldown():
        return None
    try:
        end = datetime.now(timezone.utc).date() - timedelta(days=1)
        start = end - timedelta(days=days_back)
        resp = requests.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": lat, "longitude": lon,
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit",
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
            timeout=15,
        )
        if resp.status_code == 429:
            _open_meteo_trip_cooldown()
            return None
        if resp.status_code != 200:
            return None
        highs = resp.json().get("daily", {}).get("temperature_2m_max", [])
        if highs and highs[-1] is not None:
            return round(float(highs[-1]), 1)
    except Exception as e:
        logger.warning("Persistence fetch failed: %s", e)
    return None


def analog_forecast(lat: float, lon: float, target_date: str) -> Optional[dict]:
    """Find the past 3 years' high temperatures for the same calendar day
    as the target. The mean is the climatological analog forecast.
    """
    cache_key = f"analog_{lat}_{lon}_{target_date}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    if _open_meteo_in_cooldown():
        return None
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d")
        years_back = 3
        # Pull the same calendar day across the past 3 years
        analogs = []
        for y in range(target.year - years_back, target.year):
            try:
                d = target.replace(year=y).date()
            except ValueError:
                continue  # Feb 29 fallback
            resp = requests.get(
                "https://archive-api.open-meteo.com/v1/archive",
                params={
                    "latitude": lat, "longitude": lon,
                    "daily": "temperature_2m_max",
                    "temperature_unit": "fahrenheit",
                    "start_date": d.isoformat(),
                    "end_date": d.isoformat(),
                },
                timeout=10,
            )
            if resp.status_code == 429:
                _open_meteo_trip_cooldown()
                return None
            if resp.status_code != 200:
                continue
            highs = resp.json().get("daily", {}).get("temperature_2m_max", [])
            if highs and highs[0] is not None:
                analogs.append({"year": y, "high": round(float(highs[0]), 1)})
        if not analogs:
            return None
        vals = [a["high"] for a in analogs]
        result = {
            "mean": round(statistics.mean(vals), 1),
            "min": min(vals),
            "max": max(vals),
            "std": round(statistics.stdev(vals), 1) if len(vals) > 1 else 5.0,
            "years": analogs,
        }
        cache_set(cache_key, result)
        return result
    except Exception as e:
        logger.warning("Analog fetch failed: %s", e)
    return None


# ─── Alpha: Cross-market correlation engine ──────────────────────────────────
#
# When a weather system hits one city, downstream cities along the same storm
# track are likely to experience similar conditions 6-24 hours later. This
# engine tracks those corridors and generates correlated-market alerts.
#

def _haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points in statute miles."""
    R = 3959  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# US weather corridors: typical synoptic propagation paths.
# "speed_mph" is the average speed of the dominant front type along this path.
# Cold fronts: 25-35 mph.  Warm fronts: 10-20 mph.  Ridges/troughs: 15-25 mph.
WEATHER_CORRIDORS = {
    "northeast_track": {
        "cities": ["chicago", "new york"],
        "front_types": ["cold front", "frontal passage", "trough", "low pressure"],
        "speed_mph": 30,
        "label": "NE Storm Track",
    },
    "midwest_to_atlantic": {
        "cities": ["chicago", "atlanta", "miami"],
        "front_types": ["cold front", "frontal passage", "frontal boundary"],
        "speed_mph": 25,
        "label": "Midwest → SE",
    },
    "great_plains_south": {
        "cities": ["denver", "dallas", "houston", "austin"],
        "front_types": ["cold front", "trough", "frontal boundary"],
        "speed_mph": 30,
        "label": "Great Plains Southward",
    },
    "texas_triangle": {
        "cities": ["dallas", "austin", "houston"],
        "front_types": ["cold front", "warm front", "frontal passage", "frontal boundary"],
        "speed_mph": 25,
        "label": "Texas Triangle",
    },
    "gulf_atlantic": {
        "cities": ["houston", "atlanta", "miami"],
        "front_types": ["warm front", "tropical", "frontal boundary"],
        "speed_mph": 20,
        "label": "Gulf → Atlantic Coast",
    },
    "west_coast": {
        "cities": ["seattle", "san francisco", "los angeles"],
        "front_types": ["cold front", "trough", "low pressure", "frontal passage"],
        "speed_mph": 25,
        "label": "Pacific Coast Southward",
    },
    "northeast_corridor": {
        "cities": ["new york", "chicago"],
        "front_types": ["warm front", "high pressure", "ridge"],
        "speed_mph": 20,
        "label": "NE Corridor (warm advection)",
    },
    "rockies_to_plains": {
        "cities": ["denver", "chicago"],
        "front_types": ["cold front", "trough", "frontal passage"],
        "speed_mph": 35,
        "label": "Rockies → Midwest",
    },
}


def _find_corridors_for_city(city_key: str) -> list[dict]:
    """Return all corridors that contain this city, with the city's position
    in the sequence (so we know which direction to look for upstream/downstream)."""
    out = []
    for cid, corridor in WEATHER_CORRIDORS.items():
        cities = corridor["cities"]
        if city_key in cities:
            idx = cities.index(city_key)
            out.append({
                "corridor_id": cid,
                "label": corridor["label"],
                "cities": cities,
                "position": idx,
                "speed_mph": corridor["speed_mph"],
                "front_types": corridor["front_types"],
                # Upstream = cities earlier in the sequence (weather arrives from them)
                "upstream": cities[:idx],
                # Downstream = cities later in the sequence (weather propagates to them)
                "downstream": cities[idx + 1:],
            })
    return out


def compute_cross_correlations(city_key: str, target_date: str) -> list[dict]:
    """Compute cross-market correlation alerts for a city.

    For each corridor the city belongs to, check:
    1. Upstream cities: have they breached thresholds? Is the same synoptic
       feature (front, trough) detected upstream?
    2. Downstream cities: are there active markets that should be warned?

    Returns a list of correlation alerts sorted by relevance.
    """
    corridors = _find_corridors_for_city(city_key)
    if not corridors:
        return []

    station = STATION_MAP.get(city_key)
    if not station:
        return []
    my_lat, my_lon = station[0], station[1]

    alerts = []

    for corr in corridors:
        # Check upstream cities for breaches / synoptic signals
        for up_city in corr["upstream"]:
            up_station = STATION_MAP.get(up_city)
            if not up_station:
                continue
            up_lat, up_lon, up_icao = up_station[0], up_station[1], up_station[2]

            # Distance and estimated propagation time
            dist = _haversine_miles(up_lat, up_lon, my_lat, my_lon)
            eta_hours = round(dist / corr["speed_mph"], 1)

            # Check if upstream city has intraday data for today
            up_intraday = get_intraday_max(up_icao, target_date)
            up_max = up_intraday["running_max"] if up_intraday else None

            # Check upstream synoptic features
            up_synoptic = fetch_nws_synoptic(up_lat, up_lon)
            matched_features = []
            if up_synoptic and up_synoptic.get("events"):
                for ev in up_synoptic["events"]:
                    for feat in ev.get("features", []):
                        if feat in corr["front_types"]:
                            matched_features.append({"feature": feat, "when": ev.get("when", "")})

            if not up_max and not matched_features:
                continue

            alert = {
                "type": "upstream",
                "source_city": up_city,
                "target_city": city_key,
                "corridor": corr["label"],
                "distance_mi": round(dist),
                "eta_hours": eta_hours,
                "source_running_max": up_max,
                "source_obs_count": up_intraday["obs_count"] if up_intraday else 0,
                "synoptic_match": matched_features[:3],
            }

            # Classify the correlation strength
            if matched_features and up_max is not None:
                alert["strength"] = "STRONG"
                front_names = ", ".join(set(f["feature"] for f in matched_features[:2]))
                alert["detail"] = (
                    f"{up_city.title()} running max {up_max:.1f}°F · "
                    f"{front_names} detected · ETA {eta_hours}h ({round(dist)} mi)"
                )
            elif matched_features:
                alert["strength"] = "MODERATE"
                front_names = ", ".join(set(f["feature"] for f in matched_features[:2]))
                alert["detail"] = (
                    f"{front_names} at {up_city.title()} · ETA {eta_hours}h ({round(dist)} mi)"
                )
            elif up_max is not None:
                alert["strength"] = "WEAK"
                alert["detail"] = (
                    f"{up_city.title()} running max {up_max:.1f}°F · "
                    f"~{eta_hours}h propagation ({round(dist)} mi) — no frontal signal yet"
                )
            else:
                continue

            alerts.append(alert)

        # Check downstream cities we might be affecting
        my_intraday = get_intraday_max(station[2], target_date)
        my_max = my_intraday["running_max"] if my_intraday else None
        my_synoptic = fetch_nws_synoptic(my_lat, my_lon)
        my_features = []
        if my_synoptic and my_synoptic.get("events"):
            for ev in my_synoptic["events"]:
                for feat in ev.get("features", []):
                    if feat in corr["front_types"]:
                        my_features.append(feat)

        if not my_max and not my_features:
            continue

        for dn_city in corr["downstream"]:
            dn_station = STATION_MAP.get(dn_city)
            if not dn_station:
                continue
            dn_lat, dn_lon = dn_station[0], dn_station[1]
            dist = _haversine_miles(my_lat, my_lon, dn_lat, dn_lon)
            eta_hours = round(dist / corr["speed_mph"], 1)

            alert = {
                "type": "downstream",
                "source_city": city_key,
                "target_city": dn_city,
                "corridor": corr["label"],
                "distance_mi": round(dist),
                "eta_hours": eta_hours,
                "source_running_max": my_max,
                "synoptic_match": [{"feature": f} for f in set(my_features[:3])],
            }

            if my_features:
                front_names = ", ".join(set(my_features[:2]))
                alert["strength"] = "MODERATE"
                alert["detail"] = (
                    f"This city's {front_names} → {dn_city.title()} in ~{eta_hours}h ({round(dist)} mi)"
                )
            else:
                alert["strength"] = "WEAK"
                alert["detail"] = (
                    f"Same corridor as {dn_city.title()} · ~{eta_hours}h downstream ({round(dist)} mi)"
                )

            alerts.append(alert)

    # Sort: STRONG first, then MODERATE, then WEAK
    order = {"STRONG": 0, "MODERATE": 1, "WEAK": 2}
    alerts.sort(key=lambda a: order.get(a.get("strength", "WEAK"), 3))

    # Deduplicate (same source_city can appear from multiple corridors)
    seen = set()
    deduped = []
    for a in alerts:
        key = (a["type"], a["source_city"], a["target_city"])
        if key not in seen:
            seen.add(key)
            deduped.append(a)
    return deduped


# ─── Alpha: Intraday running-max tracker ─────────────────────────────────────
#
# The highest-impact proprietary signal: if the market asks "Will NYC be above
# 75°F?" and at 2pm the running max at KLGA is already 76°F, we know the answer
# with near-certainty while the market price may still be at 80%.
#
# Strategy:
#   1. Background thread polls METAR every 5 min for all active stations
#   2. We track the running daily max in the `intraday_max` table
#   3. The `/api/intraday/<city>/<date>` endpoint returns:
#      - running_max: highest observed so far today
#      - last_obs: most recent temp
#      - hours_remaining: est. hours of warmth left (sunset)
#      - hrrr_remaining_max: hourly forecast peak for remaining hours
#      - threshold_status: "BREACHED" / "AT RISK" / "SAFE"
#

def update_intraday_max(icao: str):
    """Poll METAR for the station and update the running daily max."""
    obs = fetch_metar(icao)
    if not obs or obs.get("temp_f") is None:
        return None
    temp_f = obs["temp_f"]
    # Determine local date from obs_time
    try:
        obs_ts = obs.get("obs_time")
        if isinstance(obs_ts, (int, float)):
            obs_dt = datetime.fromtimestamp(obs_ts, tz=timezone.utc)
        else:
            obs_dt = datetime.now(timezone.utc)
    except Exception:
        obs_dt = datetime.now(timezone.utc)
    obs_date = obs_dt.strftime("%Y-%m-%d")

    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT running_max, obs_count FROM intraday_max WHERE icao = ? AND obs_date = ?",
                (icao, obs_date),
            ).fetchone()
            if row:
                new_max = max(row["running_max"], temp_f)
                conn.execute(
                    """UPDATE intraday_max
                       SET running_max = ?, last_obs_f = ?, obs_count = obs_count + 1,
                           updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                       WHERE icao = ? AND obs_date = ?""",
                    (new_max, temp_f, icao, obs_date),
                )
                return new_max
            else:
                conn.execute(
                    """INSERT INTO intraday_max (icao, obs_date, running_max, last_obs_f)
                       VALUES (?, ?, ?, ?)""",
                    (icao, obs_date, temp_f, temp_f),
                )
                return temp_f
    except Exception as e:
        logger.warning("update_intraday_max %s: %s", icao, e)
    return None


def get_intraday_max(icao: str, obs_date: str) -> Optional[dict]:
    """Return the current running max for a station today."""
    try:
        with _get_conn(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM intraday_max WHERE icao = ? AND obs_date = ?",
                (icao, obs_date),
            ).fetchone()
        if row:
            return dict(row)
    except Exception:
        pass
    return None


def estimate_hours_remaining(lat: float, lon: float) -> Optional[float]:
    """Rough estimate of warmth-hours remaining today (hours until ~local sunset).
    Uses a simplified solar position calculation.
    """
    now = datetime.now(timezone.utc)
    # Approximate local solar noon offset from UTC
    lon_offset = lon / 15.0  # hours from UTC
    local_solar = now.hour + now.minute / 60 + lon_offset
    # Peak temps typically occur ~2-3h after solar noon (14:00-15:00 local).
    # Temps drop significantly after ~17:00 local (5pm).
    peak_end_local = 17.0  # 5pm local solar time
    remaining = peak_end_local - local_solar
    return max(0.0, round(remaining, 1))


def threshold_status(running_max: float, threshold: dict, hours_remaining: float,
                     forecast_remaining_max: Optional[float] = None) -> dict:
    """Given running max and a market threshold, classify the intraday state.

    threshold is like {"kind": "above", "value": 75} or {"kind": "between", "low": 70, "high": 71}
    """
    kind = threshold.get("kind")
    if kind == "above":
        target = threshold["value"]
        if running_max >= target:
            return {"status": "BREACHED", "detail": f"Running max {running_max:.1f}°F already ≥ {target}°F",
                    "confidence": 0.98}
        gap = target - running_max
        if hours_remaining > 0 and forecast_remaining_max is not None and forecast_remaining_max >= target:
            return {"status": "LIKELY", "detail": f"{gap:.1f}°F below, forecast peak {forecast_remaining_max:.1f}°F still coming",
                    "confidence": 0.75}
        if hours_remaining <= 1 and gap > 3:
            return {"status": "SAFE", "detail": f"{gap:.1f}°F below with {hours_remaining:.1f}h warmth left",
                    "confidence": 0.90}
        return {"status": "AT_RISK", "detail": f"{gap:.1f}°F below, {hours_remaining:.1f}h warmth remaining",
                "confidence": 0.5}

    elif kind == "below":
        target = threshold["value"]
        if hours_remaining <= 0.5 and running_max <= target:
            return {"status": "BREACHED", "detail": f"Running max {running_max:.1f}°F stayed ≤ {target}°F, day ending",
                    "confidence": 0.95}
        if running_max > target:
            return {"status": "SAFE", "detail": f"Already exceeded {target}°F (running max {running_max:.1f}°F), NO wins",
                    "confidence": 0.98}
        return {"status": "AT_RISK", "detail": f"Currently {running_max:.1f}°F ≤ {target}°F, {hours_remaining:.1f}h warmth left",
                "confidence": 0.5}

    elif kind == "between":
        lo, hi = threshold.get("low", 0), threshold.get("high", 999)
        if running_max > hi:
            return {"status": "SAFE", "detail": f"Already above range ({running_max:.1f}°F > {hi}°F), NO wins",
                    "confidence": 0.98}
        if hours_remaining <= 0.5 and lo <= round(running_max) <= hi:
            return {"status": "BREACHED", "detail": f"Running max {running_max:.1f}°F in [{lo}–{hi}] range, day ending",
                    "confidence": 0.85}
        return {"status": "AT_RISK", "detail": f"Running max {running_max:.1f}°F, target [{lo}–{hi}], {hours_remaining:.1f}h left",
                "confidence": 0.5}

    return {"status": "UNKNOWN", "detail": "Cannot parse threshold", "confidence": 0.0}


def _intraday_poll_loop():
    """Background thread: poll METAR every 5 minutes for all stations with
    active markets resolving today. This is what populates the intraday_max
    table and creates the real-time alpha edge.
    """
    import time as _time
    _register_thread("intraday_poll", interval_seconds=300)
    _time.sleep(60)  # Wait 1 min for server to boot
    while True:
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            # Find stations with markets resolving today
            active_icaos = set()
            cached = cache_get("parsed_markets")
            if cached and cached.get("markets"):
                for m in cached["markets"]:
                    td = m.get("target_date", "")
                    city = m.get("city")
                    if td == today and city:
                        s = STATION_MAP.get(city)
                        if s and len(s) > 2:
                            active_icaos.add(s[2])
            # Also poll a baseline set of major US stations
            baseline = {"KLGA", "KORD", "KDAL", "KMIA", "KLAX", "KATL",
                        "KAUS", "KHOU", "KBKF", "KSFO", "KSEA"}
            to_poll = active_icaos | baseline
            for icao in to_poll:
                try:
                    update_intraday_max(icao)
                except Exception as e:
                    logger.warning("intraday poll %s: %s", icao, e)
                _time.sleep(2)  # Stagger requests
            _record_run("intraday_poll", ok=True)
        except Exception as e:
            logger.error("Intraday poll loop error: %s", e)
            _record_run("intraday_poll", ok=False, error=str(e))
        _time.sleep(300)  # 5 minutes


def fetch_forecast(lat: float, lon: float, date_str: str,
                   station: Optional[str] = None) -> Optional[dict]:
    """Wrapper that returns multi-model forecast."""
    return fetch_multi_model_forecast(lat, lon, date_str, station)


def fetch_current_weather(lat: float, lon: float) -> Optional[dict]:
    """Fetch current weather conditions from Open-Meteo."""
    cache_key = f"current_{lat}_{lon}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        resp = requests.get(CURRENT_WEATHER_URL, params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,wind_direction_10m",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
        }, timeout=10)
        if resp.status_code == 200:
            current = resp.json().get("current", {})
            if current:
                data = {
                    "temperature": current.get("temperature_2m"),
                    "apparent_temperature": current.get("apparent_temperature"),
                    "humidity": current.get("relative_humidity_2m"),
                    "precipitation": current.get("precipitation"),
                    "weather_code": current.get("weather_code"),
                    "wind_speed": current.get("wind_speed_10m"),
                    "wind_direction": current.get("wind_direction_10m"),
                    "time": current.get("time"),
                }
                cache_set(cache_key, data)
                return data
    except Exception as e:
        logger.warning("Current weather fetch failed for (%s, %s): %s", lat, lon, e)
    return None


def _c_to_f(c):
    """Convert Celsius to Fahrenheit."""
    return c * 9.0 / 5.0 + 32.0


def compute_probability(forecast: dict, temp_info: dict) -> Optional[float]:
    """Backwards-compatible shim: returns the consensus probability only.

    Internally delegates to `compute_probability_full`, which exposes both
    the Gaussian and empirical-CDF estimates plus a tail-warning flag.
    Callers that want the full breakdown should use that function directly.
    """
    full = compute_probability_full(forecast, temp_info)
    return full.get("probability") if full else None


def compute_probability_full(forecast: dict, temp_info: dict) -> Optional[dict]:
    """Score a market with both Gaussian and empirical-CDF probabilities.

    The empirical reading uses the raw ensemble member temperatures stored
    on the forecast dict (`forecast["ensemble"]`, populated by
    `fetch_multi_model_forecast`). When available it's preferred because
    it captures fat tails the Gaussian fit misses — exactly where the
    dashboard's threshold-edge signals get most aggressive.

    Returns a dict with keys: probability (consensus), gaussian, empirical,
    method, tail_warning, n_members. None if neither path produced a
    number.
    """
    if not forecast:
        return None
    mean = forecast.get("mean")
    std = forecast.get("std")
    members = forecast.get("ensemble") or []
    if mean is None and not members:
        return None
    return _wcal.blended_probability(temp_info, mean, std, members=members)


def categorize_market(question: str, tags: list) -> str:
    q = question.lower()
    if any(k in q for k in ["temperature", "temp", "degrees", "°f", "°c", "ºc", "hottest", "coldest", "warmest"]):
        return "temperature"
    if any(k in q for k in ["hurricane", "tropical storm", "landfall"]):
        return "hurricane"
    if any(k in q for k in ["precipitation", "rain", "snow", "rainfall"]):
        return "precipitation"
    if any(k in q for k in ["arctic", "sea ice", "ice extent"]):
        return "arctic"
    if any(k in q for k in ["earthquake", "megaquake"]):
        return "earthquake"
    if any(k in q for k in ["tornado"]):
        return "tornado"
    if any(k in q for k in ["volcano", "eruption"]):
        return "volcano"
    if any(k in q for k in ["meteor", "asteroid"]):
        return "meteor"
    if any(k in q for k in ["pandemic", "covid", "measles", "coronavirus", "cdc", "cases in"]):
        return "pandemic"
    return "other"


# ─── Signal Logging Helper ────────────────────────────────────────────────────

def log_signal(market_id: str, question: str, category: str,
               yes_price: Optional[float], model_prob: Optional[float],
               edge: Optional[float], action: str = "auto") -> None:
    """Insert a signal into the weather_signals_log table."""
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO weather_signals_log (market_id, question, category, yes_price, model_prob, edge, action) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (market_id, question, category, yes_price, model_prob, edge, action),
            )
    except Exception as e:
        logger.warning("Failed to log signal: %s", e)


# ─── API ───────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json")


@app.route("/sw.js")
def service_worker():
    return send_from_directory("static", "sw.js", mimetype="application/javascript")


@app.route("/icon-192.png")
def icon_192():
    return send_from_directory("static", "icon-192.png")


@app.route("/icon-512.png")
def icon_512():
    return send_from_directory("static", "icon-512.png")


def _parse_market(m):
    """Parse a raw market into a structured dict (no forecast data)."""
    question = m.get("question", "") or m.get("title", "")
    if not question:
        return None
    prices = m.get("outcomePrices", [])
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            prices = []
    yes_price = _safe_float(prices[0], None) if len(prices) > 0 else None
    no_price = _safe_float(prices[1], None) if len(prices) > 1 else None
    city = parse_city(question)
    temp_info = parse_temperature(question)
    target_date = parse_date(question) or parse_date(m.get("_event_title", ""))
    category = categorize_market(question, m.get("_event_tags", []))
    station = None
    if city:
        s = STATION_MAP.get(city)
        if s:
            station = {"lat": s[0], "lon": s[1], "icao": s[2], "name": s[3]}
    has_temp = temp_info["threshold"] is not None or temp_info["temp_lower"] is not None
    market_id = m.get("conditionId") or m.get("id", "")

    desc = m.get("description", "") or ""
    resolution_station = None
    resolution_icao = None
    resolution_source = None
    rs_match = re.search(r"recorded (?:at|by) the (.+?) in degrees", desc)
    if rs_match:
        resolution_station = rs_match.group(1).strip()
    icao_match = re.search(r"wunderground\.com/history/daily/\S+/(\w+)", desc)
    if icao_match:
        resolution_icao = icao_match.group(1)
        resolution_source = "Wunderground"
    elif "Hong Kong Observatory" in desc:
        resolution_source = "HK Observatory"
    elif "weather.gov" in desc or "data.gov" in desc:
        resolution_source = "Government"

    # Extract CLOB token IDs for price history
    clob_tokens = m.get("clobTokenIds")
    if isinstance(clob_tokens, str):
        try:
            clob_tokens = json.loads(clob_tokens)
        except (json.JSONDecodeError, TypeError):
            clob_tokens = None
    yes_token = clob_tokens[0] if clob_tokens and len(clob_tokens) > 0 else None

    return {
        "id": market_id,
        "question": question,
        "slug": m.get("slug", ""),
        "event_title": m.get("_event_title", ""),
        "tags": m.get("_event_tags", []),
        "yes_price": yes_price,
        "no_price": no_price,
        "volume": m.get("volume"),
        "liquidity": m.get("liquidity"),
        "end_date": m.get("endDate"),
        "city": city,
        "target_date": target_date,
        "temp_info": temp_info if has_temp else None,
        "station": station,
        "forecast": None,
        "model_prob": None,
        "edge": None,
        "edge_pct": None,
        "has_forecast": False,
        "weather_prediction": None,
        "category": category,
        "resolution_station": resolution_station,
        "resolution_icao": resolution_icao,
        "resolution_source": resolution_source,
        "source": "polymarket",
        "yes_token": yes_token,
    }


# ─────────────────────────────────────────────────────────────────────────
# FX rates proxy (frankfurter.dev) — cached, USD base
# ─────────────────────────────────────────────────────────────────────────
_FX_CACHE = {"data": None, "fetched_at": 0.0}
_fx_cache_lock = threading.Lock()
_FX_TTL = 3600  # 1 hour
_FX_FALLBACK = {
    "base": "USD",
    "date": "fallback",
    "rates": {
        "USD": 1.0, "EUR": 0.92, "GBP": 0.79, "JPY": 150.0, "AUD": 1.52,
        "CAD": 1.36, "CHF": 0.88, "CNY": 7.20, "HKD": 7.83, "NZD": 1.65,
        "SEK": 10.5, "KRW": 1340.0, "SGD": 1.34, "NOK": 10.6, "MXN": 17.0,
        "INR": 83.0, "ZAR": 18.5, "TRY": 32.0, "BRL": 5.0, "DKK": 6.85,
        "PLN": 3.95, "THB": 35.0, "IDR": 15700.0, "HUF": 360.0, "CZK": 23.0,
        "ILS": 3.7, "PHP": 56.0, "MYR": 4.7, "RON": 4.6, "ISK": 137.0,
    },
}


@app.route("/api/fx-rates")
def api_fx_rates():
    """Return USD-base FX rates, cached for 1h. Source: frankfurter.dev."""
    now = time.time()
    with _fx_cache_lock:
        cached = _FX_CACHE["data"]
        if cached and (now - _FX_CACHE["fetched_at"]) < _FX_TTL:
            return jsonify(cached)
    try:
        r = requests.get(
            "https://api.frankfurter.dev/v1/latest?base=USD",
            timeout=5,
        )
        if r.status_code == 200:
            data = r.json()
            data.setdefault("rates", {})
            data["rates"]["USD"] = 1.0
            with _fx_cache_lock:
                _FX_CACHE["data"] = data
                _FX_CACHE["fetched_at"] = now
            return jsonify(data)
    except Exception as e:
        logging.warning("FX rate fetch failed: %s", e)
    with _fx_cache_lock:
        cached = _FX_CACHE["data"]
    if cached:
        return jsonify(cached)
    return jsonify(_FX_FALLBACK)


@app.route("/api/markets")
@require_auth
def api_markets():
    """Fast endpoint: returns market data without forecasts."""
    cached = cache_get("parsed_markets")
    if cached is not None:
        return jsonify(cached)

    # Polymarket
    raw_markets = fetch_all_weather_markets()
    enriched = [e for e in (_parse_market(m) for m in raw_markets) if e is not None]

    # Kalshi
    try:
        kalshi_raw = fetch_kalshi_weather_markets()
        # Per-market try/except so one malformed entry can't kill the batch.
        kalshi_parsed = []
        for km in kalshi_raw:
            try:
                parsed = _parse_kalshi_market(km)
            except Exception as parse_err:
                logger.warning("Skipped malformed Kalshi market %s: %s",
                               (km or {}).get("ticker", "?"), parse_err)
                continue
            if parsed is not None:
                kalshi_parsed.append(parsed)
        enriched.extend(kalshi_parsed)
        logger.info("Merged %d Kalshi markets into response", len(kalshi_parsed))
    except Exception as e:
        logger.error("Kalshi fetch failed, continuing with Polymarket only: %s", e)

    def _sort_key(x):
        try:
            return -float(x.get("volume") or 0)
        except (TypeError, ValueError):
            return 0.0
    enriched.sort(key=_sort_key)

    result = {"markets": enriched, "count": len(enriched),
              "timestamp": datetime.now(timezone.utc).isoformat()}
    cache_set("parsed_markets", result)
    return jsonify(result)


@app.route("/api/forecasts")
@require_auth
def api_forecasts():
    """Slower endpoint: returns multi-model forecasts for all city+date combos, with probabilities."""
    cached = cache_get("all_forecasts")
    if cached is not None:
        return jsonify(cached)

    # Collect unique city+date+temp_info combos from BOTH sources
    forecast_needs: dict[str, tuple] = {}  # key -> (lat, lon, date, city)
    market_temps: dict[str, list] = {}  # key -> list of (market_id, yes_price, temp_info)

    # --- Polymarket raw markets ---
    raw_markets = fetch_all_weather_markets()
    for m in raw_markets:
        question = m.get("question", "") or m.get("title", "")
        if not question:
            continue
        city = parse_city(question)
        if not city:
            continue
        s = STATION_MAP.get(city)
        if not s:
            continue
        temp_info = parse_temperature(question)
        has_temp = temp_info["threshold"] is not None or temp_info["temp_lower"] is not None
        if not has_temp:
            continue
        target_date = parse_date(question) or parse_date(m.get("_event_title", ""))
        if not target_date:
            continue

        fc_key = f"{city}:{target_date}"
        if fc_key not in forecast_needs:
            forecast_needs[fc_key] = (s[0], s[1], target_date, city)
            market_temps[fc_key] = []

        prices = m.get("outcomePrices", [])
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except json.JSONDecodeError:
                prices = []
        yes_price = _safe_float(prices[0], None) if len(prices) > 0 else None
        market_id = m.get("conditionId") or m.get("id", "")
        market_temps[fc_key].append((market_id, yes_price, temp_info))

    # --- Kalshi markets (reuse parsed cache from api_markets when available) ---
    try:
        _cached_markets = cache_get("parsed_markets")
        if _cached_markets and _cached_markets.get("markets"):
            _kalshi_parsed = [m for m in _cached_markets["markets"] if m.get("source") == "kalshi"]
        else:
            _kalshi_parsed = []
            for km in fetch_kalshi_weather_markets():
                try:
                    parsed = _parse_kalshi_market(km)
                except Exception as parse_err:
                    logger.warning("Skipped malformed Kalshi market %s: %s",
                                   (km or {}).get("ticker", "?"), parse_err)
                    continue
                if parsed is not None:
                    _kalshi_parsed.append(parsed)
        for parsed in _kalshi_parsed:
            if not parsed or not parsed.get("city") or not parsed.get("target_date") or not parsed.get("temp_info"):
                continue
            city = parsed["city"]
            s = STATION_MAP.get(city)
            if not s:
                continue
            fc_key = f"{city}:{parsed['target_date']}"
            if fc_key not in forecast_needs:
                forecast_needs[fc_key] = (s[0], s[1], parsed["target_date"], city)
                market_temps[fc_key] = []
            market_temps[fc_key].append((parsed["id"], parsed["yes_price"], parsed["temp_info"]))
    except Exception as e:
        logger.error("Kalshi forecasts merge failed: %s", e)

    # Parallel fetch all forecasts.
    # Each city+date triggers ~10 inner API calls (8 ensemble models + NWS + climo).
    # Keep outer pool small (3) so peak concurrency is ~30, well under
    # Open-Meteo's 600/min limit and leaving headroom for /api/weather_history.
    from concurrent.futures import ThreadPoolExecutor

    def _fetch_fc(args):
        key, payload = args
        # Backward compatibility for older 3-tuple format if any cache hit
        if len(payload) == 4:
            lat, lon, date, city = payload
        else:
            lat, lon, date = payload
            city = None
        return key, fetch_multi_model_forecast(lat, lon, date, station=city)

    forecast_data: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_fetch_fc, item): item[0] for item in forecast_needs.items()}
        for future in futures:
            try:
                key, fc = future.result()
                if fc:
                    forecast_data[key] = fc
            except Exception as e:
                failed_key = futures[future]
                logger.warning("Forecast fetch failed for %s: %s", failed_key, e)

    # Build per-market enrichment: forecast + per-model probabilities
    market_enrichments = {}
    for fc_key, fc in forecast_data.items():
        for (market_id, yes_price, temp_info) in market_temps.get(fc_key, []):
            # Consensus probability
            consensus_prob = compute_probability(fc, temp_info)
            edge = None
            edge_pct = None
            if consensus_prob is not None and yes_price is not None and yes_price > 0:
                edge = round(consensus_prob - yes_price, 4)
                edge_pct = round(edge * 100, 1)

            # Per-model probabilities
            per_model = {}
            for model_id, mdata in fc.get("models", {}).items():
                mp = compute_probability(mdata, temp_info)
                if mp is not None:
                    me = None
                    if yes_price and yes_price > 0:
                        me = round((mp - yes_price) * 100, 1)
                    per_model[model_id] = {
                        "name": mdata.get("model_name", model_id),
                        "org": mdata.get("org", ""),
                        "mean": mdata["mean"],
                        "std": mdata["std"],
                        "prob": round(mp, 4),
                        "edge_pct": me,
                        "members": mdata.get("members", 0),
                        "is_resolution": mdata.get("is_resolution_model", False),
                    }

            market_enrichments[market_id] = {
                "model_prob": consensus_prob,
                "edge": edge,
                "edge_pct": edge_pct,
                "weather_prediction": f"{fc['mean']}°F (±{fc['std']}°F)",
                "forecast_mean": fc["mean"],
                "forecast_std": fc["std"],
                "forecast_min": fc["min"],
                "forecast_max": fc["max"],
                "models": per_model,
                "model_count": len(per_model),
            }

            # Log signals
            if edge is not None and abs(edge) > 0.05:
                action = "BUY_YES" if edge > 0 else "BUY_NO"
                question = ""
                for rm in raw_markets:
                    if (rm.get("conditionId") or rm.get("id", "")) == market_id:
                        question = rm.get("question", "")
                        break
                # If not found in Polymarket raw data, search Kalshi parsed markets
                if not question and market_id.startswith("kalshi_"):
                    try:
                        for km in _kalshi_parsed:
                            if km and km.get("id") == market_id:
                                question = km.get("question", "")
                                break
                    except NameError:
                        pass
                category = "temperature"
                log_signal(market_id, question, category, yes_price, consensus_prob, edge, action)

    result = {"forecasts": market_enrichments, "count": len(market_enrichments),
              "timestamp": datetime.now(timezone.utc).isoformat()}
    # Don't cache empty results — force a retry on next request
    if market_enrichments:
        cache_set("all_forecasts", result)

    # Take a price snapshot after enrichment
    try:
        import threading as _th
        _th.Thread(target=snapshot_prices, daemon=True).start()
    except Exception:
        pass

    return jsonify(result)


@app.route("/api/forecast/<city>/<date>")
@require_auth
def api_forecast(city, date):
    # Validate date format
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return jsonify({"error": "Invalid date format, expected YYYY-MM-DD"}), 400
    city_key = CITY_ALIASES.get(city.lower(), city.lower())
    station = STATION_MAP.get(city_key)
    if not station:
        return jsonify({"error": f"Unknown city: {city}"}), 404
    forecast = fetch_forecast(station[0], station[1], date, station=city_key)
    if not forecast:
        return jsonify({"error": "Forecast not available"}), 404
    return jsonify({
        "city": city_key,
        "station": {"lat": station[0], "lon": station[1], "icao": station[2], "name": station[3]},
        "date": date,
        "forecast": forecast,
    })


# ─── New: Live observation, synoptic, analog, spread trend, ENSO ───────────────

@app.route("/api/metar/<icao>")
@require_auth
def api_metar(icao):
    """Latest METAR for an airport (the actual resolution station)."""
    icao = (icao or "").upper().strip()
    if not re.match(r'^[A-Z]{4}$', icao):
        return jsonify({"error": "Invalid ICAO code"}), 400
    obs = fetch_metar(icao)
    if obs is None:
        return jsonify({"error": "METAR unavailable"}), 503
    return jsonify(obs)


@app.route("/api/hourly/<city>/<date>")
@require_auth
def api_hourly(city, date):
    """Hourly forecast at the resolution station for the target date."""
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return jsonify({"error": "Invalid date format"}), 400
    city_key = CITY_ALIASES.get(city.lower(), city.lower())
    station = STATION_MAP.get(city_key)
    if not station:
        return jsonify({"error": f"Unknown city: {city}"}), 404
    hourly = fetch_hourly_at_station(station[0], station[1], date)
    if hourly is None:
        return jsonify({"error": "Hourly forecast unavailable"}), 503
    return jsonify(hourly)


@app.route("/api/synoptic/<city>")
@require_auth
def api_synoptic(city):
    """NWS narrative-extracted synoptic features (fronts, pressure systems)
    for a US city. Returns nothing useful for international cities."""
    city_key = CITY_ALIASES.get(city.lower(), city.lower())
    station = STATION_MAP.get(city_key)
    if not station:
        return jsonify({"error": f"Unknown city: {city}"}), 404
    syn = fetch_nws_synoptic(station[0], station[1])
    return jsonify({"city": city_key, "synoptic": syn or {"events": [], "had_data": False}})


@app.route("/api/enso")
@require_auth
def api_enso():
    """Current ENSO phase (El Niño / La Niña / Neutral) + AO/NAO indices."""
    enso = fetch_enso_state()
    teleconn = fetch_teleconnections()
    return jsonify({
        "enso": enso,
        "teleconnections": teleconn or {},
    })


@app.route("/api/analog/<city>/<date>")
@require_auth
def api_analog(city, date):
    """Persistence (yesterday's high) + 3-year historical analog forecast
    for the target date. Two model-free baselines for comparison."""
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return jsonify({"error": "Invalid date format"}), 400
    city_key = CITY_ALIASES.get(city.lower(), city.lower())
    station = STATION_MAP.get(city_key)
    if not station:
        return jsonify({"error": f"Unknown city: {city}"}), 404
    persistence = persistence_forecast(station[0], station[1])
    analog = analog_forecast(station[0], station[1], date)
    return jsonify({
        "city": city_key,
        "date": date,
        "persistence_high": persistence,
        "analog": analog,
    })


@app.route("/api/spread_trend/<city>/<date>")
@require_auth
def api_spread_trend(city, date):
    """Recent ensemble snapshots so the frontend can render a sparkline of
    how consensus has been evolving for this market."""
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return jsonify({"error": "Invalid date format"}), 400
    city_key = CITY_ALIASES.get(city.lower(), city.lower())
    if city_key not in STATION_MAP:
        return jsonify({"error": f"Unknown city: {city}"}), 404
    snaps = get_spread_trend(city_key, date)
    biases = get_model_biases(city_key)
    return jsonify({
        "city": city_key,
        "date": date,
        "snapshots": snaps,
        "model_biases": biases,
    })


@app.route("/api/coastal/<city>")
@require_auth
def api_coastal(city):
    """Coastal flow indicator (onshore/offshore/alongshore) for cities where
    marine layer or sea-breeze regimes affect the temperature forecast."""
    city_key = CITY_ALIASES.get(city.lower(), city.lower())
    station = STATION_MAP.get(city_key)
    if not station:
        return jsonify({"error": f"Unknown city: {city}"}), 404
    if city_key not in COASTAL_CITIES:
        return jsonify({"city": city_key, "coastal": None})
    metar = fetch_metar(station[2]) if len(station) > 2 else None
    wind_dir = (metar or {}).get("wind_dir")
    flow = coastal_flow_assessment(city_key, wind_dir)
    return jsonify({
        "city": city_key,
        "coastal": flow,
        "wind_obs_from_metar": bool(metar),
    })


@app.route("/api/market_signals/<city>/<date>")
@require_auth
def api_market_signals(city, date):
    """Bundle ALL the new signals for a market in one round-trip:
    METAR, hourly, synoptic, analog/persistence, spread trend, coastal,
    ENSO state, and current model biases. The frontend modal calls this
    instead of hitting six separate endpoints.
    """
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return jsonify({"error": "Invalid date format"}), 400
    city_key = CITY_ALIASES.get(city.lower(), city.lower())
    station = STATION_MAP.get(city_key)
    if not station:
        return jsonify({"error": f"Unknown city: {city}"}), 404
    lat, lon, icao, station_name = station[0], station[1], station[2], station[3]

    # Fetch everything in parallel — all of these are independent.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    payload: dict = {
        "city": city_key,
        "date": date,
        "station": {"lat": lat, "lon": lon, "icao": icao, "name": station_name},
    }
    tasks = {
        "metar": lambda: fetch_metar(icao),
        "hourly": lambda: fetch_hourly_at_station(lat, lon, date),
        "synoptic": lambda: fetch_nws_synoptic(lat, lon),
        "persistence_high": lambda: persistence_forecast(lat, lon),
        "analog": lambda: analog_forecast(lat, lon, date),
        "spread_trend": lambda: get_spread_trend(city_key, date),
        "biases": lambda: get_model_biases(city_key),
        "enso": fetch_enso_state,
        "teleconnections": fetch_teleconnections,
        "intraday": lambda: (update_intraday_max(icao), get_intraday_max(icao, date))[1],
        "correlations": lambda: compute_cross_correlations(city_key, date),
    }
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futs = {pool.submit(fn): name for name, fn in tasks.items()}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                payload[name] = fut.result()
            except Exception as e:
                logger.warning("market_signals.%s failed: %s", name, e)
                payload[name] = None

    # Coastal flow needs the wind dir from METAR, so derive it after.
    if city_key in COASTAL_CITIES:
        wind_dir = (payload.get("metar") or {}).get("wind_dir")
        payload["coastal"] = coastal_flow_assessment(city_key, wind_dir)
    else:
        payload["coastal"] = None

    # Add hours-remaining to intraday data
    if payload.get("intraday"):
        payload["intraday"]["hours_remaining"] = estimate_hours_remaining(lat, lon)

    return jsonify(payload)


@app.route("/api/correlations/<city>/<date>")
@require_auth
def api_correlations(city, date):
    """Cross-market weather correlations.

    Returns upstream/downstream correlation alerts based on synoptic
    features propagating along known weather corridors.
    """
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return jsonify({"error": "Invalid date format"}), 400
    city_key = CITY_ALIASES.get(city.lower(), city.lower())
    if city_key not in STATION_MAP:
        return jsonify({"error": f"Unknown city: {city}"}), 404
    correlations = compute_cross_correlations(city_key, date)
    corridors = _find_corridors_for_city(city_key)
    return jsonify({
        "city": city_key,
        "date": date,
        "corridors": [{"id": c["corridor_id"], "label": c["label"],
                       "upstream": c["upstream"], "downstream": c["downstream"]}
                      for c in corridors],
        "correlations": correlations,
        "count": len(correlations),
    })


@app.route("/api/intraday/<city>/<date>")
@require_auth
def api_intraday(city, date):
    """Alpha endpoint: intraday running-max tracker.

    Returns the highest temperature observed so far today at the resolution
    station, how many hours of warmth remain, and whether the market's
    threshold has already been breached.
    """
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return jsonify({"error": "Invalid date format"}), 400
    city_key = CITY_ALIASES.get(city.lower(), city.lower())
    station = STATION_MAP.get(city_key)
    if not station:
        return jsonify({"error": f"Unknown city: {city}"}), 404
    lat, lon, icao = station[0], station[1], station[2]

    # Trigger a fresh METAR poll + update (sub-second)
    update_intraday_max(icao)
    intraday = get_intraday_max(icao, date)
    hours_left = estimate_hours_remaining(lat, lon)

    # Try to get the hourly forecast peak for remaining hours
    remaining_peak = None
    hourly = fetch_hourly_at_station(lat, lon, date)
    if hourly and hourly.get("max_f") is not None:
        remaining_peak = hourly["max_f"]

    result = {
        "city": city_key,
        "date": date,
        "icao": icao,
        "running_max": intraday["running_max"] if intraday else None,
        "last_obs_f": intraday["last_obs_f"] if intraday else None,
        "obs_count": intraday["obs_count"] if intraday else 0,
        "updated_at": intraday["updated_at"] if intraday else None,
        "hours_remaining": hours_left,
        "hourly_forecast_peak": remaining_peak,
    }
    return jsonify(result)


@app.route("/api/intraday_alert/<city>/<date>")
@require_auth
def api_intraday_alert(city, date):
    """Check all markets for this city+date and flag threshold status.

    Returns per-market intraday resolution confidence. This is the "money
    signal": if status is BREACHED, the market outcome is already determined.
    """
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return jsonify({"error": "Invalid date format"}), 400
    city_key = CITY_ALIASES.get(city.lower(), city.lower())
    station = STATION_MAP.get(city_key)
    if not station:
        return jsonify({"error": f"Unknown city: {city}"}), 404
    lat, lon, icao = station[0], station[1], station[2]

    update_intraday_max(icao)
    intraday = get_intraday_max(icao, date)
    if not intraday:
        return jsonify({"city": city_key, "date": date, "alerts": [], "no_data": True})

    running_max = intraday["running_max"]
    hours_left = estimate_hours_remaining(lat, lon)
    remaining_peak = None
    hourly = fetch_hourly_at_station(lat, lon, date)
    if hourly:
        remaining_peak = hourly.get("max_f")

    # Find all markets for this city+date
    cached = cache_get("parsed_markets")
    markets = cached.get("markets", []) if cached else []
    alerts = []
    for m in markets:
        if m.get("city") != city_key or m.get("target_date") != date:
            continue
        ti = m.get("temp_info", {})
        if not ti:
            continue
        # Build threshold dict from temp_info
        thresh = {}
        if ti.get("threshold") is not None:
            if ti.get("is_over") is True:
                thresh = {"kind": "above", "value": ti["threshold"]}
            elif ti.get("is_over") is False:
                thresh = {"kind": "below", "value": ti["threshold"]}
        elif ti.get("temp_lower") is not None and ti.get("temp_upper") is not None:
            thresh = {"kind": "between", "low": ti["temp_lower"], "high": ti["temp_upper"]}
        if not thresh:
            continue
        status = threshold_status(running_max, thresh, hours_left, remaining_peak)
        alerts.append({
            "market_id": m.get("id"),
            "question": m.get("question"),
            "yes_price": m.get("yes_price"),
            "threshold": thresh,
            "running_max": running_max,
            "hours_remaining": hours_left,
            **status,
        })

    # Sort: BREACHED first, then LIKELY, then by confidence desc
    order = {"BREACHED": 0, "LIKELY": 1, "SAFE": 2, "AT_RISK": 3, "UNKNOWN": 4}
    alerts.sort(key=lambda a: (order.get(a["status"], 5), -a.get("confidence", 0)))

    return jsonify({
        "city": city_key,
        "date": date,
        "icao": icao,
        "running_max": running_max,
        "hours_remaining": hours_left,
        "alerts": alerts,
        "count": len(alerts),
    })


@app.route("/api/weather_history/<city>")
@require_auth
def api_weather_history(city):
    """Return daily weather data for a city over the past 3 years.

    Uses Open-Meteo's historical archive API. Returns comprehensive daily
    metrics: temps, precipitation, snow, wind, humidity, sunshine, cloud
    cover, pressure, UV, and dew point.
    """
    city_key = CITY_ALIASES.get(city.lower(), city.lower())
    station = STATION_MAP.get(city_key)
    if not station:
        return jsonify({"error": f"Unknown city: {city}"}), 404

    lat, lon = station[0], station[1]
    cache_key = f"wxhist3y_{city_key}"
    cached = cache_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    try:
        end_date = datetime.now(timezone.utc).date() - timedelta(days=1)
        start_date = end_date - timedelta(days=3 * 365)

        daily_vars = ",".join([
            "temperature_2m_max", "temperature_2m_min", "temperature_2m_mean",
            "apparent_temperature_max", "apparent_temperature_min",
            "precipitation_sum", "rain_sum", "snowfall_sum",
            "precipitation_hours",
            "weather_code",
            "wind_speed_10m_max", "wind_gusts_10m_max", "wind_direction_10m_dominant",
            "shortwave_radiation_sum", "et0_fao_evapotranspiration",
        ])

        archive_params = {
            "latitude": lat,
            "longitude": lon,
            "daily": daily_vars,
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        if _open_meteo_in_cooldown():
            with _open_meteo_cooldown_lock:
                wait_s = max(1, int(_open_meteo_cooldown_until - time.time()))
            return jsonify({
                "error": "Open-Meteo rate-limited",
                "retry_after": wait_s,
            }), 503
        resp = None
        for attempt in range(3):
            try:
                resp = requests.get(
                    "https://archive-api.open-meteo.com/v1/archive",
                    params=archive_params,
                    timeout=30,
                )
                if resp.status_code == 429:
                    _open_meteo_trip_cooldown()
                    return jsonify({
                        "error": "Open-Meteo rate-limited",
                        "retry_after": 60,
                    }), 503
                if resp.status_code == 200:
                    break
            except requests.RequestException as ex:
                logger.warning("Weather archive attempt %d failed: %s", attempt + 1, ex)
                resp = None
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
        if resp is None or resp.status_code != 200:
            return jsonify({"error": "Weather archive unavailable"}), 502

        daily = resp.json().get("daily", {})
        dates = daily.get("time", [])
        if not dates:
            return jsonify({"error": "No data returned"}), 404

        def _val(key, i):
            arr = daily.get(key, [])
            return arr[i] if i < len(arr) and arr[i] is not None else None

        days = []
        for i, d in enumerate(dates):
            days.append({
                "date": d,
                "high": _val("temperature_2m_max", i),
                "low": _val("temperature_2m_min", i),
                "mean": _val("temperature_2m_mean", i),
                "feels_high": _val("apparent_temperature_max", i),
                "feels_low": _val("apparent_temperature_min", i),
                "precip": _val("precipitation_sum", i),
                "rain": _val("rain_sum", i),
                "snow": _val("snowfall_sum", i),
                "precip_hrs": _val("precipitation_hours", i),
                "weather_code": _val("weather_code", i),
                "wind_max": _val("wind_speed_10m_max", i),
                "wind_gust": _val("wind_gusts_10m_max", i),
                "wind_dir": _val("wind_direction_10m_dominant", i),
                "solar": _val("shortwave_radiation_sum", i),
                "et0": _val("et0_fao_evapotranspiration", i),
            })

        # Compute summary stats
        def _stats(key):
            vals = [d[key] for d in days if d.get(key) is not None]
            if not vals:
                return None
            return {
                "avg": round(statistics.mean(vals), 1),
                "min": round(min(vals), 1),
                "max": round(max(vals), 1),
                "std": round(statistics.stdev(vals), 1) if len(vals) > 1 else 0,
            }

        precips = [d["precip"] for d in days if d.get("precip") is not None]
        snows = [d["snow"] for d in days if d.get("snow") is not None]

        # Monthly averages
        monthly = {}
        for d in days:
            month = d["date"][:7]  # YYYY-MM
            if month not in monthly:
                monthly[month] = {"highs": [], "lows": [], "precip": [], "snow": []}
            if d.get("high") is not None:
                monthly[month]["highs"].append(d["high"])
            if d.get("low") is not None:
                monthly[month]["lows"].append(d["low"])
            if d.get("precip") is not None:
                monthly[month]["precip"].append(d["precip"])
            if d.get("snow") is not None:
                monthly[month]["snow"].append(d["snow"])

        monthly_summary = []
        for month, vals in sorted(monthly.items()):
            monthly_summary.append({
                "month": month,
                "avg_high": round(statistics.mean(vals["highs"]), 1) if vals["highs"] else None,
                "avg_low": round(statistics.mean(vals["lows"]), 1) if vals["lows"] else None,
                "total_precip": round(sum(vals["precip"]), 2) if vals["precip"] else 0,
                "total_snow": round(sum(vals["snow"]), 2) if vals["snow"] else 0,
                "days": len(vals["highs"]),
            })

        result = {
            "city": city_key,
            "station": {"lat": lat, "lon": lon, "icao": station[2], "name": station[3]},
            "days": days,
            "monthly": monthly_summary,
            "summary": {
                "temp_high": _stats("high"),
                "temp_low": _stats("low"),
                "temp_mean": _stats("mean"),
                "feels_high": _stats("feels_high"),
                "wind_max": _stats("wind_max"),
                "wind_gust": _stats("wind_gust"),
                "solar": _stats("solar"),
                "total_precip": round(sum(precips), 2) if precips else None,
                "total_snow": round(sum(snows), 2) if snows else None,
                "rainy_days": sum(1 for p in precips if p and p > 0.01),
                "snow_days": sum(1 for s in snows if s and s > 0.01),
                "period": f"{dates[0]} to {dates[-1]}",
                "total_days": len(days),
            },
        }
        cache_set(cache_key, result)
        return jsonify(result)
    except Exception as e:
        logger.error("Weather history fetch failed for %s: %s", city_key, e)
        return jsonify({"error": "Failed to fetch weather history"}), 500


@app.route("/api/stations")
@require_auth
def api_stations():
    stations = []
    seen: set[str] = set()
    for city, (lat, lon, icao, name) in STATION_MAP.items():
        if icao not in seen:
            seen.add(icao)
            current = fetch_current_weather(lat, lon)
            stations.append({
                "city": city, "lat": lat, "lon": lon, "icao": icao, "name": name,
                "current_weather": current,
            })
    return jsonify({"stations": stations})


# ─── History & Accuracy Endpoints ─────────────────────────────────────────────

@app.route("/api/history")
@require_auth
def api_history():
    """Return recent signals with pagination. Query params: page, per_page, category, period."""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    category = request.args.get("category", None)
    period = request.args.get("period", None)
    per_page = min(per_page, 200)
    sb_offset = (page - 1) * per_page

    with _get_conn(readonly=True) as conn:
        where_clauses = []
        params = []
        if category:
            where_clauses.append("category = ?")
            params.append(category)
        if period and period != "all":
            period_map = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
            delta = period_map.get(period)
            if delta:
                cutoff = (datetime.now(timezone.utc) - delta).isoformat()
                where_clauses.append("timestamp >= ?")
                params.append(cutoff)

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        total = conn.execute(
            f"SELECT COUNT(*) FROM weather_signals_log{where_sql}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"SELECT * FROM weather_signals_log{where_sql} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params + [per_page, sb_offset],
        ).fetchall()
        signals = [dict(r) for r in rows]

    return jsonify({
        "signals": signals,
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": (total + per_page - 1) // per_page if per_page > 0 else 0,
    })


@app.route("/api/accuracy")
@require_auth
def api_accuracy():
    """Compute accuracy stats: win rate, avg edge, by category."""
    with _get_conn(readonly=True) as conn:
        rows = conn.execute("SELECT market_id, edge, category FROM weather_signals_log").fetchall()
        all_signals = [dict(r) for r in rows]

        rows = conn.execute("SELECT market_id, actual_outcome, payout FROM weather_resolutions").fetchall()
        all_resolutions = [dict(r) for r in rows]
    res_map = {}
    for r in all_resolutions:
        res_map[r["market_id"]] = r

    # Overall stats
    total_signals = len(all_signals)
    edges = [s["edge"] for s in all_signals if s.get("edge") is not None]
    avg_edge = round(statistics.mean(edges), 4) if edges else None
    avg_abs_edge = round(statistics.mean([abs(e) for e in edges]), 4) if edges else None

    # Resolved stats
    total_resolved = 0
    wins = 0
    payouts = []
    for s in all_signals:
        r = res_map.get(s["market_id"])
        if r and r.get("actual_outcome"):
            total_resolved += 1
            if r.get("payout") is not None:
                payouts.append(r["payout"])
            edge = s.get("edge") or 0
            outcome = r["actual_outcome"]
            if (edge > 0 and outcome == "YES") or (edge < 0 and outcome == "NO"):
                wins += 1

    win_rate = round(wins / total_resolved, 4) if total_resolved > 0 else None
    avg_payout = round(statistics.mean(payouts), 4) if payouts else None

    # By category
    cat_data = {}
    for s in all_signals:
        cat = s.get("category") or "other"
        if cat not in cat_data:
            cat_data[cat] = {"category": cat, "signal_count": 0, "edges": [],
                             "resolved_count": 0, "wins": 0}
        cat_data[cat]["signal_count"] += 1
        if s.get("edge") is not None:
            cat_data[cat]["edges"].append(s["edge"])
        r = res_map.get(s["market_id"])
        if r and r.get("actual_outcome"):
            cat_data[cat]["resolved_count"] += 1
            edge = s.get("edge") or 0
            outcome = r["actual_outcome"]
            if (edge > 0 and outcome == "YES") or (edge < 0 and outcome == "NO"):
                cat_data[cat]["wins"] += 1

    categories = []
    for cat in sorted(cat_data.values(), key=lambda x: -x["signal_count"]):
        e = cat["edges"]
        rc = cat["resolved_count"]
        w = cat["wins"]
        categories.append({
            "category": cat["category"],
            "signal_count": cat["signal_count"],
            "avg_edge": round(statistics.mean(e), 4) if e else None,
            "avg_abs_edge": round(statistics.mean([abs(x) for x in e]), 4) if e else None,
            "resolved_count": rc,
            "wins": w,
            "win_rate": round(w / rc, 4) if rc > 0 else None,
        })

    return jsonify({
        "overall": {
            "total_signals": total_signals,
            "avg_edge": avg_edge,
            "avg_abs_edge": avg_abs_edge,
            "total_resolved": total_resolved,
            "wins": wins,
            "win_rate": win_rate,
            "avg_payout": avg_payout,
        },
        "by_category": categories,
    })


@app.route("/api/healthz")
def api_healthz():
    """Operational health snapshot for the background loops.

    Returns one entry per registered loop with `last_attempt_at`,
    `last_success_at`, `consecutive_failures`, total counts, and a
    derived `stale` flag (true if the loop has missed >2 intervals).

    Public — no auth required so external monitors can poll it.
    """
    snap = _thread_health_snapshot()
    any_stale = any(v.get("stale") for v in snap.values())
    any_error = any(v.get("consecutive_failures", 0) > 0 for v in snap.values())
    return jsonify({
        "status": "stale" if any_stale else ("degraded" if any_error else "ok"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "threads": snap,
    })


@app.route("/api/calibration")
@require_auth
def api_calibration():
    """Model calibration over the last N days of resolved signals.

    Joins `weather_signals_log` with `weather_resolutions` and returns
    Brier score, log loss, and a 10-bucket reliability diagram. This is
    the dashboard's answer to "is the model actually well-calibrated?".

    Query params:
        days  — lookback window (default 90, max 365)
    """
    days = max(7, min(365, int(request.args.get("days", 90))))
    try:
        with _get_conn(readonly=True) as conn:
            rows = conn.execute(
                """SELECT s.market_id, s.model_prob, s.edge, s.category, r.actual_outcome
                   FROM weather_signals_log s
                   JOIN weather_resolutions r ON r.market_id = s.market_id
                   WHERE r.actual_outcome IN ('YES','NO')
                     AND s.timestamp >= datetime('now', ?)
                     AND s.model_prob IS NOT NULL""",
                (f"-{days} days",),
            ).fetchall()
    except Exception as e:
        logger.warning("api_calibration query failed: %s", e)
        rows = []

    preds = [float(r["model_prob"]) for r in rows if r["model_prob"] is not None]
    outcomes = [1 if r["actual_outcome"] == "YES" else 0 for r in rows
                if r["model_prob"] is not None]

    return jsonify({
        "days": days,
        "n": len(preds),
        "brier_score": round(_wcal.brier_score(preds, outcomes) or 0.0, 4) if preds else None,
        "log_loss": round(_wcal.log_loss(preds, outcomes) or 0.0, 4) if preds else None,
        "reliability": _wcal.reliability_diagram(preds, outcomes, n_bins=10),
    })


@app.route("/api/leadtime_fit/<city>")
@require_auth
def api_leadtime_fit(city):
    """Per-city fitted lead-time sigma curve from forecast_history.

    Exposes the curve so users (and the frontend) can see whether the
    fit is data-driven (`source: fitted`) or fell back to the hand-tuned
    constant (`source: default`). Useful for spot-checking the model and
    for showing per-city skill curves on the dashboard.
    """
    city_key = (city or "").lower().strip()
    info = STATION_MAP.get(city_key)
    if not info:
        return jsonify({"error": "unknown city"}), 404
    fit = fit_leadtime_curve_for_station(city_key)
    residuals = get_model_residuals(city_key)
    return jsonify({
        "city": city_key,
        "leadtime_curve": fit,
        "model_residuals": residuals,
        "consensus_sigma_floor": _wcal.consensus_sigma_floor(residuals),
    })


def snapshot_prices() -> int:
    """Take a snapshot of current market prices for historical tracking."""
    try:
        cached = cache_get("parsed_markets")
        if not cached:
            return 0
        markets = cached.get("markets", [])
        if not markets:
            return 0

        # Also try to get forecast data
        fc_cached = cache_get("all_forecasts")
        forecasts = fc_cached.get("forecasts", {}) if fc_cached else {}

        rows_to_insert = []
        for m in markets:
            mid = m.get("id", "")
            if not mid:
                continue
            enrich = forecasts.get(mid, {})
            rows_to_insert.append((
                mid,
                m.get("source", "polymarket"),
                m.get("question", ""),
                m.get("city"),
                m.get("target_date"),
                m.get("yes_price"),
                enrich.get("model_prob") if enrich.get("model_prob") is not None else m.get("model_prob"),
                enrich.get("edge") if enrich.get("edge") is not None else m.get("edge"),
                _safe_float(m.get("volume")),
            ))
        count = 0
        with _get_conn() as conn:
            for i in range(0, len(rows_to_insert), 500):
                batch = rows_to_insert[i:i + 500]
                conn.executemany(
                    "INSERT INTO weather_price_snapshots "
                    "(market_id, source, question, city, target_date, yes_price, model_prob, edge, volume) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    batch,
                )
                count += len(batch)
        logger.info("Snapshot: saved %d market prices", count)
        return count
    except Exception as e:
        logger.error("Snapshot failed: %s", e)
        return 0


def fetch_kalshi_price_history(series_ticker: str, ticker: str, period: int = 1440) -> list[dict]:
    """Fetch historical candlestick data from Kalshi. period=1440 means daily."""
    try:
        for base_path in [
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            f"/markets/{ticker}/candlesticks",
            f"/historical/markets/{ticker}/candlesticks",
        ]:
            url = f"{KALSHI_BASE}{base_path}"
            params = {"period": period}
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                candles = data.get("candlesticks", [])
                if candles:
                    result = []
                    for c in candles:
                        if not c.get("end_period_ts"):
                            continue
                        raw_price = c.get("price")
                        if isinstance(raw_price, dict):
                            close = _safe_float(raw_price.get("close"))
                        else:
                            close = _safe_float(raw_price)
                        result.append({
                            "timestamp": c["end_period_ts"],
                            "price": close / 100,
                        })
                    return result
        return []
    except Exception as e:
        logger.warning("Kalshi price history fetch failed for %s: %s", ticker, e)
        return []


def backfill_price_history() -> dict:
    """Fetch and store historical markets from Polymarket (closed events) and Kalshi candlesticks."""
    poly_count = 0
    kalshi_count = 0

    # ── Phase 1: Fetch closed Polymarket weather markets from the past year ──
    try:
        tag_slugs = ["temperature", "weather"]
        seen = set()
        poly_rows = []
        for tag in tag_slugs:
            offset = 0
            while offset < 2000:
                resp = requests.get(f"{GAMMA_BASE}/events", params={
                    "tag_slug": tag, "closed": "true", "limit": 100, "offset": offset
                }, timeout=20)
                if resp.status_code != 200:
                    break
                events = resp.json()
                if not events:
                    break
                for ev in events:
                    for m in ev.get("markets", []):
                        mid = m.get("conditionId") or m.get("id", "")
                        if not mid or mid in seen:
                            continue
                        seen.add(mid)
                        question = m.get("question", "")
                        city = parse_city(question)
                        target_date = parse_date(question) or parse_date(ev.get("title", ""))
                        prices = m.get("outcomePrices", [])
                        if isinstance(prices, str):
                            try:
                                prices = json.loads(prices)
                            except Exception:
                                prices = []
                        yes_price = _safe_float(prices[0], None) if prices else None
                        end_date = m.get("endDate", "")
                        ts = end_date if end_date else m.get("updatedAt", "")
                        if not ts:
                            continue
                        poly_rows.append((
                            ts, mid, "polymarket", question, city, target_date,
                            yes_price, _safe_float(m.get("volume")),
                        ))
                offset += 100
        # Insert in batches (upsert via INSERT OR REPLACE)
        with _get_conn() as conn:
            for i in range(0, len(poly_rows), 500):
                batch = poly_rows[i:i + 500]
                conn.executemany(
                    "INSERT OR IGNORE INTO weather_price_snapshots "
                    "(timestamp, market_id, source, question, city, target_date, yes_price, volume) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    batch,
                )
                poly_count += len(batch)
    except Exception as e:
        logger.error("Polymarket backfill error: %s", e)

    # ── Phase 2: Fetch Kalshi candlestick history for current markets ──
    try:
        cached = cache_get("parsed_markets")
        if cached:
            for m in cached.get("markets", []):
                if m.get("source") != "kalshi":
                    continue
                ticker = m.get("kalshi_ticker")
                if not ticker:
                    continue
                # Check how many existing snapshots we have for this market
                with _get_conn(readonly=True) as conn:
                    existing_count = conn.execute(
                        "SELECT COUNT(*) FROM weather_price_snapshots WHERE market_id = ?",
                        (m["id"],),
                    ).fetchone()[0]
                if existing_count > 3:
                    continue
                series = re.match(r"([A-Z]+)", ticker)
                series_ticker = series.group(1) if series else ""
                history = fetch_kalshi_price_history(series_ticker, ticker, period=1440)
                kalshi_rows = []
                for h in history:
                    ts = h.get("timestamp")
                    price = h.get("price")
                    if ts is None or price is None:
                        continue
                    if isinstance(ts, (int, float)):
                        ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                    kalshi_rows.append((
                        ts, m["id"], "kalshi", m.get("question", ""),
                        m.get("city"), m.get("target_date"), price, 0,
                    ))
                if kalshi_rows:
                    with _get_conn() as conn:
                        conn.executemany(
                            "INSERT OR IGNORE INTO weather_price_snapshots "
                            "(timestamp, market_id, source, question, city, target_date, yes_price, volume) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            kalshi_rows,
                        )
                    kalshi_count += len(kalshi_rows)
    except Exception as e:
        logger.error("Kalshi backfill error: %s", e)

    logger.info("Backfill complete: %d Polymarket closed markets, %d Kalshi candles", poly_count, kalshi_count)
    return {"polymarket": poly_count, "kalshi": kalshi_count}


@app.route("/api/backfill_history", methods=["POST"])
@require_admin
def api_backfill_history():
    """Admin-only: trigger a historical price backfill for all markets."""
    import threading as _th
    _th.Thread(target=backfill_price_history, daemon=True).start()
    return jsonify({"status": "backfill started in background"})


@app.route("/api/price_history/<market_id>")
@require_auth
def api_price_history(market_id):
    """Return price history for a specific market. Daily by default, hourly requires premium."""
    granularity = request.args.get("granularity", "daily")  # daily or hourly

    with _get_conn(readonly=True) as conn:
        rows = conn.execute(
            "SELECT timestamp, yes_price, model_prob, edge, volume "
            "FROM weather_price_snapshots WHERE market_id = ? ORDER BY timestamp ASC",
            (market_id,),
        ).fetchall()
    snapshots = [dict(r) for r in rows]

    # For daily view: aggregate to one point per day
    if granularity == "daily" and snapshots:
        daily = {}
        for s in snapshots:
            ts = s.get("timestamp") or ""
            day = ts[:10] if ts else None
            if day and (day not in daily or ts > daily[day]["timestamp"]):
                daily[day] = s
        snapshots = sorted(daily.values(), key=lambda x: x["timestamp"])

    return jsonify({
        "market_id": market_id,
        "snapshots": snapshots,
        "granularity": granularity,
        "requires_premium": granularity != "daily",
    })


@app.route("/api/price_history_city/<city>")
@require_auth
def api_price_history_city(city):
    """Return price history for all markets in a city, grouped by market_id."""
    with _get_conn(readonly=True) as conn:
        rows = conn.execute(
            "SELECT market_id, timestamp, yes_price, model_prob, edge, volume, question, source "
            "FROM weather_price_snapshots WHERE LOWER(city) = ? ORDER BY market_id, timestamp ASC",
            (city.lower(),),
        ).fetchall()

    grouped = {}
    for r in [dict(row) for row in rows]:
        mid = r["market_id"]
        if mid not in grouped:
            grouped[mid] = {"market_id": mid, "question": r["question"], "source": r["source"], "snapshots": []}
        grouped[mid]["snapshots"].append({
            "timestamp": r["timestamp"], "yes_price": r["yes_price"],
            "model_prob": r["model_prob"], "edge": r["edge"], "volume": r["volume"],
        })
    return jsonify({"city": city, "markets": list(grouped.values())})


@app.route("/api/snapshot_stats")
@require_auth
def api_snapshot_stats():
    """Return summary stats about stored price snapshots."""
    with _get_conn(readonly=True) as conn:
        total = conn.execute("SELECT COUNT(*) FROM weather_price_snapshots").fetchone()[0]

        row = conn.execute(
            "SELECT timestamp FROM weather_price_snapshots ORDER BY timestamp ASC LIMIT 1"
        ).fetchone()
        oldest = row[0] if row else None

        row = conn.execute(
            "SELECT timestamp FROM weather_price_snapshots ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        newest = row[0] if row else None

        unique_markets = conn.execute(
            "SELECT COUNT(DISTINCT market_id) FROM weather_price_snapshots"
        ).fetchone()[0]

    return jsonify({"total_snapshots": total, "unique_markets": unique_markets, "oldest": oldest, "newest": newest})


@app.route("/api/log_signal", methods=["POST"])
@require_auth
def api_log_signal():
    """Manually log a signal (called by frontend when user views a signal)."""
    data = request.get_json(force=True, silent=True) or {}
    market_id = data.get("market_id")
    if not market_id:
        return jsonify({"error": "market_id is required"}), 400

    log_signal(
        market_id=market_id,
        question=data.get("question", ""),
        category=data.get("category", "other"),
        yes_price=data.get("yes_price"),
        model_prob=data.get("model_prob"),
        edge=data.get("edge"),
        action=data.get("action", "manual_view"),
    )
    return jsonify({"status": "ok"})


# ─── Alerts Endpoints ─────────────────────────────────────────────────────────

@app.route("/api/alerts/settings", methods=["GET"])
@require_auth
def api_alerts_settings_get():
    """Get current alert settings."""
    user = request.user
    with _get_conn(readonly=True) as conn:
        row = conn.execute(
            "SELECT * FROM weather_alert_settings WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user["id"],),
        ).fetchone()

    if not row:
        return jsonify({"settings": None})
    settings = dict(row)
    # categories stored as JSON text in SQLite
    if isinstance(settings.get("categories"), str):
        try:
            settings["categories"] = json.loads(settings["categories"])
        except (json.JSONDecodeError, TypeError):
            settings["categories"] = []
    return jsonify({"settings": settings})


@app.route("/api/alerts/settings", methods=["POST"])
@require_auth
def api_alerts_settings_post():
    """Save alert preferences."""
    data = request.get_json(force=True, silent=True) or {}
    edge_threshold = data.get("edge_threshold", 0.08)
    categories = data.get("categories", [])
    push_enabled = 1 if data.get("push_enabled", False) else 0
    email = data.get("email", None)

    user = request.user
    categories_json = json.dumps(categories)
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO weather_alert_settings (user_id, edge_threshold, categories, push_enabled, email) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "edge_threshold=excluded.edge_threshold, categories=excluded.categories, "
            "push_enabled=excluded.push_enabled, email=excluded.email",
            (user["id"], edge_threshold, categories_json, push_enabled, email),
        )
    return jsonify({"status": "ok"})


@app.route("/api/alerts/active")
@require_auth
def api_alerts_active():
    """Get current alerts that match user settings."""
    user = request.user
    with _get_conn(readonly=True) as conn:
        row = conn.execute(
            "SELECT * FROM weather_alert_settings WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user["id"],),
        ).fetchone()

    if not row:
        return jsonify({"alerts": [], "settings": None})

    settings = dict(row)
    edge_threshold = settings.get("edge_threshold", 0.08)
    filter_categories = settings.get("categories", [])
    if isinstance(filter_categories, str):
        try:
            filter_categories = json.loads(filter_categories)
        except (json.JSONDecodeError, TypeError):
            filter_categories = []

    # Fetch current markets (uses cache)
    raw_markets = fetch_all_weather_markets()
    alerts = []

    for m in raw_markets:
        question = m.get("question", "") or m.get("title", "")
        if not question:
            continue

        prices = m.get("outcomePrices", [])
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except json.JSONDecodeError:
                prices = []

        yes_price = _safe_float(prices[0], None) if len(prices) > 0 else None
        city = parse_city(question)
        temp_info = parse_temperature(question)
        target_date = parse_date(question) or parse_date(m.get("_event_title", ""))
        category = categorize_market(question, m.get("_event_tags", []))

        # Category filter
        if filter_categories and category not in filter_categories:
            continue

        station = None
        if city:
            s = STATION_MAP.get(city)
            if s:
                station = {"lat": s[0], "lon": s[1], "icao": s[2], "name": s[3]}

        has_temp = temp_info["threshold"] is not None or temp_info["temp_lower"] is not None
        if not (station and target_date and has_temp):
            continue

        forecast = fetch_forecast(station["lat"], station["lon"], target_date)
        if not forecast:
            continue

        model_prob = compute_probability(forecast, temp_info)
        if model_prob is None or yes_price is None or yes_price <= 0:
            continue

        edge = round(model_prob - yes_price, 4)
        if abs(edge) >= edge_threshold:
            alerts.append({
                "market_id": m.get("conditionId") or m.get("id", ""),
                "question": question,
                "category": category,
                "city": city,
                "target_date": target_date,
                "yes_price": yes_price,
                "model_prob": model_prob,
                "edge": edge,
                "edge_pct": round(edge * 100, 1),
                "action": "BUY_YES" if edge > 0 else "BUY_NO",
                "forecast_mean": forecast["mean"],
                "forecast_std": forecast["std"],
                "source": forecast.get("source"),
            })

    alerts.sort(key=lambda x: -abs(x["edge"]))

    filter_cats_parsed = filter_categories if isinstance(filter_categories, list) else []

    return jsonify({
        "alerts": alerts,
        "count": len(alerts),
        "settings": {
            "edge_threshold": edge_threshold,
            "categories": filter_cats_parsed,
            "push_enabled": bool(settings.get("push_enabled")),
        },
    })


# ─── Auth Endpoints ──────────────────────────────────────────────────────────

@app.route("/api/auth/me")
def api_me():
    user = _get_user_from_request()
    if not user:
        if not _is_behind_gateway():
            # Not behind gateway — allow anonymous access with a local user
            user = {"id": "local", "username": "local", "email": "", "is_admin": 1}
        else:
            return jsonify({"user": None, "settings": {}, "favorites": []}), 200

    # Load persisted settings/favorites from weather_user_prefs (or in-memory fallback)
    settings = {}
    favorites = []
    user_id = user["id"]
    try:
        with _get_conn(readonly=True) as conn:
            row = conn.execute(
                "SELECT settings, favorites FROM weather_user_prefs WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            if row:
                r = dict(row)
                raw_settings = r.get("settings") or "{}"
                raw_favorites = r.get("favorites") or "[]"
                settings = json.loads(raw_settings) if isinstance(raw_settings, str) else raw_settings
                favorites = json.loads(raw_favorites) if isinstance(raw_favorites, str) else raw_favorites
    except Exception as e:
        logger.warning("Failed to load prefs for %s: %s", user_id, e)

    # Fall back to in-memory cache if DB returned nothing
    if not settings and not favorites:
        with _user_prefs_lock:
            cached = _user_prefs_cache.get(user_id)
        if cached:
            settings = cached.get("settings", {})
            favorites = cached.get("favorites", [])

    return jsonify({
        "user": {
            "id": user["id"],
            "username": user.get("username", ""),
            "is_admin": bool(user.get("is_admin")),
            "email": user.get("email"),
            "settings": settings,
            "favorites": favorites,
        },
    })


@app.route("/api/auth/settings", methods=["PUT"])
@require_auth
def api_user_settings():
    """Persist user settings to weather_user_prefs table (upsert)."""
    data = request.get_json(silent=True) or {}
    user_id = request.user["id"]
    log_activity(user_id, "update_settings")
    try:
        settings_json = json.dumps(data)
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO weather_user_prefs (user_id, settings) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET settings=excluded.settings",
                (user_id, settings_json),
            )
    except Exception as e:
        logger.warning("Failed to persist settings for %s: %s", user_id, e)
        # Fall back to in-memory cache so settings survive within this session
        with _user_prefs_lock:
            if len(_user_prefs_cache) > _USER_PREFS_CACHE_MAX_SIZE:
                _user_prefs_cache.clear()
            _user_prefs_cache[user_id] = {"settings": data}
    return jsonify({"status": "ok"})


@app.route("/api/auth/favorites", methods=["PUT"])
@require_auth
def api_user_favorites():
    """Persist user favorites to weather_user_prefs table (upsert)."""
    data = request.get_json(silent=True) or []
    user_id = request.user["id"]
    try:
        favorites_json = json.dumps(data)
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO weather_user_prefs (user_id, favorites) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET favorites=excluded.favorites",
                (user_id, favorites_json),
            )
    except Exception as e:
        logger.warning("Failed to persist favorites for %s: %s", user_id, e)
        with _user_prefs_lock:
            if len(_user_prefs_cache) > _USER_PREFS_CACHE_MAX_SIZE:
                _user_prefs_cache.clear()
            _user_prefs_cache.setdefault(user_id, {})["favorites"] = data
    return jsonify({"status": "ok"})


# ─── Admin Endpoints ─────────────────────────────────────────────────────────

@app.route("/admin")
@require_admin
def admin_page():
    """Serve admin dashboard HTML."""
    return send_from_directory(app.static_folder, "admin.html")


@app.route("/api/admin/users")
@require_admin
def api_admin_users():
    """List users from the profiles table (managed by gateway)."""
    with _get_conn(readonly=True) as conn:
        rows = conn.execute(
            "SELECT id, username, email, is_admin, created_at FROM profiles ORDER BY created_at DESC"
        ).fetchall()
    return jsonify({"users": [dict(r) for r in rows]})


@app.route("/api/admin/metrics")
@require_admin
def api_admin_metrics():
    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(days=1)).isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()

    with _get_conn(readonly=True) as conn:
        # Total users from profiles
        total_users = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]

        # Active users from weather_user_activity
        active_24h = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM weather_user_activity WHERE timestamp >= ?", (day_ago,)
        ).fetchone()[0]

        active_7d = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM weather_user_activity WHERE timestamp >= ?", (week_ago,)
        ).fetchone()[0]

        # Total signals
        total_signals = conn.execute("SELECT COUNT(*) FROM weather_signals_log").fetchone()[0]

        # Activity by day (last 1000 entries, then group by day)
        recent_rows = conn.execute(
            "SELECT timestamp FROM weather_user_activity ORDER BY timestamp DESC LIMIT 1000"
        ).fetchall()
        activity_by_day_map = {}
        for r in recent_rows:
            day = (r[0] or "")[:10]
            if day:
                activity_by_day_map[day] = activity_by_day_map.get(day, 0) + 1
        activity_by_day = [{"day": d, "c": c} for d, c in sorted(activity_by_day_map.items(), reverse=True)[:30]]

        # Popular actions
        action_rows = conn.execute("SELECT action FROM weather_user_activity").fetchall()
        action_counts = {}
        for r in action_rows:
            a = r[0] or ""
            action_counts[a] = action_counts.get(a, 0) + 1
        popular_actions = [{"action": a, "c": c} for a, c in sorted(action_counts.items(), key=lambda x: -x[1])[:10]]

    return jsonify({
        "total_users": total_users,
        "active_24h": active_24h,
        "active_7d": active_7d,
        "total_signals": total_signals,
        "signups_by_day": [],  # Signups tracked by gateway, not this dashboard
        "activity_by_day": activity_by_day,
        "popular_actions": popular_actions,
    })


@app.route("/api/admin/activity")
@require_admin
def api_admin_activity():
    lim = request.args.get("limit", 100, type=int)
    with _get_conn(readonly=True) as conn:
        rows = conn.execute(
            "SELECT * FROM weather_user_activity ORDER BY timestamp DESC LIMIT ?", (lim,)
        ).fetchall()
        activities = [dict(r) for r in rows]

        # Enrich with usernames from profiles
        user_ids = list(set(a.get("user_id") for a in activities if a.get("user_id")))
        username_map = {}
        if user_ids:
            placeholders = ",".join("?" * len(user_ids))
            profile_rows = conn.execute(
                f"SELECT id, username FROM profiles WHERE id IN ({placeholders})", user_ids
            ).fetchall()
            for p in profile_rows:
                username_map[p[0]] = p[1]

    for a in activities:
        a["username"] = username_map.get(a.get("user_id"), "unknown")

    return jsonify({"activity": activities})


# ── Bot Signal Feed + Calibration ────────────────────────────────────────────
# Reads from the weather_bot's trades.db (separate SQLite, written by
# polymarket_weather_bot/main.py).  Falls back gracefully if DB missing.

_BOT_DB = Path(__file__).parent.parent / "polymarket_weather_bot" / "trades.db"


def _get_bot_conn():
    """Read-only connection to the bot's trades.db."""
    if not _BOT_DB.exists():
        return None
    conn = sqlite3.connect(str(_BOT_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


@app.route("/api/bot/signals")
@require_auth
def api_bot_signals():
    """Live trading signals from the weather bot (last 100)."""
    conn = _get_bot_conn()
    if conn is None:
        return jsonify({"signals": [], "note": "Bot database not found"})
    try:
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY timestamp DESC LIMIT 100"
        ).fetchall()
        return jsonify({"signals": [dict(r) for r in rows]})
    finally:
        conn.close()


@app.route("/api/bot/trades")
@require_auth
def api_bot_trades():
    """Recent trades placed by the weather bot."""
    limit = request.args.get("limit", 50, type=int)
    conn = _get_bot_conn()
    if conn is None:
        return jsonify({"trades": [], "note": "Bot database not found"})
    try:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return jsonify({"trades": [dict(r) for r in rows]})
    finally:
        conn.close()


@app.route("/api/bot/calibration")
@require_auth
def api_bot_calibration():
    """Brier score and calibration breakdown from resolved signals."""
    conn = _get_bot_conn()
    if conn is None:
        return jsonify({"n": 0, "brier_model": None, "brier_market": None,
                        "note": "Bot database not found"})
    try:
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='calibration'"
        ).fetchone()
        if not tbl:
            return jsonify({"n": 0, "brier_model": None, "brier_market": None,
                            "note": "No calibration data yet"})
        rows = conn.execute(
            "SELECT model_prob, market_prob, outcome, prob_method, platform "
            "FROM calibration WHERE outcome IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return jsonify({"n": 0, "brier_model": None, "brier_market": None})

    n = len(rows)
    brier_model = sum((r["model_prob"] - r["outcome"]) ** 2 for r in rows) / n
    brier_market = sum((r["market_prob"] - r["outcome"]) ** 2 for r in rows) / n

    buckets = {}
    for r in rows:
        b = min(int(r["model_prob"] * 10), 9)
        label = f"{b * 10}-{b * 10 + 10}%"
        if label not in buckets:
            buckets[label] = {"n": 0, "sp": 0.0, "so": 0.0}
        buckets[label]["n"] += 1
        buckets[label]["sp"] += r["model_prob"]
        buckets[label]["so"] += r["outcome"]

    cal = {}
    for label, d in sorted(buckets.items()):
        cal[label] = {"n": d["n"],
                      "avg_predicted": round(d["sp"] / d["n"], 3),
                      "avg_actual": round(d["so"] / d["n"], 3)}

    return jsonify({
        "n": n,
        "brier_model": round(brier_model, 4),
        "brier_market": round(brier_market, 4),
        "edge_vs_market": round(brier_market - brier_model, 4),
        "calibration_buckets": cal,
    })


@app.route("/api/bot/stats")
@require_auth
def api_bot_stats():
    """Summary stats from the weather bot."""
    conn = _get_bot_conn()
    if conn is None:
        return jsonify({"note": "Bot database not found"})
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        total = conn.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(amount),0) as total FROM trades"
        ).fetchone()
        td_trades = conn.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(amount),0) as total "
            "FROM trades WHERE timestamp LIKE ?", (f"{today}%",)
        ).fetchone()
        td_sigs = conn.execute(
            "SELECT COUNT(*) as cnt FROM signals "
            "WHERE timestamp LIKE ? AND action != 'NO_TRADE'", (f"{today}%",)
        ).fetchone()
        by_plat = conn.execute(
            "SELECT COALESCE(platform,'polymarket') as plat, COUNT(*) as cnt, "
            "COALESCE(SUM(amount),0) as total FROM trades GROUP BY plat"
        ).fetchall()
        by_city = conn.execute(
            "SELECT city, COUNT(*) as cnt, COALESCE(SUM(amount),0) as total "
            "FROM trades GROUP BY city ORDER BY cnt DESC"
        ).fetchall()
    finally:
        conn.close()

    return jsonify({
        "all_time": {"trades": total["cnt"], "wagered": round(total["total"], 2)},
        "today": {"trades": td_trades["cnt"], "wagered": round(td_trades["total"], 2),
                  "actionable_signals": td_sigs["cnt"]},
        "by_platform": {r["plat"]: {"trades": r["cnt"], "wagered": round(r["total"], 2)} for r in by_plat},
        "by_city": {r["city"]: {"trades": r["cnt"], "wagered": round(r["total"], 2)} for r in by_city},
    })


def _snapshot_loop():
    """Background thread: take price snapshots every 30 minutes."""
    import time as _time
    _register_thread("snapshot_prices", interval_seconds=1800)
    _time.sleep(120)  # Wait 2 min for first data to load
    while True:
        try:
            snapshot_prices()
            _record_run("snapshot_prices", ok=True)
        except Exception as e:
            logger.error("Snapshot loop error: %s", e)
            _record_run("snapshot_prices", ok=False, error=str(e))
        _time.sleep(1800)  # 30 minutes


def _bias_pairing_loop():
    """Background thread: every 6 hours, walk every station in STATION_MAP
    and pair any unpaired forecast_history rows with the now-observed high
    from Open-Meteo's archive. This is what makes get_model_biases() return
    actual numbers — without it the bias table would just keep growing
    without ever getting closed out.
    """
    import time as _time
    _register_thread("bias_pairing", interval_seconds=6 * 3600)
    _time.sleep(600)  # Wait 10 min after boot before first pairing
    while True:
        try:
            paired_total = 0
            seen_stations = set()
            for city_key, info in STATION_MAP.items():
                # STATION_MAP has alias entries (e.g. "nyc" -> same coords as "new york").
                # Avoid pairing the same coords twice in one pass.
                coord_key = (round(info[0], 4), round(info[1], 4))
                if coord_key in seen_stations:
                    continue
                seen_stations.add(coord_key)
                if _open_meteo_in_cooldown():
                    _time.sleep(60)
                    continue
                try:
                    n = pair_forecasts_with_observed(city_key, info[0], info[1])
                    paired_total += n or 0
                except Exception as e:
                    logger.warning("Bias pair %s: %s", city_key, e)
                _time.sleep(2)  # Be polite to Open-Meteo
            if paired_total:
                logger.info("Bias pairing pass: paired %d rows", paired_total)
            _record_run("bias_pairing", ok=True)
        except Exception as e:
            logger.error("Bias pairing loop error: %s", e)
            _record_run("bias_pairing", ok=False, error=str(e))
        _time.sleep(6 * 3600)  # 6 hours


@app.after_request
def _add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    if _is_behind_gateway():
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


if __name__ == "__main__":
    _debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    _production = os.environ.get("PRODUCTION", "0") == "1"
    # Never allow debug mode in production
    if _debug and _production:
        logging.error("FLASK_DEBUG=1 is not allowed when PRODUCTION=1 — disabling debug mode")
        _debug = False
    # Never bind to all interfaces in production, even if DEV_MODE leaks through
    bind_host = "127.0.0.1" if _production else ("0.0.0.0" if _DEV_MODE else "127.0.0.1")
    # Only start background threads in the reloader child (or when reloader is off)
    if not _debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        t = threading.Thread(target=_snapshot_loop, daemon=True)
        t.start()
        logger.info("Price snapshot background thread started (every 30 min)")
        bp = threading.Thread(target=_bias_pairing_loop, daemon=True)
        bp.start()
        logger.info("Forecast bias pairing background thread started (every 6 h)")
        ip = threading.Thread(target=_intraday_poll_loop, daemon=True)
        ip.start()
        logger.info("Intraday running-max poller started (every 5 min)")
    app.run(host=bind_host, port=5050, debug=_debug)

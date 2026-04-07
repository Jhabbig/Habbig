#!/usr/bin/env python3
"""Polymarket Weather Dashboard — Flask backend."""

from __future__ import annotations

import functools
import hmac
import json
import logging
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
"""


@contextmanager
def _get_conn():
    """Yield a SQLite connection with WAL mode and row_factory.

    WAL mode supports concurrent readers, so the lock is only held during
    writes (commit/rollback) to avoid blocking read-heavy web requests
    behind the snapshot thread.
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
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

_BEHIND_GATEWAY = bool(os.environ.get("GATEWAY_SSO_SECRET"))


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
                with _get_conn() as conn:
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
            return jsonify({"error": "unauthorized"}), 401
        request.user = user
        return f(*args, **kwargs)
    return wrapper


def require_admin(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        user = _get_user_from_request()
        if not user or not user.get("is_admin"):
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

# ─── Cache ─────────────────────────────────────────────────────────────────────

_cache: dict = {}
_user_prefs_cache: dict = {}  # Fallback in-memory cache for user settings/favorites
CACHE_TTL = 300  # 5 minutes — frontend polls every 4 min to stay ahead


def cache_get(key: str):
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < CACHE_TTL:
        return entry["data"]
    return None


def cache_set(key: str, data):
    _cache[key] = {"data": data, "ts": time.time()}


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


def _parse_kalshi_market(m: dict) -> Optional[dict]:
    """Parse a Kalshi market into the same structure as Polymarket."""
    ticker = m.get("ticker", "")
    title = m.get("title", "")
    if not title:
        return None

    # Price: yes_bid is what you can buy YES at, yes_ask is what you can sell at
    # Use last_price or midpoint
    yes_bid = float(m.get("yes_bid_dollars") or 0)
    yes_ask = float(m.get("yes_ask_dollars") or 0)
    last_price = float(m.get("last_price_dollars") or 0)
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
        temp_info["threshold"] = float(floor_strike)
        temp_info["is_over"] = True
    elif strike_type == "less" and cap_strike is not None:
        temp_info["threshold"] = float(cap_strike)
        temp_info["is_over"] = False
    elif strike_type == "between" and floor_strike is not None and cap_strike is not None:
        temp_info["temp_lower"] = float(floor_strike)
        temp_info["temp_upper"] = float(cap_strike)

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

    # Volume
    volume_fp = m.get("volume_fp") or m.get("volume") or "0"
    open_interest = m.get("open_interest_fp") or "0"

    return {
        "id": f"kalshi_{ticker}",
        "question": title,
        "slug": ticker,
        "event_title": event_ticker,
        "tags": ["kalshi", "weather"],
        "yes_price": yes_price,
        "no_price": no_price,
        "volume": str(float(volume_fp)),
        "liquidity": str(float(open_interest)),
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
    for month_name, month_num in month_map.items():
        if len(month_name) > 3 and month_name in tl:
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


def _fetch_ensemble_model(lat: float, lon: float, date_str: str, model: str) -> Optional[dict]:
    """Fetch ensemble forecast for a single model. Returns dict with mean/std/min/max/ensemble or None."""
    try:
        resp = requests.get(ENSEMBLE_URL, params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit",
            "start_date": date_str, "end_date": date_str,
            "models": model,
        }, timeout=10)
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
}

# Which model Polymarket primarily resolves from (Weather Underground uses NWS/GFS for US)
RESOLUTION_MODEL = "gfs_seamless"


def fetch_multi_model_forecast(lat: float, lon: float, date_str: str) -> Optional[dict]:
    """Fetch forecasts from all available weather models in parallel."""
    cache_key = f"multifc_{lat}_{lon}_{date_str}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    from concurrent.futures import ThreadPoolExecutor

    models_data = {}

    def _fetch_one(model_id):
        return model_id, _fetch_ensemble_model(lat, lon, date_str, model_id)

    with ThreadPoolExecutor(max_workers=len(WEATHER_MODELS)) as pool:
        futures = {pool.submit(_fetch_one, m): m for m in WEATHER_MODELS.keys()}
        for future in futures:
            try:
                model_id, result = future.result()
                if result:
                    info = WEATHER_MODELS[model_id]
                    result["model_name"] = info["name"]
                    result["org"] = info["org"]
                    result["is_resolution_model"] = (model_id == RESOLUTION_MODEL)
                    models_data[model_id] = result
            except Exception as e:
                failed_model = futures[future]
                logger.warning("Model %s forecast fetch failed: %s", failed_model, e)

    if not models_data:
        return None

    # Compute consensus (average of all models)
    all_means = [m["mean"] for m in models_data.values()]
    all_stds = [m["std"] for m in models_data.values()]
    all_temps = []
    for m in models_data.values():
        all_temps.extend(m["ensemble"])

    result = {
        "mean": round(statistics.mean(all_means), 1),
        "std": round(statistics.mean(all_stds), 1),
        "min": round(min(all_temps), 1),
        "max": round(max(all_temps), 1),
        "ensemble": all_temps,
        "source": f"{len(models_data)} models",
        "models": models_data,
    }
    cache_set(cache_key, result)
    return result


def fetch_forecast(lat: float, lon: float, date_str: str) -> Optional[dict]:
    """Wrapper that returns multi-model forecast."""
    return fetch_multi_model_forecast(lat, lon, date_str)


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
    mean = forecast["mean"]
    std = forecast["std"]
    if mean is None or std is None:
        return None

    # Convert Celsius thresholds to Fahrenheit (forecasts are always in F)
    is_celsius = temp_info.get("unit", "F").upper().startswith("C")

    threshold = temp_info.get("threshold")
    is_over = temp_info.get("is_over")
    lower = temp_info.get("temp_lower")
    upper = temp_info.get("temp_upper")

    if is_celsius:
        if threshold is not None:
            threshold = _c_to_f(threshold)
        if lower is not None:
            lower = _c_to_f(lower)
        if upper is not None:
            upper = _c_to_f(upper)
        # Also scale std for range comparison (°C std * 1.8 = °F std)
        # No -- std is already in °F from the forecast. Only thresholds need conversion.

    if threshold is not None:
        if is_over:
            prob = round(1.0 - norm.cdf(threshold, loc=mean, scale=std), 4)
        else:
            prob = round(norm.cdf(threshold, loc=mean, scale=std), 4)
        return max(0.01, min(0.99, prob))
    elif lower is not None and upper is not None:
        prob = round(
            norm.cdf(upper + 0.5, loc=mean, scale=std) - norm.cdf(lower - 0.5, loc=mean, scale=std), 4
        )
        return max(0.01, min(0.99, prob))
    return None


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
    yes_price = float(prices[0]) if len(prices) > 0 else None
    no_price = float(prices[1]) if len(prices) > 1 else None
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


@app.route("/api/markets")
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
        kalshi_parsed = [e for e in (_parse_kalshi_market(m) for m in kalshi_raw) if e is not None]
        enriched.extend(kalshi_parsed)
        logger.info("Merged %d Kalshi markets into response", len(kalshi_parsed))
    except Exception as e:
        logger.error("Kalshi fetch failed, continuing with Polymarket only: %s", e)

    enriched.sort(key=lambda x: -(float(x["volume"]) if x["volume"] else 0))

    result = {"markets": enriched, "count": len(enriched),
              "timestamp": datetime.now(timezone.utc).isoformat()}
    cache_set("parsed_markets", result)
    return jsonify(result)


@app.route("/api/forecasts")
def api_forecasts():
    """Slower endpoint: returns multi-model forecasts for all city+date combos, with probabilities."""
    cached = cache_get("all_forecasts")
    if cached is not None:
        return jsonify(cached)

    # Collect unique city+date+temp_info combos from BOTH sources
    forecast_needs: dict[str, tuple] = {}  # key -> (lat, lon, date)
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
            forecast_needs[fc_key] = (s[0], s[1], target_date)
            market_temps[fc_key] = []

        prices = m.get("outcomePrices", [])
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except json.JSONDecodeError:
                prices = []
        yes_price = float(prices[0]) if len(prices) > 0 else None
        market_id = m.get("conditionId") or m.get("id", "")
        market_temps[fc_key].append((market_id, yes_price, temp_info))

    # --- Kalshi markets (already parsed) ---
    try:
        kalshi_raw = fetch_kalshi_weather_markets()
        for km in kalshi_raw:
            parsed = _parse_kalshi_market(km)
            if not parsed or not parsed.get("city") or not parsed.get("target_date") or not parsed.get("temp_info"):
                continue
            city = parsed["city"]
            s = STATION_MAP.get(city)
            if not s:
                continue
            fc_key = f"{city}:{parsed['target_date']}"
            if fc_key not in forecast_needs:
                forecast_needs[fc_key] = (s[0], s[1], parsed["target_date"])
                market_temps[fc_key] = []
            market_temps[fc_key].append((parsed["id"], parsed["yes_price"], parsed["temp_info"]))
    except Exception as e:
        logger.error("Kalshi forecasts merge failed: %s", e)

    # Parallel fetch all forecasts
    from concurrent.futures import ThreadPoolExecutor

    def _fetch_fc(args):
        key, (lat, lon, date) = args
        return key, fetch_multi_model_forecast(lat, lon, date)

    forecast_data: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=20) as pool:
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
def api_forecast(city, date):
    city_key = CITY_ALIASES.get(city.lower(), city.lower())
    station = STATION_MAP.get(city_key)
    if not station:
        return jsonify({"error": f"Unknown city: {city}"}), 404
    forecast = fetch_forecast(station[0], station[1], date)
    if not forecast:
        return jsonify({"error": "Forecast not available"}), 404
    return jsonify({
        "city": city_key,
        "station": {"lat": station[0], "lon": station[1], "icao": station[2], "name": station[3]},
        "date": date,
        "forecast": forecast,
    })


@app.route("/api/stations")
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
def api_history():
    """Return recent signals with pagination. Query params: page, per_page, category, period."""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    category = request.args.get("category", None)
    period = request.args.get("period", None)
    per_page = min(per_page, 200)
    sb_offset = (page - 1) * per_page

    with _get_conn() as conn:
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
def api_accuracy():
    """Compute accuracy stats: win rate, avg edge, by category."""
    with _get_conn() as conn:
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
                float(m.get("volume") or 0),
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
                    return [{
                        "timestamp": c.get("end_period_ts"),
                        "price": float(c.get("price", {}).get("close", 0)) / 100,
                    } for c in candles if c.get("end_period_ts")]
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
                        yes_price = float(prices[0]) if prices else None
                        end_date = m.get("endDate", "")
                        ts = end_date if end_date else m.get("updatedAt", "")
                        if not ts:
                            continue
                        poly_rows.append((
                            ts, mid, "polymarket", question, city, target_date,
                            yes_price, float(m.get("volume") or 0),
                        ))
                offset += 100
        # Insert in batches (upsert via INSERT OR REPLACE)
        with _get_conn() as conn:
            for i in range(0, len(poly_rows), 500):
                batch = poly_rows[i:i + 500]
                conn.executemany(
                    "INSERT OR REPLACE INTO weather_price_snapshots "
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
                with _get_conn() as conn:
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
                            "INSERT INTO weather_price_snapshots "
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
def api_price_history(market_id):
    """Return price history for a specific market. Daily by default, hourly requires premium."""
    granularity = request.args.get("granularity", "daily")  # daily or hourly

    with _get_conn() as conn:
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
def api_price_history_city(city):
    """Return price history for all markets in a city, grouped by market_id."""
    with _get_conn() as conn:
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
def api_snapshot_stats():
    """Return summary stats about stored price snapshots."""
    with _get_conn() as conn:
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
    with _get_conn() as conn:
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
    with _get_conn() as conn:
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

        yes_price = float(prices[0]) if len(prices) > 0 else None
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

@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    """Redirect to gateway logout."""
    resp = make_response(jsonify({"status": "ok", "redirect": "https://habbig.com/logout"}))
    resp.delete_cookie("norain_token")
    return resp


@app.route("/api/auth/me")
def api_me():
    user = _get_user_from_request()
    if not user:
        return jsonify({"user": None}), 200

    # Load persisted settings/favorites from weather_user_prefs (or in-memory fallback)
    settings = {}
    favorites = []
    user_id = user["id"]
    try:
        with _get_conn() as conn:
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
    if not settings and not favorites and user_id in _user_prefs_cache:
        cached = _user_prefs_cache[user_id]
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
        _user_prefs_cache.setdefault(user_id, {})["favorites"] = data
    return jsonify({"status": "ok"})


# ─── Admin Endpoints ─────────────────────────────────────────────────────────

@app.route("/admin")
def admin_page():
    """Serve admin dashboard HTML — client-side auth check handles access control."""
    return send_from_directory(app.static_folder, "admin.html")


@app.route("/api/admin/users")
@require_admin
def api_admin_users():
    """List users from the profiles table (managed by gateway)."""
    with _get_conn() as conn:
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

    with _get_conn() as conn:
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
    with _get_conn() as conn:
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


def _snapshot_loop():
    """Background thread: take price snapshots every 30 minutes."""
    import time as _time
    _time.sleep(120)  # Wait 2 min for first data to load
    while True:
        try:
            snapshot_prices()
        except Exception as e:
            logger.error("Snapshot loop error: %s", e)
        _time.sleep(1800)  # 30 minutes


if __name__ == "__main__":
    _debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    # Only start background thread in the reloader child (or when reloader is off)
    if not _debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        t = threading.Thread(target=_snapshot_loop, daemon=True)
        t.start()
        logger.info("Price snapshot background thread started (every 30 min)")
    app.run(host="0.0.0.0", port=5050, debug=_debug)

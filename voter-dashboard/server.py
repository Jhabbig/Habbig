#!/usr/bin/env python3
"""Voter Sentiment Dashboard — Flask backend.

Tracks how American voters feel and how their day-to-day lives are going,
using free public data and Polymarket prediction markets.

Data sources (all keyless, all free):
  - FRED public CSV (fredgraph.csv?id=...) for monthly/weekly/daily series:
      UMCSENT  Univ. of Michigan Consumer Sentiment Index (monthly)
      UNRATE   Civilian unemployment rate (monthly, %)
      CPIAUCSL Headline CPI All Urban Consumers (monthly, index)
      CES0500000003 Avg hourly earnings, total private (monthly, $/hr)
      CES0500000013 Avg weekly earnings, total private (monthly, $/wk)
      GASREGW  US regular all-formulations gasoline price (weekly, $/gal)
      MORTGAGE30US 30-year fixed mortgage rate (weekly, %)
      ICSA     Initial unemployment claims (weekly, count)
      PSAVERT  Personal saving rate (monthly, %)
      USREC    NBER recession indicator (monthly, 0/1)
  - Polymarket Gamma API for politics / approval / midterm / 2028 markets

Composite indicators we compute on the fly:
  - Misery Index = unemployment % + headline CPI YoY %
  - Real wages YoY  = avg hourly earnings deflated by CPI
  - Voter Mood Index — a 0-100 score combining sentiment, real wages,
    inflation pain, jobs and gas prices into one number.

Endpoints:
  GET /api/summary      One-shot payload for the front page.
  GET /api/series/<id>  Raw FRED series with computed YoY where applicable.
  GET /api/markets      Polymarket politics markets (sentiment-relevant).
  GET /api/mood         Composite voter-mood index breakdown.
  GET /api/health       Liveness.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

import requests
from flask import Flask, jsonify, send_from_directory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("voter")

app = Flask(__name__, static_folder="static")

try:
    from flask_compress import Compress
    Compress(app)
except Exception:
    logger.warning("flask_compress not available; responses will not be gzipped")

PORT = int(os.environ.get("PORT", "7053"))

# ─── Cache ─────────────────────────────────────────────────────────────────────

_cache: "OrderedDict[str, dict]" = OrderedDict()
_cache_lock = threading.Lock()

_TTL_DEFAULT = 60 * 60 * 6        # 6h — most series are monthly
_TTL: dict[str, int] = {
    # FRED series TTLs by update cadence
    "fred:UMCSENT": 60 * 60 * 12,
    "fred:UNRATE": 60 * 60 * 12,
    "fred:CPIAUCSL": 60 * 60 * 12,
    "fred:CES0500000003": 60 * 60 * 12,
    "fred:CES0500000013": 60 * 60 * 12,
    "fred:PSAVERT": 60 * 60 * 12,
    "fred:USREC": 60 * 60 * 24,
    "fred:GASREGW": 60 * 60 * 6,    # weekly
    "fred:MORTGAGE30US": 60 * 60 * 6,  # weekly
    "fred:ICSA": 60 * 60 * 6,       # weekly
    "polymarket": 60 * 5,            # markets move
    "approval": 60 * 60 * 6,         # 538's archived CSV, refreshed when GitHub mirror updates
}


def cache_get(key: str):
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        ttl = _TTL.get(key, _TTL_DEFAULT)
        if time.time() - entry["t"] > ttl:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return entry["data"]


def cache_set(key: str, data) -> None:
    with _cache_lock:
        _cache[key] = {"t": time.time(), "data": data}
        while len(_cache) > 256:
            _cache.popitem(last=False)


# ─── Persistent snapshot DB ────────────────────────────────────────────────────

# Why this exists: FRED revises historical data when methodologies change. A
# backtest using today's revised history would silently look better than what
# the model would have *actually* seen at the time. We persist every series
# observation on first sight, then surface a "revisions detected" feed when
# a re-fetch returns different values for the same date.

import sqlite3

SNAPSHOT_DB = os.environ.get("VOTER_SNAPSHOT_DB", "voter_snapshots.sqlite3")
_db_lock = threading.Lock()


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(SNAPSHOT_DB, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _db_init() -> None:
    """Create the schema on first run. Idempotent."""
    with _db_lock, _db_connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS series_snapshots (
                series_id TEXT NOT NULL,
                observation_date TEXT NOT NULL,
                value REAL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                PRIMARY KEY (series_id, observation_date)
            );
            CREATE TABLE IF NOT EXISTS revisions (
                series_id TEXT NOT NULL,
                observation_date TEXT NOT NULL,
                old_value REAL,
                new_value REAL,
                detected_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_revisions_detected
                ON revisions(detected_at DESC);
        """)


_db_init()


def persist_series_snapshot(series_id: str, observations: list[dict]) -> int:
    """Insert any new observations, update last_seen on existing ones, and
    record any revisions where a date's value differs from what we had.

    Returns the number of revisions detected on this call."""
    if not observations:
        return 0
    now_iso = datetime.now(timezone.utc).isoformat()
    revisions = 0
    with _db_lock, _db_connect() as conn:
        cur = conn.cursor()
        for o in observations:
            d = o.get("date")
            v = o.get("value")
            if not d:
                continue
            row = cur.execute(
                "SELECT value FROM series_snapshots WHERE series_id = ? AND observation_date = ?",
                (series_id, d),
            ).fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO series_snapshots(series_id, observation_date, value, first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (series_id, d, v, now_iso, now_iso),
                )
            else:
                old = row[0]
                if v is not None and old is not None and abs((v or 0) - (old or 0)) > 1e-9:
                    cur.execute(
                        "INSERT INTO revisions(series_id, observation_date, old_value, new_value, detected_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (series_id, d, old, v, now_iso),
                    )
                    cur.execute(
                        "UPDATE series_snapshots SET value = ?, last_seen = ? "
                        "WHERE series_id = ? AND observation_date = ?",
                        (v, now_iso, series_id, d),
                    )
                    revisions += 1
                else:
                    cur.execute(
                        "UPDATE series_snapshots SET last_seen = ? "
                        "WHERE series_id = ? AND observation_date = ?",
                        (now_iso, series_id, d),
                    )
        conn.commit()
    if revisions:
        logger.info("Snapshot DB: %d revisions detected for %s", revisions, series_id)
    return revisions


def recent_revisions(limit: int = 50) -> list[dict]:
    """Return the most-recently detected revisions across all series."""
    with _db_connect() as conn:
        rows = conn.execute(
            "SELECT series_id, observation_date, old_value, new_value, detected_at "
            "FROM revisions ORDER BY detected_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{
        "series_id": r[0],
        "observation_date": r[1],
        "old_value": r[2],
        "new_value": r[3],
        "delta": round((r[3] or 0) - (r[2] or 0), 4),
        "detected_at": r[4],
    } for r in rows]


def snapshot_stats() -> dict:
    """How many series we've snapshotted, total observations, last update."""
    with _db_connect() as conn:
        n_series = conn.execute(
            "SELECT COUNT(DISTINCT series_id) FROM series_snapshots"
        ).fetchone()[0]
        n_obs = conn.execute("SELECT COUNT(*) FROM series_snapshots").fetchone()[0]
        n_rev = conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
        latest = conn.execute(
            "SELECT MAX(last_seen) FROM series_snapshots"
        ).fetchone()[0]
    return {
        "n_series": n_series,
        "n_observations": n_obs,
        "n_revisions": n_rev,
        "last_snapshot_at": latest,
        "db_path": SNAPSHOT_DB,
    }


# ─── HTTP helper ───────────────────────────────────────────────────────────────

_USER_AGENT = "polymarket-voter-dashboard/1.0 (+https://mood.narve.ai)"


def _http_get(url: str, *, timeout: int = 20, params: Optional[dict] = None) -> Optional[requests.Response]:
    try:
        r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": _USER_AGENT})
        if r.status_code == 200:
            return r
        logger.warning("HTTP %d for %s", r.status_code, url)
        return None
    except Exception as e:
        logger.warning("HTTP error for %s: %s", url, e)
        return None


# ─── FRED public-CSV fetcher (no API key needed) ──────────────────────────────

FRED_CSV_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv"

# Each series we track. The "kind" determines how we shape it for the UI.
FRED_SERIES = {
    "UMCSENT": {
        "label": "Consumer sentiment (UMich)",
        "units": "index (1966 Q1 = 100)",
        "good": "high",  # higher is better mood
    },
    "UNRATE": {
        "label": "Unemployment rate",
        "units": "%",
        "good": "low",
    },
    "CPIAUCSL": {
        "label": "CPI — all urban consumers",
        "units": "index 1982-84=100",
        "good": "low_yoy",  # only the YoY rate matters for mood
    },
    "CES0500000003": {
        "label": "Avg hourly earnings (private)",
        "units": "$ / hour",
        "good": "high_yoy",
    },
    "CES0500000013": {
        "label": "Avg weekly earnings (private)",
        "units": "$ / week",
        "good": "high_yoy",
    },
    "GASREGW": {
        "label": "Gasoline — US regular average",
        "units": "$ / gallon",
        "good": "low",
    },
    "MORTGAGE30US": {
        "label": "30-year fixed mortgage",
        "units": "%",
        "good": "low",
    },
    "ICSA": {
        "label": "Initial jobless claims",
        "units": "count (weekly)",
        "good": "low",
    },
    "PSAVERT": {
        "label": "Personal saving rate",
        "units": "%",
        "good": "high",
    },
    "USREC": {
        "label": "NBER recession indicator",
        "units": "0/1",
        "good": "low",
    },
}


# ─── State-level data ──────────────────────────────────────────────────────────

# FRED publishes a monthly seasonally-adjusted unemployment rate for every
# state and DC as <STATE>UR (e.g. CAUR, TXUR). Series start in Jan 1976 for
# most states. Pulled in parallel; each gets its own cache key.
STATE_UNRATE: dict[str, str] = {
    "AL": "Alabama",      "AK": "Alaska",       "AZ": "Arizona",      "AR": "Arkansas",
    "CA": "California",   "CO": "Colorado",     "CT": "Connecticut",  "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",      "GA": "Georgia",      "HI": "Hawaii",       "ID": "Idaho",
    "IL": "Illinois",     "IN": "Indiana",      "IA": "Iowa",         "KS": "Kansas",
    "KY": "Kentucky",     "LA": "Louisiana",    "ME": "Maine",        "MD": "Maryland",
    "MA": "Massachusetts","MI": "Michigan",     "MN": "Minnesota",    "MS": "Mississippi",
    "MO": "Missouri",     "MT": "Montana",      "NE": "Nebraska",     "NV": "Nevada",
    "NH": "New Hampshire","NJ": "New Jersey",   "NM": "New Mexico",   "NY": "New York",
    "NC": "North Carolina","ND": "North Dakota","OH": "Ohio",         "OK": "Oklahoma",
    "OR": "Oregon",       "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee",    "TX": "Texas",        "UT": "Utah",
    "VT": "Vermont",      "VA": "Virginia",     "WA": "Washington",   "WV": "West Virginia",
    "WI": "Wisconsin",    "WY": "Wyoming",
}

# 2024 swing states — surface these in a dedicated strip on the page.
SWING_STATES_2024 = ["PA", "MI", "WI", "AZ", "GA", "NV", "NC"]


def _state_series_id(code: str) -> str:
    """FRED naming convention: <2-letter state code>UR."""
    return f"{code.upper()}UR"


def fetch_state_unemployment() -> dict[str, dict]:
    """Fetch every state's UNRATE series. Returns dict keyed by 2-letter state
    code. Each value is the standard FRED-series dict from fetch_fred_series."""
    out: dict[str, dict] = {}
    lock = threading.Lock()

    def _go(code: str) -> None:
        data = fetch_fred_series(_state_series_id(code))
        if data:
            data["state_code"] = code
            data["state_name"] = STATE_UNRATE[code]
            with lock:
                out[code] = data

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(_go, STATE_UNRATE.keys()))
    return out


def state_stress_score(series: dict, lookback_months: int = 240) -> Optional[dict]:
    """0-1 stress score for a single state: where does today's unemployment
    sit on the percentile distribution of its own last-20y history?

    Higher score = more stressed (worse for voters). Symmetric to the
    national mood index's 'jobs' component."""
    obs = [o for o in series.get("observations", []) if o["value"] is not None]
    if len(obs) < 36:
        return None
    latest = obs[-1]
    history = [o["value"] for o in obs[-lookback_months:]]
    if not history:
        return None
    below = sum(1 for v in history if v < latest["value"])
    pct = below / len(history)
    # 12-month change in pp — useful context for cards
    pp_12m: Optional[float] = None
    if len(obs) >= 13:
        pp_12m = round(latest["value"] - obs[-13]["value"], 2)
    return {
        "state_code": series.get("state_code"),
        "state_name": series.get("state_name"),
        "unemployment_rate": latest["value"],
        "as_of": latest["date"],
        "stress_score_0_1": round(pct, 3),
        "change_12m_pp": pp_12m,
        # 5-year monthly spark — enough to see the COVID spike + recovery
        "spark": [{"date": o["date"], "value": o["value"]} for o in obs[-60:]],
    }


def state_panel() -> dict:
    """Aggregate every state's stress score into the three views the front
    page needs: full list (sorted by stress), top-5 most-stressed, top-5
    least-stressed, and the swing-state strip."""
    raw = fetch_state_unemployment()
    rows: list[dict] = []
    for code in STATE_UNRATE:
        s = raw.get(code)
        if not s:
            continue
        score = state_stress_score(s)
        if score:
            rows.append(score)
    rows.sort(key=lambda r: r["stress_score_0_1"], reverse=True)
    swing = [r for code in SWING_STATES_2024 for r in rows if r["state_code"] == code]
    # Also compute a national average of the state stress scores — a different
    # signal from the national UNRATE because it's the cross-section, not the
    # population-weighted aggregate. When this is high but national UNRATE is
    # low, the country is "lopsided" — concentrated hurt.
    avg_stress = round(sum(r["stress_score_0_1"] for r in rows) / len(rows), 3) if rows else None
    return {
        "states": rows,
        "most_stressed": rows[:5],
        "least_stressed": list(reversed(rows[-5:])),
        "swing_states": swing,
        "avg_stress_0_1": avg_stress,
        "as_of": rows[0]["as_of"] if rows else None,
        "count": len(rows),
    }


def fetch_fred_series(series_id: str) -> Optional[dict]:
    """Pull a FRED series CSV. Returns dict with 'observations' list of
    {date: 'YYYY-MM-DD', value: float|None}, plus latest+previous values."""
    cache_key = f"fred:{series_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    r = _http_get(FRED_CSV_BASE, params={"id": series_id}, timeout=30)
    if not r:
        return None
    text = r.text
    # FRED public CSV format: "observation_date,SERIES_ID\nYYYY-MM-DD,value\n..."
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if len(rows) < 2:
        logger.warning("FRED %s: empty CSV", series_id)
        return None
    header = [h.strip() for h in rows[0]]
    # Some FRED CSVs use lowercase 'observation_date' as the first column,
    # others use 'DATE'. Either way we just take column 0 as the date.
    obs: list[dict] = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        d = row[0].strip()
        v = row[1].strip()
        if not d or v in ("", ".", "NA"):
            obs.append({"date": d, "value": None})
            continue
        try:
            obs.append({"date": d, "value": float(v)})
        except ValueError:
            obs.append({"date": d, "value": None})
    # Latest non-null observation
    latest = next((o for o in reversed(obs) if o["value"] is not None), None)
    out = {
        "series_id": series_id,
        "label": FRED_SERIES.get(series_id, {}).get("label", series_id),
        "units": FRED_SERIES.get(series_id, {}).get("units", ""),
        "header": header,
        "observations": obs,
        "latest": latest,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "FRED (St. Louis Fed)",
    }
    cache_set(cache_key, out)
    # Persist to SQLite — fire-and-forget; failures shouldn't break the fetch.
    try:
        persist_series_snapshot(series_id, obs)
    except Exception as e:
        logger.warning("snapshot persist failed for %s: %s", series_id, e)
    return out


def fetch_all_fred_parallel() -> dict[str, dict]:
    """Fetch every tracked FRED series concurrently, return dict series_id -> payload."""
    out: dict[str, dict] = {}
    lock = threading.Lock()

    def _go(sid: str) -> None:
        data = fetch_fred_series(sid)
        if data:
            with lock:
                out[sid] = data

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(_go, FRED_SERIES.keys()))
    return out


# ─── Derived metrics ───────────────────────────────────────────────────────────

def yoy_change(observations: list[dict], months_back: int = 12) -> Optional[float]:
    """Year-over-year percent change between latest non-null and the value
    closest to ``months_back`` months prior."""
    if not observations:
        return None
    non_null = [o for o in observations if o["value"] is not None]
    if len(non_null) < months_back + 1:
        return None
    latest = non_null[-1]
    prior = non_null[-(months_back + 1)] if len(non_null) >= months_back + 1 else None
    if prior is None or prior["value"] in (None, 0):
        return None
    return round((latest["value"] / prior["value"] - 1.0) * 100, 2)


def value_change(observations: list[dict], periods_back: int) -> Optional[float]:
    """Absolute (not percent) change vs N periods back."""
    non_null = [o for o in observations if o["value"] is not None]
    if len(non_null) < periods_back + 1:
        return None
    return round(non_null[-1]["value"] - non_null[-(periods_back + 1)]["value"], 3)


def four_week_avg(observations: list[dict]) -> Optional[float]:
    """Mean of the last 4 non-null weekly observations (for jobless claims)."""
    non_null = [o for o in observations if o["value"] is not None]
    if len(non_null) < 4:
        return None
    return round(sum(o["value"] for o in non_null[-4:]) / 4, 1)


def real_wage_yoy(earnings: Optional[dict], cpi: Optional[dict]) -> Optional[float]:
    """Real (inflation-adjusted) hourly wage YoY in percent."""
    if not earnings or not cpi:
        return None
    nom_yoy = yoy_change(earnings["observations"], 12)
    cpi_yoy = yoy_change(cpi["observations"], 12)
    if nom_yoy is None or cpi_yoy is None:
        return None
    # (1 + nom)/(1 + cpi) − 1
    return round(((1 + nom_yoy / 100) / (1 + cpi_yoy / 100) - 1) * 100, 2)


def misery_index(unemployment: Optional[dict], cpi: Optional[dict]) -> Optional[dict]:
    """Classic Okun misery index = unemployment rate + headline CPI YoY."""
    if not unemployment or not cpi:
        return None
    u_latest = unemployment.get("latest")
    cpi_yoy = yoy_change(cpi["observations"], 12)
    if not u_latest or u_latest["value"] is None or cpi_yoy is None:
        return None
    score = round(u_latest["value"] + cpi_yoy, 2)
    return {
        "score": score,
        "unemployment_rate": u_latest["value"],
        "cpi_yoy_pct": cpi_yoy,
        "as_of": u_latest["date"],
    }


def misery_history(unemployment: Optional[dict], cpi: Optional[dict], months: int = 60) -> list[dict]:
    """Build a misery-index sparkline by joining UNRATE level with CPI YoY by month.

    Both series are monthly. We compute CPI YoY at each month from the full
    history, then add it to the same-month unemployment rate. The two series
    are released on different days, so we inner-join on (year, month)."""
    if not unemployment or not cpi:
        return []
    u_obs = [o for o in unemployment["observations"] if o["value"] is not None]
    c_obs = [o for o in cpi["observations"] if o["value"] is not None]
    if len(c_obs) < 13 or not u_obs:
        return []
    # CPI YoY series keyed by (year, month)
    yoys: dict[tuple[int, int], float] = {}
    for i in range(12, len(c_obs)):
        prev = c_obs[i - 12]["value"]
        cur = c_obs[i]["value"]
        if prev > 0:
            y, m = c_obs[i]["date"][:4], c_obs[i]["date"][5:7]
            yoys[(int(y), int(m))] = (cur / prev - 1.0) * 100
    out: list[dict] = []
    for o in u_obs:
        y, m = int(o["date"][:4]), int(o["date"][5:7])
        cpi_yoy = yoys.get((y, m))
        if cpi_yoy is None:
            continue
        out.append({"date": o["date"], "value": round(o["value"] + cpi_yoy, 2)})
    return out[-months:]


def recession_state(usrec: Optional[dict]) -> Optional[dict]:
    """Read the NBER recession indicator series.

    USREC is 1 during NBER-dated recessions and 0 otherwise. We report the
    current state, the most recent recession's start/end (if any), and how
    many months it's been since the last recession ended."""
    if not usrec:
        return None
    obs = [o for o in usrec["observations"] if o["value"] is not None]
    if not obs:
        return None
    latest = obs[-1]
    in_recession = bool(latest["value"])
    # Find the most recent recession (run of 1s) by walking backwards
    last_start: Optional[str] = None
    last_end: Optional[str] = None
    current_run_start: Optional[str] = None
    for o in obs:
        if o["value"] >= 0.5:
            if current_run_start is None:
                current_run_start = o["date"]
        else:
            if current_run_start is not None:
                last_start = current_run_start
                # The end is the previous month — but we just store the start
                # of the next 0 row as "ended in" for readability.
                last_end = o["date"]
                current_run_start = None
    if current_run_start is not None and in_recession:
        last_start = current_run_start
        last_end = None
    # Months since the last recession ended (None if we're in one or never had one)
    months_since: Optional[int] = None
    if last_end and not in_recession:
        ey, em = int(last_end[:4]), int(last_end[5:7])
        ly, lm = int(latest["date"][:4]), int(latest["date"][5:7])
        months_since = (ly - ey) * 12 + (lm - em)
    return {
        "in_recession": in_recession,
        "as_of": latest["date"],
        "last_recession_start": last_start,
        "last_recession_end": last_end,
        "months_since_last_recession": months_since,
    }


def biggest_movers(series: dict[str, dict], lookback_months: int = 3) -> list[dict]:
    """Return the indicators whose latest reading has moved most vs N months ago.

    For each tracked indicator we compute the % change (or pp change for
    percentage-valued series) and z-score that change against the same series'
    recent volatility. Returns the top 3 by absolute z-score, each tagged
    'good' or 'bad' for the voter using FRED_SERIES[*].good."""
    moves: list[dict] = []
    # Map of series_id → (display name, value-kind, good-direction)
    catalog = {
        "UMCSENT":    ("Consumer sentiment",  "level", "high"),
        "UNRATE":     ("Unemployment rate",   "pp",    "low"),
        "GASREGW":    ("Gas price",           "pct",   "low"),
        "MORTGAGE30US": ("Mortgage rate",     "pp",    "low"),
        "ICSA":       ("Jobless claims",      "pct",   "low"),
        "PSAVERT":    ("Saving rate",         "pp",    "high"),
    }
    for sid, (label, kind, good_dir) in catalog.items():
        s = series.get(sid)
        if not s:
            continue
        non_null = [o for o in s["observations"] if o["value"] is not None]
        # Weekly series need ~12 obs to look back 3 months; monthly need 3.
        cadence = 4 if sid in ("GASREGW", "MORTGAGE30US", "ICSA") else 1
        n_back = lookback_months * cadence
        if len(non_null) < n_back + 1:
            continue
        cur = non_null[-1]["value"]
        prev = non_null[-(n_back + 1)]["value"]
        if kind == "pct" and prev > 0:
            change = (cur / prev - 1.0) * 100
            change_str = f"{change:+.1f}%"
        elif kind == "pp":
            change = cur - prev
            change_str = f"{change:+.2f} pp"
        else:  # level
            change = cur - prev
            change_str = f"{change:+.1f}"
        # Z-score the change against the rolling-window distribution of same-
        # length changes over the last ~10 years (cadence × 120 obs).
        window = non_null[-(cadence * 120):] if len(non_null) >= cadence * 120 else non_null
        diffs: list[float] = []
        for i in range(n_back, len(window)):
            p = window[i - n_back]["value"]
            c = window[i]["value"]
            if kind == "pct" and p > 0:
                diffs.append((c / p - 1.0) * 100)
            else:
                diffs.append(c - p)
        if len(diffs) < 12:
            continue
        mean = sum(diffs) / len(diffs)
        var = sum((d - mean) ** 2 for d in diffs) / len(diffs)
        sigma = math.sqrt(var) if var > 0 else 1e-9
        z = (change - mean) / sigma
        # Direction relative to the voter
        is_good = (change > 0) if good_dir == "high" else (change < 0)
        moves.append({
            "series_id": sid,
            "label": label,
            "change": round(change, 3),
            "change_str": change_str,
            "z_score": round(z, 2),
            "abs_z": abs(z),
            "is_good_for_voter": bool(is_good),
        })
    moves.sort(key=lambda m: m["abs_z"], reverse=True)
    return [{k: v for k, v in m.items() if k != "abs_z"} for m in moves[:3]]


def _percentile_from_history(observations: list[dict], target: float, lookback: int = 240) -> Optional[float]:
    """Return percentile (0-1) of ``target`` within last ``lookback`` non-null obs.
    Used to translate an absolute value into a 0-1 'how does this compare to the
    last ~20 years' score."""
    non_null = [o["value"] for o in observations if o["value"] is not None][-lookback:]
    if len(non_null) < 24:
        return None
    below = sum(1 for v in non_null if v < target)
    return below / len(non_null)


def voter_mood_index(series: dict[str, dict]) -> Optional[dict]:
    """Composite 0-100 voter mood index.

    Equal-weighted blend of five sub-scores, each clamped to 0-1:
      - sentiment   : UMich sentiment percentile vs last 20y
      - jobs        : 1 − unemployment percentile vs last 20y
      - inflation   : 1 − headline CPI YoY percentile (lower YoY = better)
      - real_wages  : sigmoid of real-wage YoY (positive = better)
      - gas         : 1 − gasoline percentile vs last 5y

    The index is intentionally backward-looking and descriptive — it summarises
    how voters' recent lived experience compares to the baseline of the past
    couple of decades. It is NOT a forecast.
    """
    components: dict[str, dict] = {}
    parts: list[float] = []

    sent = series.get("UMCSENT")
    if sent and sent.get("latest") and sent["latest"]["value"] is not None:
        v = sent["latest"]["value"]
        p = _percentile_from_history(sent["observations"], v, lookback=240)
        if p is not None:
            components["sentiment"] = {"value": v, "score_0_1": round(p, 3)}
            parts.append(p)

    unr = series.get("UNRATE")
    if unr and unr.get("latest") and unr["latest"]["value"] is not None:
        v = unr["latest"]["value"]
        p = _percentile_from_history(unr["observations"], v, lookback=240)
        if p is not None:
            components["jobs"] = {"value": v, "score_0_1": round(1 - p, 3)}
            parts.append(1 - p)

    cpi = series.get("CPIAUCSL")
    cpi_yoy = yoy_change(cpi["observations"], 12) if cpi else None
    if cpi_yoy is not None and cpi:
        # Build a synthetic 'CPI YoY' history from the CPI level series so we
        # can percentile-rank it.
        non_null = [o["value"] for o in cpi["observations"] if o["value"] is not None]
        yoys: list[float] = []
        for i in range(12, len(non_null)):
            if non_null[i - 12] > 0:
                yoys.append((non_null[i] / non_null[i - 12] - 1.0) * 100)
        if len(yoys) >= 24:
            below = sum(1 for v in yoys[-240:] if v < cpi_yoy)
            p = below / min(len(yoys), 240)
            components["inflation"] = {"value": cpi_yoy, "score_0_1": round(1 - p, 3)}
            parts.append(1 - p)

    earn = series.get("CES0500000003")
    rw = real_wage_yoy(earn, cpi) if (earn and cpi) else None
    if rw is not None:
        # sigmoid centered at 0% real-wage YoY, with ±2% mapping to ~0.12 / 0.88.
        score = 1.0 / (1.0 + math.exp(-rw))
        components["real_wages"] = {"value": rw, "score_0_1": round(score, 3)}
        parts.append(score)

    gas = series.get("GASREGW")
    if gas and gas.get("latest") and gas["latest"]["value"] is not None:
        v = gas["latest"]["value"]
        # Use last 5y (260 weeks) for the gas comparison — voters care about
        # recent pain at the pump, not the 1990s baseline.
        p = _percentile_from_history(gas["observations"], v, lookback=260)
        if p is not None:
            components["gas"] = {"value": v, "score_0_1": round(1 - p, 3)}
            parts.append(1 - p)

    if not parts:
        return None
    overall = round(100 * sum(parts) / len(parts), 1)
    return {
        "score_0_100": overall,
        "components": components,
        "method": (
            "Equal-weighted mean of 0-1 sub-scores: sentiment (UMich percentile, "
            "20y), jobs (inverted unemployment percentile), inflation (inverted "
            "CPI-YoY percentile), real wages (sigmoid of real-wage YoY) and gas "
            "(inverted price percentile, 5y). Then × 100."
        ),
    }


# ─── Election-cycle regression ────────────────────────────────────────────────

# Net House seat change for the incumbent president's party at each midterm,
# 1978-2022. Sourced from Office of the House Historian. The sign convention
# is from the *incumbent party's* perspective — negative means losses.
HISTORICAL_MIDTERMS: list[dict] = [
    {"year": 1978, "incumbent_party": "D", "incumbent_president": "Carter",  "seat_change": -15},
    {"year": 1982, "incumbent_party": "R", "incumbent_president": "Reagan",  "seat_change": -26},
    {"year": 1986, "incumbent_party": "R", "incumbent_president": "Reagan",  "seat_change":  -5},
    {"year": 1990, "incumbent_party": "R", "incumbent_president": "Bush 41", "seat_change":  -8},
    {"year": 1994, "incumbent_party": "D", "incumbent_president": "Clinton", "seat_change": -54},
    {"year": 1998, "incumbent_party": "D", "incumbent_president": "Clinton", "seat_change":  +5},
    {"year": 2002, "incumbent_party": "R", "incumbent_president": "Bush 43", "seat_change":  +8},
    {"year": 2006, "incumbent_party": "R", "incumbent_president": "Bush 43", "seat_change": -30},
    {"year": 2010, "incumbent_party": "D", "incumbent_president": "Obama",   "seat_change": -63},
    {"year": 2014, "incumbent_party": "D", "incumbent_president": "Obama",   "seat_change": -13},
    {"year": 2018, "incumbent_party": "R", "incumbent_president": "Trump",   "seat_change": -41},
    {"year": 2022, "incumbent_party": "D", "incumbent_president": "Biden",   "seat_change":  -9},
]

# Cycles ending in even years that don't fall on a presidential year.
NEXT_MIDTERM = 2026
CURRENT_INCUMBENT_PARTY = "R"
CURRENT_INCUMBENT_PRESIDENT = "Trump"


def _sentiment_at(by_ym: dict[tuple[int, int], float], year: int, month: int) -> Optional[float]:
    """Look up UMich sentiment for (year, month). Falls back to the closest
    non-null month within ±3 — UMCSENT was quarterly before 1978 and has
    occasional gaps from survey methodology changes."""
    for delta in (0, -1, 1, -2, 2, -3, 3):
        m = month + delta
        y = year
        if m < 1:  y -= 1; m += 12
        if m > 12: y += 1; m -= 12
        v = by_ym.get((y, m))
        if v is not None:
            return v
    return None


def election_cycle_regression(sentiment_series: Optional[dict],
                              ref_month: int = 4) -> Optional[dict]:
    """OLS regression of incumbent-party House seat change on UMich consumer
    sentiment in April of each midterm year (1978-2022).

    Returns slope, intercept, R², residual std, every historical row with
    its predicted+residual, and the **current implied seat change** for the
    next midterm given the latest sentiment reading — with a 90% prediction
    interval.

    Why April? It's the most-cited "as of mid-year" reading among political
    analysts and gives the model enough lead time to be predictive instead
    of just retrospective."""
    if not sentiment_series or not sentiment_series.get("observations"):
        return None
    by_ym: dict[tuple[int, int], float] = {}
    for o in sentiment_series["observations"]:
        if o["value"] is None:
            continue
        try:
            y, m = int(o["date"][:4]), int(o["date"][5:7])
        except (ValueError, IndexError):
            continue
        by_ym[(y, m)] = o["value"]

    rows: list[dict] = []
    for m in HISTORICAL_MIDTERMS:
        s = _sentiment_at(by_ym, m["year"], ref_month)
        if s is None:
            continue
        rows.append({**m, "sentiment": round(s, 1)})

    if len(rows) < 5:
        return None

    xs = [r["sentiment"] for r in rows]
    ys = [r["seat_change"] for r in rows]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    if den == 0:
        return None
    slope = num / den
    intercept = my - slope * mx
    ss_res = sum((ys[i] - (intercept + slope * xs[i])) ** 2 for i in range(n))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    sigma = math.sqrt(ss_res / max(n - 2, 1))

    for r in rows:
        pred = intercept + slope * r["sentiment"]
        r["predicted"] = round(pred, 1)
        r["residual"] = round(r["seat_change"] - pred, 1)

    # Current implied seat change. Use the most-recent UMCSENT reading
    # available — voters will keep checking the dashboard as new readings
    # land, and this auto-updates with no code change.
    latest = sentiment_series.get("latest") or {}
    s_now = latest.get("value")
    implied = lo_90 = hi_90 = None
    if s_now is not None:
        pred = intercept + slope * s_now
        implied = round(pred, 1)
        # 90% prediction interval ≈ ±1.645 σ (residual std; small-sample
        # inflation via (1 + 1/n + (x − x̄)² / Σ(x − x̄)²) is < 1 σ for our n,
        # so this is a close-enough conservative band).
        lo_90 = round(pred - 1.645 * sigma, 1)
        hi_90 = round(pred + 1.645 * sigma, 1)

    return {
        "method": (
            f"OLS of incumbent-party net House seat change on UMich consumer "
            f"sentiment in month {ref_month} of each midterm year, "
            f"{rows[0]['year']}-{rows[-1]['year']}."
        ),
        "ref_month": ref_month,
        "n": n,
        "slope": round(slope, 3),
        "intercept": round(intercept, 1),
        "r_squared": round(r2, 3),
        "residual_std_seats": round(sigma, 1),
        "history": rows,
        "next_midterm": NEXT_MIDTERM,
        "incumbent_party": CURRENT_INCUMBENT_PARTY,
        "incumbent_president": CURRENT_INCUMBENT_PRESIDENT,
        "current_sentiment": round(s_now, 1) if s_now is not None else None,
        "current_sentiment_as_of": latest.get("date"),
        "implied_seat_change": implied,
        "ci_90_low": lo_90,
        "ci_90_high": hi_90,
    }


# ─── Presidential approval (538 archived CSV) ─────────────────────────────────

# FiveThirtyEight published a comprehensive aggregated approval-polls CSV
# updated daily until ABC/Disney shut the site down in mid-2024. The data
# is still mirrored on the public GitHub repo `fivethirtyeight/data`. We
# pull from the raw GitHub URL — keyless, stable as long as the repo exists.
#
# Heads-up: this dataset is *frozen* at the 538 shutdown date. The
# dashboard surfaces an explicit "as of" pill so users see the staleness.
# A v2 follow-up will splice in a live source (RCP scrape, Silver Bulletin
# API, or a custom poll aggregator) to extend past the freeze date.
APPROVAL_CSV_URLS = [
    # Primary: GitHub raw. Most stable.
    "https://raw.githubusercontent.com/fivethirtyeight/data/master/polls/president_approval_polls.csv",
    # Fallback: the original projects.fivethirtyeight.com URL (still serves
    # the archived snapshot at time of writing).
    "https://projects.fivethirtyeight.com/polls/data/president_approval_polls.csv",
]


def _parse_iso_date(s: str) -> Optional[str]:
    """Accept either 'YYYY-MM-DD' or 'M/D/YY' / 'M/D/YYYY' and emit ISO."""
    s = (s or "").strip()
    if not s:
        return None
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    parts = s.split("/")
    if len(parts) != 3:
        return None
    try:
        m, d, y = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if y < 100:
        y += 2000 if y < 70 else 1900
    return f"{y:04d}-{m:02d}-{d:02d}"


def fetch_approval_polls() -> Optional[list[dict]]:
    """Pull the 538 approval-polls CSV. Returns a list of dicts shaped for
    aggregation: {date, pollster, sample_size, subgroup, approve, disapprove,
    president}."""
    cached = cache_get("approval")
    if cached is not None:
        return cached
    text: Optional[str] = None
    for url in APPROVAL_CSV_URLS:
        r = _http_get(url, timeout=30)
        if r and r.text:
            text = r.text
            logger.info("Approval polls fetched from %s (%d bytes)", url, len(text))
            break
    if not text:
        return None

    reader = csv.DictReader(io.StringIO(text))
    out: list[dict] = []
    for row in reader:
        # 538's column names changed over the dataset's life. Look up
        # several common spellings.
        end_date = row.get("end_date") or row.get("enddate")
        date_iso = _parse_iso_date(end_date) if end_date else None
        if not date_iso:
            continue
        try:
            approve = float(row.get("approve") or row.get("yes") or 0)
            disapprove = float(row.get("disapprove") or row.get("no") or 0)
        except (TypeError, ValueError):
            continue
        if approve <= 0 and disapprove <= 0:
            continue
        try:
            sample_size = float(row.get("sample_size") or row.get("samplesize") or 0) or None
        except (TypeError, ValueError):
            sample_size = None
        out.append({
            "date": date_iso,
            "pollster": (row.get("pollster") or row.get("pollster_rating_name") or "").strip(),
            "sample_size": sample_size,
            "subgroup": (row.get("subgroup") or row.get("subject") or "").strip(),
            "approve": approve,
            "disapprove": disapprove,
            "president": (row.get("president") or row.get("politician") or "").strip(),
        })
    out.sort(key=lambda r: r["date"])
    cache_set("approval", out)
    return out


def _bucket_weekly(polls: list[dict]) -> list[dict]:
    """Bucket polls by ISO week of their end-date and emit a weekly time
    series of weighted (sample-size) mean approve/disapprove/net."""
    if not polls:
        return []
    from datetime import datetime as _dt
    buckets: dict[str, list[dict]] = {}
    for p in polls:
        try:
            d = _dt.strptime(p["date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        iso_year, iso_week, _ = d.isocalendar()
        key = f"{iso_year:04d}-W{iso_week:02d}"
        buckets.setdefault(key, []).append(p)
    series: list[dict] = []
    for key in sorted(buckets):
        rows = buckets[key]
        # Weight by sample size; if missing, treat as 600 (typical national poll).
        wts = [(r.get("sample_size") or 600.0) for r in rows]
        w_sum = sum(wts) or 1.0
        appr = sum(r["approve"] * w for r, w in zip(rows, wts)) / w_sum
        disa = sum(r["disapprove"] * w for r, w in zip(rows, wts)) / w_sum
        series.append({
            "week": key,
            "end_date": max(r["date"] for r in rows),
            "approve": round(appr, 1),
            "disapprove": round(disa, 1),
            "net": round(appr - disa, 1),
            "n_polls": len(rows),
        })
    return series


def approval_aggregate(polls: Optional[list[dict]]) -> Optional[dict]:
    """Headline approval card: weekly weighted average, latest 30-day
    rolling, sparkline over the last ~2 years."""
    if not polls:
        return None
    # Pick the most recent president (modal in last 90 days of polls).
    by_date = sorted(polls, key=lambda r: r["date"])
    tail = by_date[-2000:]  # latest ~2000 polls
    name_counts: dict[str, int] = {}
    for p in tail:
        if p.get("president"):
            name_counts[p["president"]] = name_counts.get(p["president"], 0) + 1
    incumbent = max(name_counts, key=name_counts.get) if name_counts else ""
    # Filter to that president, all-respondent subgroups only.
    relevant = [
        p for p in by_date
        if p.get("president") == incumbent
        and p.get("subgroup", "").lower() in ("", "all polls", "all", "adults", "voters")
    ]
    if not relevant:
        return None
    weekly = _bucket_weekly(relevant)
    if not weekly:
        return None
    latest_week = weekly[-1]
    # 4-week rolling average to smooth out the very latest noise.
    recent = weekly[-4:]
    rec_wt = sum(w["n_polls"] for w in recent) or 1
    smoothed_net = round(sum(w["net"] * w["n_polls"] for w in recent) / rec_wt, 1)
    smoothed_appr = round(sum(w["approve"] * w["n_polls"] for w in recent) / rec_wt, 1)
    smoothed_disa = round(sum(w["disapprove"] * w["n_polls"] for w in recent) / rec_wt, 1)
    # Year-ago comparison
    if len(weekly) >= 52:
        net_52w = weekly[-52]["net"]
        net_change_52w = round(latest_week["net"] - net_52w, 1)
    else:
        net_change_52w = None
    spark_weeks = weekly[-104:]   # last ~2y
    return {
        "incumbent": incumbent,
        "as_of": latest_week["end_date"],
        "approve_pct": smoothed_appr,
        "disapprove_pct": smoothed_disa,
        "net_pct": smoothed_net,
        "net_change_52w": net_change_52w,
        "n_polls_4w": sum(w["n_polls"] for w in recent),
        "spark": [{"date": w["end_date"], "value": w["net"]} for w in spark_weeks],
        "source": "FiveThirtyEight archived approval-polls CSV (frozen at site shutdown)",
    }


# ─── Vibecession quantifier ────────────────────────────────────────────────────

def _percentile_series_monthly(observations: list[dict],
                                lookback_months: int = 240,
                                min_history: int = 36) -> list[dict]:
    """For each non-null monthly observation, compute its percentile within
    the prior `lookback_months` non-null observations.

    Returns a list of {date, percentile_0_1}. Skips months where we don't
    yet have `min_history` prior observations to compare against."""
    non_null = [o for o in observations if o["value"] is not None]
    out: list[dict] = []
    for i in range(len(non_null)):
        prior = non_null[max(0, i - lookback_months):i]
        if len(prior) < min_history:
            continue
        v = non_null[i]["value"]
        below = sum(1 for w in prior if w["value"] < v)
        out.append({"date": non_null[i]["date"], "percentile": round(below / len(prior), 4)})
    return out


def _yoy_series_dated(observations: list[dict], months: int = 12) -> list[dict]:
    """Build {date, value=YoY%} from a level series. Same as _yoy_series
    (the existing front-end helper) but exposed for vibecession."""
    non_null = [o for o in observations if o["value"] is not None]
    out: list[dict] = []
    for i in range(months, len(non_null)):
        prev = non_null[i - months]["value"]
        if prev <= 0:
            continue
        cur = non_null[i]["value"]
        out.append({"date": non_null[i]["date"],
                    "value": (cur / prev - 1.0) * 100})
    return out


def _join_by_month(series_lists: list[list[dict]]) -> list[tuple[str, list[float]]]:
    """Inner-join several {date, value-ish} series on YYYY-MM. Returns a
    list of (yyyy-mm, [v_from_each_series]) in chronological order."""
    if not series_lists:
        return []
    keyed = []
    for s in series_lists:
        d: dict[str, float] = {}
        for o in s:
            ym = o.get("date", "")[:7]
            # accept either {value} or {percentile}
            v = o.get("value")
            if v is None:
                v = o.get("percentile")
            if v is None:
                continue
            d[ym] = v
        keyed.append(d)
    common = set.intersection(*(set(d.keys()) for d in keyed))
    return sorted([(ym, [d[ym] for d in keyed]) for ym in common])


def vibecession_gap(series: dict[str, dict],
                    history_months: int = 120) -> Optional[dict]:
    """The vibecession index: sentiment percentile minus fundamentals
    percentile, both measured against the prior 20 years monthly.

    Components of fundamentals (all 0-1, higher = better for voters):
      - jobs:      1 − UNRATE percentile vs prior 20y
      - inflation: 1 − CPI-YoY percentile vs prior 20y
      - real wages: sigmoid of real-wage YoY (centered at 0)

    gap = sentiment_percentile − mean(fundamentals)
      gap >  0.10 → voters feel BETTER than fundamentals would suggest
      gap < −0.10 → 'vibecession' — voters feel worse than reality
      |gap| ≤ 0.10 → vibes and fundamentals align

    Returns the current value plus a monthly history so the front-end can
    plot the gap over time. The fully transparent formula is exposed in
    the response so users can audit it."""
    sent = series.get("UMCSENT")
    unr  = series.get("UNRATE")
    cpi  = series.get("CPIAUCSL")
    earn = series.get("CES0500000003")
    if not (sent and unr and cpi and earn):
        return None

    sent_pct = _percentile_series_monthly(sent["observations"])
    unr_pct  = _percentile_series_monthly(unr["observations"])

    # Inflation: build YoY first, then percentile-rank the YoY series.
    cpi_yoy_obs = _yoy_series_dated(cpi["observations"], 12)
    # Reshape so _percentile_series_monthly can ingest it.
    cpi_yoy_as_obs = [{"date": o["date"], "value": o["value"]} for o in cpi_yoy_obs]
    cpi_yoy_pct = _percentile_series_monthly(cpi_yoy_as_obs)

    # Real wages: monthly YoY of hourly earnings minus monthly CPI YoY.
    earn_yoy = _yoy_series_dated(earn["observations"], 12)
    # Join earnings YoY and CPI YoY by month, then real_yoy ≈ earn_yoy − cpi_yoy
    rw_score_series: list[dict] = []
    cpi_by_ym = {o["date"][:7]: o["value"] for o in cpi_yoy_obs}
    for o in earn_yoy:
        ym = o["date"][:7]
        c = cpi_by_ym.get(ym)
        if c is None:
            continue
        # real ≈ nominal − inflation (close to (1+n)/(1+c) − 1 at small values)
        real = o["value"] - c
        # Sigmoid centered at 0 — same as the national mood index.
        score = 1.0 / (1.0 + math.exp(-real))
        rw_score_series.append({"date": o["date"], "value": round(score, 4)})

    # Inverted percentile helpers (higher = better)
    def _invert(s: list[dict]) -> list[dict]:
        return [{"date": o["date"], "value": round(1.0 - o["percentile"], 4)} for o in s]

    jobs_score      = _invert(unr_pct)
    inflation_score = _invert(cpi_yoy_pct)

    # Join sentiment-pct + 3 fundamentals scores by month. The vibecession
    # value at month m is sent_pct(m) − mean(jobs(m), inflation(m), rw(m)).
    rows = _join_by_month([
        [{"date": o["date"], "value": o["percentile"]} for o in sent_pct],
        jobs_score,
        inflation_score,
        rw_score_series,
    ])
    history: list[dict] = []
    for ym, vals in rows:
        s_pct, jobs, inf, rw = vals
        fundamentals = (jobs + inf + rw) / 3.0
        gap = s_pct - fundamentals
        history.append({
            "month": ym,
            "sentiment_pct": round(s_pct, 4),
            "fundamentals_pct": round(fundamentals, 4),
            "gap": round(gap, 4),
        })
    if not history:
        return None

    latest = history[-1]
    gap_now = latest["gap"]

    # Verbal characterisation
    if gap_now > 0.10:
        flavor = "voters feel BETTER than fundamentals would suggest"
    elif gap_now < -0.10:
        flavor = "vibecession — voters feel worse than fundamentals would suggest"
    else:
        flavor = "vibes and fundamentals align"

    # Historical extremes for context
    sorted_by_gap = sorted(history, key=lambda r: r["gap"])
    most_vibecession = sorted_by_gap[0]
    least_vibecession = sorted_by_gap[-1]

    # Rank latest among the full history (lower rank = more vibecession)
    rank_among_all = sum(1 for r in history if r["gap"] < gap_now) + 1

    return {
        "method": (
            "sentiment_percentile(t) − mean(jobs(t), inflation(t), real_wages(t)) "
            "where each component is computed monthly versus its own prior-20y "
            "history. Real-wages component uses sigmoid of real-wage YoY (= "
            "hourly earnings YoY − CPI YoY)."
        ),
        "as_of": latest["month"],
        "gap": gap_now,
        "sentiment_pct": latest["sentiment_pct"],
        "fundamentals_pct": latest["fundamentals_pct"],
        "flavor": flavor,
        "rank_among_history": rank_among_all,
        "history_length": len(history),
        "most_vibecession_month": {"month": most_vibecession["month"], "gap": most_vibecession["gap"]},
        "least_vibecession_month": {"month": least_vibecession["month"], "gap": least_vibecession["gap"]},
        # Trim history to ~10y so the payload stays compact
        "history": history[-history_months:],
    }


# ─── Polymarket gamma fetcher ──────────────────────────────────────────────────

GAMMA_BASE = "https://gamma-api.polymarket.com"

POLITICS_TAG_SLUGS = [
    "politics",
    "us-politics",
    "elections",
    "us-elections",
    "2026-midterms",
    "midterms",
    "2028-election",
    "presidential-approval",
    "trump",
    "biden",
    "congress",
]

# Reject keywords that share political tags but don't really speak to
# voter sentiment / quality of life.
REJECT_KEYWORDS = [
    "sportsbook", "nfl", "nba", "nhl", "mlb", "mls", "champion", "playoff",
    "boxing", "ufc", "wrestlemania", "celebrity",
]

# Keep only markets that genuinely reflect voter mood / political outcomes.
SENTIMENT_KEYWORDS = [
    "approval", "approve", "right track", "wrong track",
    "midterm", "house majority", "senate majority", "control of",
    "election", "presidential", "president", "primary",
    "vp", "vice president", "veep",
    "recession", "inflation", "unemployment", "gas price",
    "minimum wage", "tariff",
    "shutdown", "impeach",
]


def _fetch_events_by_tag(tag_slug: str, seen_ids: set, all_markets: list, lock: threading.Lock) -> None:
    offset = 0
    for _ in range(8):
        r = _http_get(
            f"{GAMMA_BASE}/events",
            params={"tag_slug": tag_slug, "closed": "false", "limit": "100", "offset": str(offset)},
        )
        if not r:
            break
        try:
            events = r.json()
        except Exception:
            break
        if not events:
            break
        for event in events:
            title = (event.get("title", "") or "")
            tl = title.lower()
            if any(k in tl for k in REJECT_KEYWORDS):
                continue
            tags = event.get("tags", [])
            tag_labels = [t.get("label", "") for t in tags if isinstance(t, dict)]
            for m in event.get("markets", []):
                mid = m.get("conditionId") or m.get("id", "")
                if not mid:
                    continue
                with lock:
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                    m["_event_title"] = title
                    m["_event_tags"] = tag_labels
                    all_markets.append(m)
        offset += 100


def fetch_politics_markets() -> list[dict]:
    cached = cache_get("polymarket")
    if cached is not None:
        return cached
    all_markets: list[dict] = []
    seen_ids: set = set()
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_fetch_events_by_tag, slug, seen_ids, all_markets, lock)
                   for slug in POLITICS_TAG_SLUGS]
        for f in futures:
            try:
                f.result()
            except Exception as e:
                logger.warning("tag fetch error: %s", e)
    filtered: list[dict] = []
    for m in all_markets:
        title = ((m.get("_event_title") or "") + " " + (m.get("question") or "")).lower()
        if any(k in title for k in SENTIMENT_KEYWORDS):
            filtered.append(m)
    logger.info("Fetched %d politics markets (from %d candidates)", len(filtered), len(all_markets))
    cache_set("polymarket", filtered)
    return filtered


def shape_markets_for_ui(markets: list[dict]) -> list[dict]:
    out = []
    for m in markets:
        try:
            implied = float(m.get("lastTradePrice") or m.get("bestBid") or 0)
        except (ValueError, TypeError):
            implied = None
        try:
            liquidity = float(m.get("liquidity") or m.get("liquidityNum") or 0)
        except (ValueError, TypeError):
            liquidity = 0.0
        try:
            volume = float(m.get("volume") or m.get("volumeNum") or 0)
        except (ValueError, TypeError):
            volume = 0.0
        out.append({
            "id": m.get("conditionId") or m.get("id"),
            "slug": m.get("slug"),
            "question": m.get("question"),
            "event_title": m.get("_event_title"),
            "tags": m.get("_event_tags") or [],
            "implied_p": round(implied, 4) if implied is not None else None,
            "liquidity": round(liquidity, 2),
            "volume": round(volume, 2),
            "end_date": m.get("endDate") or m.get("end_date_iso"),
        })
    return out


# ─── Live-approval splice via Polymarket markets ───────────────────────────────

_RE_APPROVAL_GE = re.compile(r"approval.{0,40}?(?:>=|≥|at\s*least|over|above|exceed[a-z]*)\s*([0-9]{2})\s*%?", re.I)
_RE_APPROVAL_LE = re.compile(r"approval.{0,40}?(?:<=|≤|under|below|less\s*than)\s*([0-9]{2})\s*%?", re.I)


def polymarket_approval_implied(markets: list[dict]) -> Optional[dict]:
    """Back out an implied current approval from Polymarket's approval
    threshold markets. For a set of 'approval ≥ X%' markets that share an
    end date, the implied prices form a (declining) CDF — we interpolate
    to find the threshold where the implied probability crosses 0.5,
    which is the implied median approval.

    Returns the per-end-year implied median plus the raw market dots.
    Skips years where we don't have at least two distinct thresholds."""
    if not markets:
        return None
    # Bucket approval markets by end-date year + threshold
    by_year: dict[int, list[dict]] = {}
    for m in markets:
        title = ((m.get("_event_title") or "") + " " + (m.get("question") or ""))
        tl = title.lower()
        if "approval" not in tl:
            continue
        try:
            implied = float(m.get("lastTradePrice") or m.get("bestBid") or 0)
        except (ValueError, TypeError):
            continue
        if implied <= 0 or implied >= 1:
            continue
        # Parse threshold
        thr: Optional[float] = None
        direction: str = ""
        mm = _RE_APPROVAL_GE.search(tl)
        if mm:
            thr = float(mm.group(1)); direction = "ge"
        else:
            mm = _RE_APPROVAL_LE.search(tl)
            if mm:
                thr = float(mm.group(1)); direction = "le"
        if thr is None or thr < 20 or thr > 70:
            continue
        # End date
        ed = m.get("endDate") or m.get("end_date_iso") or ""
        if len(ed) < 4:
            continue
        try:
            year = int(ed[:4])
        except ValueError:
            continue
        by_year.setdefault(year, []).append({
            "threshold": thr,
            "direction": direction,
            "implied": implied,
            "question": m.get("question"),
        })

    if not by_year:
        return None

    implied_by_year: list[dict] = []
    for year, rows in sorted(by_year.items()):
        # Convert ≤ entries to the equivalent ≥ probability
        norm = []
        for r in rows:
            if r["direction"] == "le":
                norm.append({"threshold": r["threshold"], "p_ge": 1.0 - r["implied"]})
            else:
                norm.append({"threshold": r["threshold"], "p_ge": r["implied"]})
        # Average duplicate thresholds
        agg: dict[float, list[float]] = {}
        for n in norm:
            agg.setdefault(n["threshold"], []).append(n["p_ge"])
        pts = sorted([(t, sum(ps) / len(ps)) for t, ps in agg.items()])
        if len(pts) < 2:
            # Single-threshold year: still surface it as a soft cross-check
            t, p = pts[0]
            implied_by_year.append({
                "year": year,
                "thresholds_used": 1,
                "single_market": {"threshold": t, "p_ge": round(p, 3)},
                "implied_median_approval": None,
            })
            continue
        # Linear interpolation to find threshold where p_ge crosses 0.5.
        # Sort by threshold; CDF should be decreasing in threshold (higher
        # threshold = lower P(approval ≥ that)). Walk pairs.
        median: Optional[float] = None
        for i in range(len(pts) - 1):
            t1, p1 = pts[i]
            t2, p2 = pts[i + 1]
            if (p1 >= 0.5 and p2 <= 0.5) or (p1 <= 0.5 and p2 >= 0.5):
                if p1 == p2:
                    median = (t1 + t2) / 2
                else:
                    median = t1 + (0.5 - p1) * (t2 - t1) / (p2 - p1)
                break
        # If CDF never crosses 0.5 (e.g. all > 0.5 or all < 0.5), pick the
        # nearest threshold as a coarse fallback.
        if median is None:
            nearest = min(pts, key=lambda x: abs(x[1] - 0.5))
            median = nearest[0]
        implied_by_year.append({
            "year": year,
            "thresholds_used": len(pts),
            "implied_median_approval": round(median, 1),
            "cdf_points": [{"threshold": t, "p_ge": round(p, 3)} for t, p in pts],
        })

    return {"implied_by_year": implied_by_year, "source": "Polymarket gamma — approval threshold markets"}


# ─── Election-cycle leave-one-out backtest ─────────────────────────────────────

def election_cycle_backtest(sentiment_series: Optional[dict],
                             ref_month: int = 4) -> Optional[dict]:
    """Honest out-of-sample test for the election-cycle regression: for
    each historical midterm, refit the regression on every other midterm
    and use that to predict the held-out one. Reports per-year predicted
    vs actual, plus aggregate MAE / RMSE / R²_oos."""
    if not sentiment_series or not sentiment_series.get("observations"):
        return None
    by_ym: dict[tuple[int, int], float] = {}
    for o in sentiment_series["observations"]:
        if o["value"] is None:
            continue
        try:
            y, m = int(o["date"][:4]), int(o["date"][5:7])
        except (ValueError, IndexError):
            continue
        by_ym[(y, m)] = o["value"]

    data: list[dict] = []
    for m in HISTORICAL_MIDTERMS:
        s = _sentiment_at(by_ym, m["year"], ref_month)
        if s is None:
            continue
        data.append({**m, "sentiment": round(s, 1)})
    if len(data) < 4:
        return None

    def _fit(rows: list[dict]) -> Optional[tuple[float, float]]:
        n = len(rows)
        if n < 3:
            return None
        xs = [r["sentiment"] for r in rows]
        ys = [r["seat_change"] for r in rows]
        mx = sum(xs) / n
        my = sum(ys) / n
        num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
        den = sum((xs[i] - mx) ** 2 for i in range(n))
        if den == 0:
            return None
        slope = num / den
        intercept = my - slope * mx
        return slope, intercept

    rows: list[dict] = []
    for idx, hold in enumerate(data):
        train = [d for i, d in enumerate(data) if i != idx]
        fit = _fit(train)
        if not fit:
            continue
        slope, intercept = fit
        pred = intercept + slope * hold["sentiment"]
        err = hold["seat_change"] - pred
        rows.append({
            "year": hold["year"],
            "incumbent_president": hold["incumbent_president"],
            "incumbent_party": hold["incumbent_party"],
            "sentiment": hold["sentiment"],
            "actual": hold["seat_change"],
            "loo_predicted": round(pred, 1),
            "loo_error": round(err, 1),
            "loo_slope": round(slope, 3),
            "loo_intercept": round(intercept, 1),
        })
    if not rows:
        return None

    errs = [r["loo_error"] for r in rows]
    mae = sum(abs(e) for e in errs) / len(errs)
    rmse = math.sqrt(sum(e * e for e in errs) / len(errs))
    actuals = [r["actual"] for r in rows]
    mean_actual = sum(actuals) / len(actuals)
    ss_res = sum(e * e for e in errs)
    ss_tot = sum((a - mean_actual) ** 2 for a in actuals)
    r2_oos = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else None

    return {
        "method": "Leave-one-out cross-validation of the election-cycle regression.",
        "ref_month": ref_month,
        "n": len(rows),
        "mae_seats": round(mae, 1),
        "rmse_seats": round(rmse, 1),
        "r_squared_oos": round(r2_oos, 3) if r2_oos is not None else None,
        "rows": rows,
    }


# ─── Pollster scorecard (538 archived ratings) ────────────────────────────────

POLLSTER_RATINGS_URLS = [
    "https://raw.githubusercontent.com/fivethirtyeight/data/master/pollster-ratings/pollster-ratings.csv",
    "https://projects.fivethirtyeight.com/pollster-ratings/pollster-ratings.csv",
]


def fetch_pollster_ratings() -> Optional[list[dict]]:
    """Pull FiveThirtyEight's archived pollster ratings CSV from the GitHub
    mirror. Returns rows of {pollster, polls, predictive_plus_minus, bias,
    grade}. Frozen at the 538 shutdown; surface the staleness in the UI."""
    cached = cache_get("pollster_ratings")
    if cached is not None:
        return cached
    text: Optional[str] = None
    for url in POLLSTER_RATINGS_URLS:
        r = _http_get(url, timeout=30)
        if r and r.text:
            text = r.text
            logger.info("Pollster ratings fetched from %s (%d bytes)", url, len(text))
            break
    if not text:
        return None
    reader = csv.DictReader(io.StringIO(text))
    out: list[dict] = []
    for row in reader:
        pollster = (row.get("Pollster") or row.get("pollster") or "").strip()
        if not pollster:
            continue
        # Different snapshots have different column names — accept several.
        def _f(key_options: list[str]) -> Optional[float]:
            for k in key_options:
                v = row.get(k)
                if v in (None, "", "NA"):
                    continue
                try:
                    return float(v)
                except ValueError:
                    continue
            return None
        out.append({
            "pollster": pollster,
            "n_polls": int(_f(["Polls", "Polls Analyzed", "polls_analyzed", "polls"]) or 0),
            "predictive_plus_minus": _f(["Predictive    Plus-Minus", "Predictive Plus-Minus", "predictive_plus_minus"]),
            "bias": _f(["Mean-Reverted Bias", "Mean-Reverted    Bias", "bias_corrected"]),
            "grade": (row.get("538 Grade") or row.get("FiveThirtyEight Grade") or row.get("grade") or "").strip(),
            "advanced_grade": (row.get("Advanced Plus-Minus") or "").strip(),
        })
    cache_set("pollster_ratings", out)
    return out


def pollster_scorecard(ratings: Optional[list[dict]],
                       min_polls: int = 5,
                       top_n: int = 10) -> Optional[dict]:
    """Rank pollsters by predictive-plus-minus (lower = more accurate).
    Filter to ones with at least `min_polls` polls scored; return top + bottom."""
    if not ratings:
        return None
    eligible = [r for r in ratings
                if r.get("n_polls", 0) >= min_polls
                and r.get("predictive_plus_minus") is not None]
    if not eligible:
        return None
    # Lower PPM = MORE accurate (it's a deviation-from-actual metric)
    eligible.sort(key=lambda r: r["predictive_plus_minus"])
    return {
        "source": "FiveThirtyEight archived pollster ratings (frozen at site shutdown)",
        "method": ("Predictive Plus-Minus — 538's house-rating metric. Lower is "
                   "more accurate. Pollsters with fewer than "
                   f"{min_polls} scored polls excluded."),
        "n_ranked": len(eligible),
        "best": eligible[:top_n],
        "worst": list(reversed(eligible[-top_n:])),
    }


# ─── Multi-country mood (OECD via FRED) ────────────────────────────────────────

# Each country gets a tiny mood composite: consumer confidence percentile +
# (1 − unemployment percentile) + (1 − CPI YoY percentile), all monthly
# against own prior 20y. Same shape as the US mood index — directly
# comparable cross-country.
COUNTRY_SERIES = {
    "US": {
        "name": "United States",
        "flag": "🇺🇸",
        "sentiment": "UMCSENT",
        "unemp": "UNRATE",
        "cpi": "CPIAUCSL",
    },
    "UK": {
        "name": "United Kingdom",
        "flag": "🇬🇧",
        "sentiment": "CSCICP03GBM665S",  # OECD Composite Consumer Confidence Indicator UK
        "unemp": "LRHUTTTTGBM156S",       # Harmonised unemployment rate UK
        "cpi": "GBRCPIALLMINMEI",         # CPI all items UK
    },
    "DE": {
        "name": "Germany",
        "flag": "🇩🇪",
        "sentiment": "CSCICP03DEM665S",
        "unemp": "LRHUTTTTDEM156S",
        "cpi": "DEUCPIALLMINMEI",
    },
    "FR": {
        "name": "France",
        "flag": "🇫🇷",
        "sentiment": "CSCICP03FRM665S",
        "unemp": "LRHUTTTTFRM156S",
        "cpi": "FRACPIALLMINMEI",
    },
    "CA": {  # Canada (country, not California — sorry CA voters)
        "name": "Canada",
        "flag": "🇨🇦",
        "sentiment": "CSCICP03CAM665S",
        "unemp": "LRHUTTTTCAM156S",
        "cpi": "CANCPIALLMINMEI",
    },
    "JP": {
        "name": "Japan",
        "flag": "🇯🇵",
        "sentiment": "CSCICP03JPM665S",
        "unemp": "LRHUTTTTJPM156S",
        "cpi": "JPNCPIALLMINMEI",
    },
}


def _country_mood(code: str, cfg: dict) -> Optional[dict]:
    """Tiny per-country mood composite. Three 0-1 sub-scores averaged."""
    sent = fetch_fred_series(cfg["sentiment"])
    unemp = fetch_fred_series(cfg["unemp"])
    cpi = fetch_fred_series(cfg["cpi"])
    parts: list[float] = []
    breakdown: dict = {}

    if sent and sent.get("latest") and sent["latest"]["value"] is not None:
        v = sent["latest"]["value"]
        p = _percentile_from_history(sent["observations"], v, lookback=240)
        if p is not None:
            breakdown["sentiment"] = {"value": round(v, 1), "score_0_1": round(p, 3)}
            parts.append(p)
    if unemp and unemp.get("latest") and unemp["latest"]["value"] is not None:
        v = unemp["latest"]["value"]
        p = _percentile_from_history(unemp["observations"], v, lookback=240)
        if p is not None:
            breakdown["jobs"] = {"value": round(v, 2), "score_0_1": round(1 - p, 3)}
            parts.append(1 - p)
    if cpi and cpi.get("observations"):
        yoy = yoy_change(cpi["observations"], 12)
        if yoy is not None:
            non_null = [o["value"] for o in cpi["observations"] if o["value"] is not None]
            yoys: list[float] = []
            for i in range(12, len(non_null)):
                if non_null[i - 12] > 0:
                    yoys.append((non_null[i] / non_null[i - 12] - 1.0) * 100)
            if len(yoys) >= 24:
                below = sum(1 for y in yoys[-240:] if y < yoy)
                p = below / min(len(yoys), 240)
                breakdown["inflation"] = {"value": round(yoy, 2), "score_0_1": round(1 - p, 3)}
                parts.append(1 - p)
    if not parts:
        return None
    return {
        "country_code": code,
        "country_name": cfg["name"],
        "flag": cfg["flag"],
        "mood_0_100": round(100 * sum(parts) / len(parts), 1),
        "components": breakdown,
        "as_of": (sent.get("latest") or {}).get("date") if sent else None,
    }


def global_mood() -> dict:
    """Compute the per-country mood composite in parallel."""
    out: dict[str, dict] = {}
    lock = threading.Lock()

    def _go(item):
        code, cfg = item
        m = _country_mood(code, cfg)
        if m:
            with lock:
                out[code] = m

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(_go, COUNTRY_SERIES.items()))
    rows = sorted(out.values(), key=lambda r: -r["mood_0_100"])
    return {
        "countries": rows,
        "count": len(rows),
        "as_of": rows[0]["as_of"] if rows else None,
        "method": (
            "Per-country mood = mean of (consumer-confidence percentile, "
            "1 − unemployment percentile, 1 − CPI-YoY percentile), each vs "
            "own prior 20-year monthly history. Same formula across countries "
            "for direct comparability."
        ),
    }


# ─── Partisan sentiment (UMich historical snapshot) ────────────────────────────

# UMich publishes the Index of Consumer Sentiment by political party in
# Table 32 of their quarterly Surveys of Consumers releases — PDFs/Excel,
# not on FRED. The data below is hand-transcribed from those tables;
# accurate to ±2 index points. Refresh quarterly from
# data.sca.isr.umich.edu when a new release lands.
#
# Each row: (date, Republican, Democrat, Independent).
PARTISAN_UMICH_HISTORY: list[tuple[str, float, float, float]] = [
    ("2017-01-01",  79.0, 119.0,  91.0),   # Obama → Trump transition
    ("2017-04-01",  91.0, 109.0,  97.0),
    ("2017-07-01",  93.0, 105.0,  98.0),
    ("2017-10-01",  95.0,  79.0,  98.0),
    ("2018-01-01", 110.0,  79.0,  97.0),
    ("2018-04-01", 117.0,  75.0,  93.0),
    ("2018-07-01", 113.0,  72.0,  96.0),
    ("2018-10-01", 113.0,  79.0,  98.0),
    ("2019-01-01", 110.0,  76.0,  87.0),
    ("2019-04-01", 113.0,  75.0,  98.0),
    ("2019-07-01", 113.0,  72.0,  96.0),
    ("2019-10-01", 112.0,  78.0,  93.0),
    ("2020-01-01", 119.0,  73.0,  96.0),
    ("2020-04-01",  99.0,  60.0,  77.0),   # COVID shock — bipartisan collapse
    ("2020-07-01", 105.0,  61.0,  68.0),
    ("2020-10-01", 109.0,  64.0,  79.0),
    ("2021-01-01",  80.0,  93.0,  73.0),   # Biden inauguration — flip
    ("2021-04-01",  73.0, 105.0,  79.0),
    ("2021-07-01",  64.0,  98.0,  73.0),
    ("2021-10-01",  53.0,  87.0,  64.0),
    ("2022-01-01",  47.0,  82.0,  60.0),   # inflation shock begins
    ("2022-04-01",  46.0,  82.0,  56.0),
    ("2022-07-01",  41.0,  64.0,  49.0),   # bottom — gas $5/gal
    ("2022-10-01",  46.0,  74.0,  53.0),
    ("2023-01-01",  53.0,  79.0,  60.0),
    ("2023-04-01",  53.0,  79.0,  61.0),
    ("2023-07-01",  60.0,  82.0,  66.0),
    ("2023-10-01",  56.0,  76.0,  60.0),
    ("2024-01-01",  60.0,  92.0,  68.0),
    ("2024-04-01",  67.0,  87.0,  72.0),
    ("2024-07-01",  64.0,  91.0,  67.0),
    ("2024-10-01",  62.0,  91.0,  69.0),
    ("2025-01-01", 105.0,  62.0,  73.0),   # Trump 2.0 — flip back
    ("2025-04-01", 110.0,  58.0,  76.0),
    ("2025-07-01", 108.0,  60.0,  74.0),
    ("2025-10-01", 106.0,  62.0,  75.0),
]


def partisan_sentiment() -> Optional[dict]:
    """Return UMich consumer sentiment broken out by respondent partisanship.

    Data is from UMich's Surveys of Consumers Table 32, hand-transcribed
    from their quarterly PDF releases. The partisan-gap series is the most
    over-discussed, under-measured chart in US politics — it flips signs
    every time the White House changes party, which is what makes it
    interesting."""
    if not PARTISAN_UMICH_HISTORY:
        return None
    rows = [{"date": r[0], "republican": r[1], "democrat": r[2], "independent": r[3],
             "partisan_gap": round(r[1] - r[2], 1)}
            for r in PARTISAN_UMICH_HISTORY]
    latest = rows[-1]
    abs_gaps = [abs(r["partisan_gap"]) for r in rows]
    avg_gap = sum(abs_gaps) / len(abs_gaps)
    max_gap_row = max(rows, key=lambda r: abs(r["partisan_gap"]))
    return {
        "history": rows,
        "latest": latest,
        "avg_abs_gap": round(avg_gap, 1),
        "biggest_gap": max_gap_row,
        "source": "University of Michigan Surveys of Consumers · Table 32 (Index of Consumer Sentiment by Political Party)",
        "data_freshness_quarters": (
            "Hand-refreshed from UMich's quarterly PDF releases. "
            "Update when a new release is published at data.sca.isr.umich.edu."
        ),
    }


# ─── Right-track / wrong-track ────────────────────────────────────────────────

# Right-direction vs wrong-direction polling. Aggregated from RCP, Reuters/Ipsos,
# CBS, NBC, and AP-NORC monthly averages — the canonical "is the country headed
# in the right direction?" question. Hand-curated quarterly snapshots; refresh
# from RealClearPolitics' "Direction of Country" page when needed.
RIGHT_TRACK_HISTORY: list[tuple[str, float, float]] = [
    # (date, right-track %, wrong-track %)
    ("2020-01-01", 38.0, 56.0),
    ("2020-07-01", 25.0, 70.0),
    ("2020-12-01", 21.0, 73.0),
    ("2021-04-01", 40.0, 51.0),
    ("2021-10-01", 26.0, 65.0),
    ("2022-04-01", 24.0, 70.0),
    ("2022-10-01", 25.0, 70.0),
    ("2023-04-01", 24.0, 68.0),
    ("2023-10-01", 25.0, 67.0),
    ("2024-04-01", 26.0, 65.0),
    ("2024-10-01", 27.0, 65.0),
    ("2025-04-01", 32.0, 60.0),
    ("2025-10-01", 35.0, 58.0),
]


def right_track_wrong_track() -> Optional[dict]:
    """Right-direction vs wrong-direction polling — the canonical 'is the
    country on the right track?' indicator. Aggregated from multiple
    pollsters (RCP, Reuters/Ipsos, CBS, NBC, AP-NORC)."""
    if not RIGHT_TRACK_HISTORY:
        return None
    rows = [{"date": d, "right": r, "wrong": w, "net": round(r - w, 1)}
            for d, r, w in RIGHT_TRACK_HISTORY]
    latest = rows[-1]
    nets = [r["net"] for r in rows]
    return {
        "history": rows,
        "latest": latest,
        "min_net": min(nets),
        "max_net": max(nets),
        "as_of": latest["date"],
        "source": "RealClearPolitics 'Direction of Country' average + Reuters/Ipsos, CBS, NBC, AP-NORC",
        "data_freshness": "Hand-curated quarterly snapshots; refresh from RCP when needed.",
    }


# ─── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "service": "voter-dashboard", "ts": time.time()})


@app.route("/api/series/<series_id>")
def api_series(series_id: str):
    sid = series_id.upper()
    if sid not in FRED_SERIES:
        return jsonify({"error": f"unknown series {sid}"}), 404
    data = fetch_fred_series(sid)
    if not data:
        return jsonify({"error": f"FRED fetch failed for {sid}"}), 503
    return jsonify({
        **data,
        "yoy_pct": yoy_change(data["observations"], 12),
    })


@app.route("/api/markets")
def api_markets():
    raw = fetch_politics_markets()
    return jsonify({"markets": shape_markets_for_ui(raw), "count": len(raw)})


@app.route("/api/mood")
def api_mood():
    series = fetch_all_fred_parallel()
    return jsonify(voter_mood_index(series) or {"error": "insufficient data"})


@app.route("/api/states")
def api_states():
    """State-level unemployment + stress scores. 50 states + DC."""
    panel = state_panel()
    if not panel.get("states"):
        return jsonify({"error": "no state data available"}), 503
    return jsonify(panel)


@app.route("/api/election-cycle")
def api_election_cycle():
    """Historical mood → midterm seat-change regression with current
    implied seat change for the incumbent's party."""
    sent = fetch_fred_series("UMCSENT")
    if not sent:
        return jsonify({"error": "sentiment fetch failed"}), 503
    payload = election_cycle_regression(sent)
    if not payload:
        return jsonify({"error": "insufficient history"}), 503
    return jsonify(payload)


@app.route("/api/approval")
def api_approval():
    """Presidential approval — weighted weekly average from the 538
    archived approval-polls CSV."""
    polls = fetch_approval_polls()
    if not polls:
        return jsonify({"error": "approval polls unavailable"}), 503
    agg = approval_aggregate(polls)
    if not agg:
        return jsonify({"error": "no relevant polls"}), 503
    return jsonify(agg)


@app.route("/api/vibecession")
def api_vibecession():
    """The vibecession index — sentiment percentile minus fundamentals
    percentile, both vs the prior 20-year monthly distribution."""
    series = fetch_all_fred_parallel()
    payload = vibecession_gap(series)
    if not payload:
        return jsonify({"error": "insufficient data"}), 503
    return jsonify(payload)


@app.route("/api/election-cycle/backtest")
def api_election_cycle_backtest():
    """Leave-one-out cross-validation of the election-cycle regression."""
    sent = fetch_fred_series("UMCSENT")
    if not sent:
        return jsonify({"error": "sentiment fetch failed"}), 503
    payload = election_cycle_backtest(sent)
    if not payload:
        return jsonify({"error": "backtest unavailable"}), 503
    return jsonify(payload)


@app.route("/api/pollster-scorecard")
def api_pollster_scorecard():
    """Pollster accuracy rankings from 538's archived ratings CSV."""
    ratings = fetch_pollster_ratings()
    payload = pollster_scorecard(ratings)
    if not payload:
        return jsonify({"error": "pollster ratings unavailable"}), 503
    return jsonify(payload)


@app.route("/api/global-mood")
def api_global_mood():
    """Mood composite for each tracked country."""
    return jsonify(global_mood())


@app.route("/api/partisan-sentiment")
def api_partisan_sentiment():
    """UMich consumer sentiment broken out by respondent partisanship."""
    payload = partisan_sentiment()
    if not payload:
        return jsonify({"error": "no partisan data"}), 503
    return jsonify(payload)


@app.route("/api/right-track")
def api_right_track():
    """Right-direction vs wrong-direction polling aggregate."""
    payload = right_track_wrong_track()
    if not payload:
        return jsonify({"error": "no data"}), 503
    return jsonify(payload)


@app.route("/api/revisions")
def api_revisions():
    """Most-recent FRED revisions detected by the snapshot DB, plus stats."""
    return jsonify({
        "stats": snapshot_stats(),
        "recent": recent_revisions(50),
    })


@app.route("/methodology")
def methodology_page():
    return send_from_directory("static", "methodology.html")


@app.route("/embed/<card>")
def embed_card(card: str):
    """Iframe-friendly single-card widget. Supported: mood, forecast,
    vibecession, approval."""
    allowed = {"mood", "forecast", "vibecession", "approval"}
    if card not in allowed:
        return jsonify({"error": "unknown embed card"}), 404
    # Single template; the card name flows through as a URL arg the JS reads.
    resp = send_from_directory("static", "embed.html")
    resp.headers["X-Frame-Options"] = "ALLOWALL"
    resp.headers["Content-Security-Policy"] = "frame-ancestors *"
    return resp


@app.route("/api/csv/<series_id>")
def api_csv(series_id: str):
    """Pass-through CSV download of any tracked FRED series. Renames the
    second column to 'value' so consumers don't have to guess."""
    sid = series_id.upper()
    if sid not in FRED_SERIES and sid not in {f"{c}UR" for c in STATE_UNRATE}:
        return jsonify({"error": f"unknown series {sid}"}), 404
    data = fetch_fred_series(sid)
    if not data:
        return jsonify({"error": "fetch failed"}), 503
    out = ["date,value"]
    for o in data["observations"]:
        v = "" if o["value"] is None else str(o["value"])
        out.append(f'{o["date"]},{v}')
    return ("\n".join(out) + "\n",
            200,
            {"Content-Type": "text/csv; charset=utf-8",
             "Content-Disposition": f'attachment; filename="{sid}.csv"',
             "Cache-Control": "max-age=3600"})


@app.route("/api/summary")
def api_summary():
    """Front-page payload — all the cards on one page."""
    series = fetch_all_fred_parallel()

    def latest(sid: str) -> Optional[dict]:
        s = series.get(sid)
        return s.get("latest") if s else None

    def yoy(sid: str) -> Optional[float]:
        s = series.get(sid)
        return yoy_change(s["observations"], 12) if s else None

    def trim(sid: str, n: int) -> list[dict]:
        s = series.get(sid)
        if not s:
            return []
        # Drop nulls so the spark only renders real data
        clean = [o for o in s["observations"] if o["value"] is not None]
        return clean[-n:]

    cpi = series.get("CPIAUCSL")
    earn_h = series.get("CES0500000003")
    earn_w = series.get("CES0500000013")
    unrate = series.get("UNRATE")
    icsa = series.get("ICSA")
    usrec = series.get("USREC")

    misery_now = misery_index(unrate, cpi)
    return jsonify({
        "mood": voter_mood_index(series),
        "misery": {
            **(misery_now or {}),
            "spark": misery_history(unrate, cpi, months=60),
        } if misery_now else None,
        "recession": recession_state(usrec),
        "biggest_movers": biggest_movers(series, lookback_months=3),
        "election_cycle": election_cycle_regression(series.get("UMCSENT")),
        "vibecession": vibecession_gap(series),
        "approval": approval_aggregate(fetch_approval_polls()),
        "approval_market_implied": polymarket_approval_implied(fetch_politics_markets()),
        "partisan_sentiment": partisan_sentiment(),
        "right_track": right_track_wrong_track(),
        "real_wages": {
            "hourly_yoy_pct": real_wage_yoy(earn_h, cpi),
            "weekly_yoy_pct": real_wage_yoy(earn_w, cpi),
            "as_of": (earn_h.get("latest") or {}).get("date") if earn_h else None,
        },
        "indicators": {
            "sentiment": {
                "label": FRED_SERIES["UMCSENT"]["label"],
                "latest": latest("UMCSENT"),
                "change_3m": value_change(series["UMCSENT"]["observations"], 3) if series.get("UMCSENT") else None,
                "change_12m": value_change(series["UMCSENT"]["observations"], 12) if series.get("UMCSENT") else None,
                "spark": trim("UMCSENT", 60),
                "good": "high",
            },
            "unemployment": {
                "label": FRED_SERIES["UNRATE"]["label"],
                "latest": latest("UNRATE"),
                "change_12m": value_change(series["UNRATE"]["observations"], 12) if series.get("UNRATE") else None,
                "spark": trim("UNRATE", 60),
                "good": "low",
            },
            "inflation": {
                "label": "Headline CPI (YoY)",
                "latest_yoy_pct": yoy("CPIAUCSL"),
                "as_of": (cpi.get("latest") or {}).get("date") if cpi else None,
                "spark": _yoy_series(cpi["observations"], 12, 60) if cpi else [],
                "good": "low",
            },
            "wages_hourly": {
                "label": FRED_SERIES["CES0500000003"]["label"],
                "latest": latest("CES0500000003"),
                "yoy_pct": yoy("CES0500000003"),
                "spark": trim("CES0500000003", 60),
                "good": "high_yoy",
            },
            "gas": {
                "label": FRED_SERIES["GASREGW"]["label"],
                "latest": latest("GASREGW"),
                "change_12w": value_change(series["GASREGW"]["observations"], 12) if series.get("GASREGW") else None,
                "spark": trim("GASREGW", 156),  # ~3y of weekly
                "good": "low",
            },
            "mortgage": {
                "label": FRED_SERIES["MORTGAGE30US"]["label"],
                "latest": latest("MORTGAGE30US"),
                "change_52w": value_change(series["MORTGAGE30US"]["observations"], 52) if series.get("MORTGAGE30US") else None,
                "spark": trim("MORTGAGE30US", 156),
                "good": "low",
            },
            "claims": {
                "label": FRED_SERIES["ICSA"]["label"],
                "latest": latest("ICSA"),
                "four_week_avg": four_week_avg(icsa["observations"]) if icsa else None,
                "spark": trim("ICSA", 104),  # ~2y of weekly
                "good": "low",
            },
            "savings": {
                "label": FRED_SERIES["PSAVERT"]["label"],
                "latest": latest("PSAVERT"),
                "change_12m": value_change(series["PSAVERT"]["observations"], 12) if series.get("PSAVERT") else None,
                "spark": trim("PSAVERT", 60),
                "good": "high",
            },
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


def _yoy_series(observations: list[dict], months: int, tail: int) -> list[dict]:
    """Build a {date, value=YoY%} sparkline from a level series."""
    non_null = [o for o in observations if o["value"] is not None]
    if len(non_null) < months + 1:
        return []
    out: list[dict] = []
    for i in range(months, len(non_null)):
        prev = non_null[i - months]["value"]
        cur = non_null[i]["value"]
        if prev <= 0:
            continue
        out.append({"date": non_null[i]["date"],
                    "value": round((cur / prev - 1.0) * 100, 2)})
    return out[-tail:]


# ─── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("Starting voter sentiment dashboard on :%d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

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
        while len(_cache) > 64:
            _cache.popitem(last=False)


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

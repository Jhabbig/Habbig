#!/usr/bin/env python3
"""Polymarket Climate Change Dashboard — Flask backend (v3).

Long-horizon climate markets: warmest year on record, Arctic + Antarctic sea
ice extent, global mean temperature anomaly, atmospheric CO2 + CH4, sea
surface temperature, ENSO regime.

Data sources:
  - NASA GISTEMP v4 (monthly global land+ocean anomaly vs 1951-1980 baseline)
  - NOAA GML Mauna Loa atmospheric CO2 (monthly mean)
  - NOAA GML globally-averaged methane CH4 (monthly mean)        [v3]
  - NSIDC Sea Ice Index G02135 v4.0 (daily Arctic + Antarctic extent)
  - Climate Reanalyzer / NOAA OISST (daily global SST)
  - NOAA CPC ONI (Oceanic Nino Index — ENSO state)

Markets via Polymarket Gamma API. Tag slugs scanned: climate-change,
global-temperature, climate, global-warming, sea-level, extreme-weather.

Models (v2):
  - Year-end record-pace projection from YTD anomaly + historical drift,
    plus N(μ, σ) threshold probabilities (P(annual ≥ 1.5 / 1.6 / 1.7°C)).
  - 24-month linear regression on Mauna Loa CO2 with residual-std threshold
    probabilities (P(year-end ≥ 425 / 430 ppm), etc.).
  - 25-year linear-trend projections of Arctic AND Antarctic annual minimum
    sea ice extent, with normal-CDF probability bands for binned markets.
  - Backtest: each model is replayed 'as of June' for the last 5 completed
    years and reported as projected-vs-actual at /api/backtest.

Edges = (model_p − implied_p) in percentage points.
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
from datetime import datetime, timezone
from typing import Optional

import requests
from flask import Flask, jsonify, request, send_from_directory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("climate")

app = Flask(__name__, static_folder="static")

# Gzip compression
try:
    from flask_compress import Compress
    Compress(app)
except Exception:
    logger.warning("flask_compress not available; responses will not be gzipped")

PORT = int(os.environ.get("PORT", "7052"))

# ─── Cache ─────────────────────────────────────────────────────────────────────

_cache: "OrderedDict[str, dict]" = OrderedDict()
_cache_lock = threading.Lock()

# Per-key TTL overrides (seconds). Climate data updates monthly/daily so we cache
# generously — re-pulling NASA CSVs every minute would be wasteful and rude.
_TTL_DEFAULT = 60 * 60  # 1h
_TTL: dict[str, int] = {
    "gistemp": 60 * 60 * 12,        # GISTEMP updates monthly, refresh twice a day
    "co2": 60 * 60 * 12,            # NOAA Mauna Loa updates monthly
    "methane": 60 * 60 * 12,        # NOAA GML CH4 updates monthly
    "sea_ice": 60 * 60 * 6,         # NSIDC updates daily
    "sst": 60 * 60 * 3,             # OISST daily
    "oni": 60 * 60 * 12,            # CPC ONI updates monthly
    "polymarket": 60 * 5,            # Markets move — refresh every 5 min
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
        # Cap cache size
        while len(_cache) > 64:
            _cache.popitem(last=False)


# ─── HTTP helper ───────────────────────────────────────────────────────────────

_USER_AGENT = "polymarket-climate-dashboard/1.0 (+https://climate.narve.ai)"


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


# ─── Polymarket gamma fetcher ──────────────────────────────────────────────────

GAMMA_BASE = "https://gamma-api.polymarket.com"

CLIMATE_TAG_SLUGS = [
    "climate-change", "global-temperature", "climate",
    "global-warming", "sea-level", "extreme-weather",
]

# Reject sports / politics / crypto markets that share keywords with climate.
# Note: do NOT use "vs." here — climate markets sometimes phrase comparisons
# (e.g. "Arctic vs Antarctic") with that token.
REJECT_KEYWORDS = [
    "nfl", "nba", "nhl", "mlb", "mls", "rugby", "premier league", "ligue 1",
    "champion", "playoff", "election", "president", "senate", "governor",
    "ipo", "stock", "bitcoin", "crypto", "tesla", "spacex", "starship",
    "head-to-head", "champions league", "fight", "boxing",
]

CLIMATE_KEYWORDS = [
    "warmest", "hottest year", "global temperature", "global average",
    "climate", "co2", "carbon dioxide", "ppm", "sea ice", "arctic",
    "antarctic", "sea level", "ipcc", "1.5", "2 degrees", "paris agreement",
    "el nino", "la nina", "enso", "ocean temperature", "sst",
]


def _fetch_events_by_tag(tag_slug: str, seen_ids: set, all_markets: list, lock: threading.Lock) -> None:
    offset = 0
    for _ in range(8):  # cap pagination
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


def fetch_climate_markets() -> list[dict]:
    cached = cache_get("polymarket")
    if cached is not None:
        return cached
    all_markets: list[dict] = []
    seen_ids: set = set()
    lock = threading.Lock()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_fetch_events_by_tag, slug, seen_ids, all_markets, lock)
                   for slug in CLIMATE_TAG_SLUGS]
        for f in futures:
            try:
                f.result()
            except Exception as e:
                logger.warning("tag fetch error: %s", e)
    # Final keyword filter — Polymarket's tagging is noisy
    filtered = []
    for m in all_markets:
        title = (m.get("_event_title") or "") + " " + (m.get("question") or "")
        tl = title.lower()
        if any(k in tl for k in CLIMATE_KEYWORDS) or any("climate" in t.lower() for t in m.get("_event_tags", [])):
            filtered.append(m)
    logger.info("Fetched %d climate markets (from %d candidates)", len(filtered), len(all_markets))
    cache_set("polymarket", filtered)
    return filtered


# ─── NASA GISTEMP global temperature anomaly ───────────────────────────────────

GISTEMP_URL = "https://data.giss.nasa.gov/gistemp/tabledata_v4/GLB.Ts+dSST.csv"


def fetch_gistemp() -> Optional[dict]:
    """Return monthly global temperature anomaly series (vs 1951-1980 baseline, °C)."""
    cached = cache_get("gistemp")
    if cached is not None:
        return cached
    r = _http_get(GISTEMP_URL, timeout=30)
    if not r:
        return None
    text = r.text
    # GISTEMP CSV has 2 header rows then "Year,Jan,Feb,..." then yearly rows
    lines = text.splitlines()
    # Locate the header row
    header_idx = None
    for i, line in enumerate(lines):
        if line.startswith("Year,Jan"):
            header_idx = i
            break
    if header_idx is None:
        logger.warning("GISTEMP: header not found")
        return None
    series_monthly: list[dict] = []
    series_annual: list[dict] = []
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for line in lines[header_idx + 1:]:
        parts = line.split(",")
        if len(parts) < 14:
            continue
        try:
            year = int(parts[0])
        except ValueError:
            continue
        for mi, mname in enumerate(months, start=1):
            v = parts[mi].strip()
            if not v or v == "***":
                continue
            try:
                anomaly = float(v)  # GISTEMP CSV is in °C, not 0.01°C
            except ValueError:
                continue
            series_monthly.append({"year": year, "month": mi, "anomaly_c": round(anomaly, 3)})
        # Annual mean column "J-D" is at position 13
        try:
            ann = parts[13].strip()
            if ann and ann != "***":
                series_annual.append({"year": year, "anomaly_c": round(float(ann), 3)})
        except (ValueError, IndexError):
            pass
    out = {
        "source": "NASA GISTEMP v4 (GLB.Ts+dSST)",
        "baseline": "1951-1980",
        "units": "°C",
        "monthly": series_monthly,
        "annual": series_annual,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set("gistemp", out)
    return out


# ─── NOAA Mauna Loa CO2 ────────────────────────────────────────────────────────

CO2_MONTHLY_URL = "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_mm_mlo.csv"
CO2_DAILY_URL = "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_trend_mlo.csv"


def fetch_co2() -> Optional[dict]:
    cached = cache_get("co2")
    if cached is not None:
        return cached
    r = _http_get(CO2_MONTHLY_URL, timeout=30)
    if not r:
        return None
    series: list[dict] = []
    for line in r.text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            year = int(parts[0])
            month = int(parts[1])
            decimal_date = float(parts[2])
            ppm_avg = float(parts[3])
        except ValueError:
            continue
        if ppm_avg < 0:
            continue
        series.append({
            "year": year, "month": month,
            "decimal_date": round(decimal_date, 4),
            "ppm": round(ppm_avg, 2),
        })
    if not series:
        return None
    latest = series[-1]
    out = {
        "source": "NOAA GML Mauna Loa (co2_mm_mlo)",
        "units": "ppm",
        "monthly": series,
        "latest": latest,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set("co2", out)
    return out


# ─── NOAA GML methane (CH4) ────────────────────────────────────────────────────

# NOAA's monthly globally-averaged CH4 mole fraction in dry air, nanomol/mol (ppb).
CH4_MONTHLY_URL = "https://gml.noaa.gov/webdata/ccgg/trends/ch4/ch4_mm_gl.csv"


def fetch_methane() -> Optional[dict]:
    """Monthly globally-averaged methane (CH4) in ppb. Same shape as CO2."""
    cached = cache_get("methane")
    if cached is not None:
        return cached
    r = _http_get(CH4_MONTHLY_URL, timeout=30)
    if not r:
        return None
    series: list[dict] = []
    for line in r.text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            year = int(parts[0])
            month = int(parts[1])
            decimal_date = float(parts[2])
            ppb_avg = float(parts[3])
        except ValueError:
            continue
        if ppb_avg < 0:
            continue
        series.append({
            "year": year, "month": month,
            "decimal_date": round(decimal_date, 4),
            "ppb": round(ppb_avg, 2),
        })
    if not series:
        return None
    out = {
        "source": "NOAA GML globally-averaged CH4 (ch4_mm_gl)",
        "units": "ppb",
        "monthly": series,
        "latest": series[-1],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set("methane", out)
    return out


def methane_year_end_projection(ch4: dict) -> Optional[dict]:
    """24-month linear regression on globally-averaged CH4, evaluated at year-end."""
    if not ch4 or not ch4.get("monthly"):
        return None
    series = ch4["monthly"]
    cur_year = max(s["year"] for s in series)
    tail = series[-24:]
    if len(tail) < 6:
        return None
    xs = [s["decimal_date"] for s in tail]
    ys = [s["ppb"] for s in tail]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    if den == 0:
        return None
    slope = num / den  # ppb per year
    intercept = my - slope * mx
    projected = intercept + slope * (cur_year + 1.0)
    resid = [ys[i] - (intercept + slope * xs[i]) for i in range(n)]
    sigma = math.sqrt(sum(r * r for r in resid) / n) if n > 0 else 5.0
    sigma = max(sigma, 2.0)  # CH4 is noisier than CO2 — minimum 2 ppb σ
    return {
        "current_year": cur_year,
        "latest_ppb": series[-1]["ppb"],
        "ppb_per_year": round(slope, 2),
        "projected_year_end_ppb": round(projected, 2),
        "residual_std_ppb": round(sigma, 2),
    }


def methane_threshold_probs(ch4_proj: Optional[dict],
                             thresholds_ppb: tuple[float, ...] = (1930, 1940, 1950, 1960, 1970, 1980, 1990, 2000)) -> Optional[dict]:
    """P(year-end CH4 ≥ T) under N(projection, residual_std)."""
    if not ch4_proj:
        return None
    mu = ch4_proj.get("projected_year_end_ppb")
    sigma = ch4_proj.get("residual_std_ppb") or 5.0
    if mu is None:
        return None
    out = []
    for t in thresholds_ppb:
        p = 1.0 - _normal_cdf((t - mu) / sigma)
        out.append({"threshold_ppb": t, "p_at_or_above": round(p, 3)})
    return {"thresholds": out, "mu_ppb": mu, "sigma_ppb": sigma}


def methane_backtest(ch4: Optional[dict], n_years: int = 5) -> list[dict]:
    """As-of-June projection vs December actual for the last n_years."""
    if not ch4 or not ch4.get("monthly"):
        return []
    series = ch4["monthly"]
    cur_year = max(s["year"] for s in series)
    rows: list[dict] = []
    for target_year in range(cur_year - n_years, cur_year):
        mid = [s for s in series if s["year"] == target_year and s["month"] == 6]
        actual = [s for s in series if s["year"] == target_year and s["month"] == 12]
        if not mid or not actual:
            continue
        cutoff = mid[-1]["decimal_date"]
        tail = [s for s in series if s["decimal_date"] <= cutoff][-24:]
        if len(tail) < 12:
            continue
        xs = [s["decimal_date"] for s in tail]
        ys = [s["ppb"] for s in tail]
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
        den = sum((xs[i] - mx) ** 2 for i in range(n))
        if den == 0:
            continue
        slope = num / den
        intercept = my - slope * mx
        projected = intercept + slope * (target_year + 1.0)
        rows.append({
            "year": target_year,
            "as_of": "Jun",
            "projected_year_end_ppb": round(projected, 2),
            "actual_dec_ppb": round(actual[-1]["ppb"], 2),
            "error_ppb": round(projected - actual[-1]["ppb"], 2),
        })
    return rows


# ─── NSIDC Arctic sea ice extent ──────────────────────────────────────────────

# NSIDC publishes daily extent in km² at:
#   https://noaadata.apps.nsidc.org/NOAA/G02135/north/daily/data/N_seaice_extent_daily_v3.0.csv
SEA_ICE_URL_NORTH = "https://noaadata.apps.nsidc.org/NOAA/G02135/north/daily/data/N_seaice_extent_daily_v4.0.csv"
SEA_ICE_URL_SOUTH = "https://noaadata.apps.nsidc.org/NOAA/G02135/south/daily/data/S_seaice_extent_daily_v4.0.csv"


def _parse_seaice_csv(text: str) -> list[dict]:
    series: list[dict] = []
    rdr = csv.reader(io.StringIO(text))
    rows = list(rdr)
    if len(rows) < 3:
        return []
    # Skip the 2 header rows ("Year, Month, Day, Extent, Missing, Source Data")
    for row in rows[2:]:
        if len(row) < 4:
            continue
        try:
            year = int(row[0].strip())
            month = int(row[1].strip())
            day = int(row[2].strip())
            extent = float(row[3].strip())
        except ValueError:
            continue
        if extent <= 0:
            continue
        series.append({"year": year, "month": month, "day": day, "extent_mkm2": round(extent, 4)})
    return series


def fetch_sea_ice() -> Optional[dict]:
    cached = cache_get("sea_ice")
    if cached is not None:
        return cached
    out = {"source": "NSIDC Sea Ice Index G02135 v4.0",
           "units": "million km²",
           "fetched_at": datetime.now(timezone.utc).isoformat()}
    rn = _http_get(SEA_ICE_URL_NORTH, timeout=30)
    if rn:
        out["arctic"] = _parse_seaice_csv(rn.text)
    rs = _http_get(SEA_ICE_URL_SOUTH, timeout=30)
    if rs:
        out["antarctic"] = _parse_seaice_csv(rs.text)
    if not out.get("arctic") and not out.get("antarctic"):
        return None
    cache_set("sea_ice", out)
    return out


# ─── Global SST (Climate Reanalyzer / NOAA OISST 2.1) ──────────────────────────

# Climate Reanalyzer publishes ready-to-eat JSON of OISST world-mean daily SST.
SST_URL = "https://climatereanalyzer.org/clim/sst_daily/json/oisst2.1_world2_sst_day.json"


def fetch_sst() -> Optional[dict]:
    cached = cache_get("sst")
    if cached is not None:
        return cached
    r = _http_get(SST_URL, timeout=30)
    if not r:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    # Format: [{name: "1981", data: [v, v, ..., 366]}, ..., {name: "2026", data: [...]}, {name: "1982-2011 mean", ...}]
    out = {
        "source": "NOAA OISST v2.1 (world 60S-60N) via climatereanalyzer.org",
        "units": "°C",
        "series": data,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set("sst", out)
    return out


# ─── ENSO ONI ──────────────────────────────────────────────────────────────────

# CPC publishes an ASCII table of monthly ONI:
#   https://psl.noaa.gov/data/correlation/oni.data
ONI_URL = "https://psl.noaa.gov/data/correlation/oni.data"


def fetch_oni() -> Optional[dict]:
    cached = cache_get("oni")
    if cached is not None:
        return cached
    r = _http_get(ONI_URL, timeout=30)
    if not r:
        return None
    text = r.text
    series: list[dict] = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) != 13:  # year + 12 months
            continue
        try:
            year = int(parts[0])
            vals = [float(p) for p in parts[1:]]
        except ValueError:
            continue
        if not (1900 <= year <= 2100):
            continue
        for mi, v in enumerate(vals, start=1):
            if v <= -99:
                continue
            series.append({"year": year, "month": mi, "oni": round(v, 2)})
    if not series:
        return None
    latest = series[-1]
    state = "Neutral"
    if latest["oni"] >= 0.5:
        state = "El Niño"
    elif latest["oni"] <= -0.5:
        state = "La Niña"
    out = {
        "source": "NOAA CPC Oceanic Niño Index (ONI, 3-month running)",
        "monthly": series,
        "latest": latest,
        "state": state,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set("oni", out)
    return out


# ─── Models ────────────────────────────────────────────────────────────────────

def annual_record_pace_projection(gistemp: dict) -> Optional[dict]:
    """Project this year's annual mean using YTD progress and historical analog years."""
    if not gistemp or not gistemp.get("monthly"):
        return None
    monthly = gistemp["monthly"]
    annual = gistemp["annual"]
    if not annual:
        return None
    cur_year = max(m["year"] for m in monthly)
    cur_months = sorted([m for m in monthly if m["year"] == cur_year], key=lambda x: x["month"])
    if not cur_months:
        return None
    n = len(cur_months)
    ytd_mean = sum(m["anomaly_c"] for m in cur_months) / n

    # For each historical year, compute (YTD-through-month-n mean, annual mean)
    diffs = []
    for y in [a["year"] for a in annual if a["year"] != cur_year]:
        ms = sorted([m for m in monthly if m["year"] == y], key=lambda x: x["month"])
        if len(ms) < 12:
            continue
        ytd_y = sum(m["anomaly_c"] for m in ms[:n]) / n
        ann_y = sum(m["anomaly_c"] for m in ms) / 12
        diffs.append(ann_y - ytd_y)
    if not diffs:
        return None
    drift = sum(diffs) / len(diffs)
    drift_std = math.sqrt(sum((d - drift) ** 2 for d in diffs) / len(diffs)) if len(diffs) > 1 else 0.05
    projection = round(ytd_mean + drift, 3)

    # Record so far
    record = max(annual, key=lambda a: a["anomaly_c"])
    p_breaks_record = _normal_cdf((projection - record["anomaly_c"]) / max(drift_std, 0.01))

    return {
        "current_year": cur_year,
        "months_observed": n,
        "ytd_anomaly_c": round(ytd_mean, 3),
        "drift_to_year_end_c": round(drift, 3),
        "drift_std_c": round(drift_std, 3),
        "projected_annual_anomaly_c": projection,
        "current_record": record,
        "p_breaks_record": round(p_breaks_record, 3),
    }


def _normal_cdf(x: float) -> float:
    # Abramowitz & Stegun approximation
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def co2_year_end_projection(co2: dict) -> Optional[dict]:
    if not co2 or not co2.get("monthly"):
        return None
    series = co2["monthly"]
    cur_year = max(s["year"] for s in series)
    # Linear regression on last 24 months
    tail = series[-24:]
    if len(tail) < 6:
        return None
    xs = [s["decimal_date"] for s in tail]
    ys = [s["ppm"] for s in tail]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    if den == 0:
        return None
    slope = num / den  # ppm per year
    intercept = my - slope * mx
    year_end_decimal = cur_year + 1.0  # i.e. Jan 1 next year ≈ year-end
    projected = intercept + slope * year_end_decimal
    # Residual std around the trend line (in-sample). Inflate slightly to cover
    # forecast horizon out to year-end.
    resid = [ys[i] - (intercept + slope * xs[i]) for i in range(n)]
    sigma = math.sqrt(sum(r * r for r in resid) / n) if n > 0 else 0.5
    sigma = max(sigma, 0.3)  # floor — Mauna Loa is too clean to claim < 0.3 ppm
    return {
        "current_year": cur_year,
        "latest_ppm": series[-1]["ppm"],
        "ppm_per_year": round(slope, 3),
        "projected_year_end_ppm": round(projected, 2),
        "residual_std_ppm": round(sigma, 3),
    }


def sea_ice_record_check(sea_ice: dict) -> Optional[dict]:
    """Return today's Arctic extent vs the historical min/max for this day-of-year."""
    if not sea_ice or not sea_ice.get("arctic"):
        return None
    series = sea_ice["arctic"]
    if not series:
        return None
    latest = series[-1]
    doy_lat = (latest["month"], latest["day"])
    same_doy = [s for s in series if (s["month"], s["day"]) == doy_lat and s["year"] != latest["year"]]
    if not same_doy:
        return None
    extents = [s["extent_mkm2"] for s in same_doy]
    rank = 1 + sum(1 for e in extents if e < latest["extent_mkm2"])
    return {
        "date": f"{latest['year']:04d}-{latest['month']:02d}-{latest['day']:02d}",
        "extent_mkm2": latest["extent_mkm2"],
        "doy_min": round(min(extents), 4),
        "doy_max": round(max(extents), 4),
        "doy_mean": round(sum(extents) / len(extents), 4),
        "rank_lowest_in_record": rank,
        "history_years": len(extents) + 1,
    }


def arctic_min_projection(sea_ice: dict) -> Optional[dict]:
    """Project this summer's Arctic minimum extent from the historical trend.

    Each year's annual minimum (typically mid-September) is treated as the
    target. We linear-regress the past 25 years' minima against year, and
    quantify the residual std for probability bands.
    """
    if not sea_ice or not sea_ice.get("arctic"):
        return None
    series = sea_ice["arctic"]
    if not series:
        return None
    # Group by year, take min of each
    by_year: dict[int, float] = {}
    by_year_doy: dict[int, tuple[int, int]] = {}
    for s in series:
        y = s["year"]
        e = s["extent_mkm2"]
        if y not in by_year or e < by_year[y]:
            by_year[y] = e
            by_year_doy[y] = (s["month"], s["day"])
    if len(by_year) < 10:
        return None
    cur_year = max(by_year.keys())
    # If current year hasn't reached its min yet (typical until late Sept),
    # exclude it from the regression so we project FOR it.
    cur_doy = by_year_doy[cur_year]
    is_post_min = cur_doy[0] >= 9 and cur_doy[1] >= 15
    fit_years = sorted(y for y in by_year if y != cur_year or is_post_min)
    fit_years = [y for y in fit_years if y >= cur_year - 25]  # last 25y window
    xs = [float(y) for y in fit_years]
    ys = [by_year[y] for y in fit_years]
    n = len(xs)
    if n < 5:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    if den == 0:
        return None
    slope = num / den
    intercept = my - slope * mx
    # residuals
    resid = [ys[i] - (intercept + slope * xs[i]) for i in range(n)]
    sigma = math.sqrt(sum(r * r for r in resid) / n) if n > 0 else 0.3
    projected = intercept + slope * cur_year
    return {
        "current_year": cur_year,
        "fit_window_years": n,
        "trend_mkm2_per_year": round(slope, 4),
        "projected_min_mkm2": round(projected, 3),
        "residual_std_mkm2": round(max(sigma, 0.1), 3),
        "is_post_min": is_post_min,
    }


def antarctic_min_projection(sea_ice: dict) -> Optional[dict]:
    """Same trick as ``arctic_min_projection`` but for the southern-hemisphere
    annual minimum (typically late February / early March)."""
    if not sea_ice or not sea_ice.get("antarctic"):
        return None
    series = sea_ice["antarctic"]
    if not series:
        return None
    by_year: dict[int, float] = {}
    by_year_doy: dict[int, tuple[int, int]] = {}
    for s in series:
        y = s["year"]
        e = s["extent_mkm2"]
        if y not in by_year or e < by_year[y]:
            by_year[y] = e
            by_year_doy[y] = (s["month"], s["day"])
    if len(by_year) < 10:
        return None
    cur_year = max(by_year.keys())
    cur_doy = by_year_doy[cur_year]
    # Antarctic min is typically Feb 14 – early March. After ~Mar 15 we treat
    # the year's minimum as found.
    is_post_min = cur_doy[0] > 3 or (cur_doy[0] == 3 and cur_doy[1] >= 15)
    fit_years = sorted(y for y in by_year if y != cur_year or is_post_min)
    fit_years = [y for y in fit_years if y >= cur_year - 25]
    xs = [float(y) for y in fit_years]
    ys = [by_year[y] for y in fit_years]
    n = len(xs)
    if n < 5:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    if den == 0:
        return None
    slope = num / den
    intercept = my - slope * mx
    resid = [ys[i] - (intercept + slope * xs[i]) for i in range(n)]
    sigma = math.sqrt(sum(r * r for r in resid) / n) if n > 0 else 0.4
    projected = intercept + slope * cur_year
    return {
        "current_year": cur_year,
        "fit_window_years": n,
        "trend_mkm2_per_year": round(slope, 4),
        "projected_min_mkm2": round(projected, 3),
        "residual_std_mkm2": round(max(sigma, 0.15), 3),
        "is_post_min": is_post_min,
    }


def temperature_threshold_probs(gistemp_proj: Optional[dict],
                                  thresholds_c: tuple[float, ...] = (1.3, 1.4, 1.5, 1.6, 1.7, 1.8)) -> Optional[dict]:
    """For each anomaly threshold, P(annual mean ≥ T) under N(projection, drift_std)."""
    if not gistemp_proj:
        return None
    mu = gistemp_proj.get("projected_annual_anomaly_c")
    sigma = gistemp_proj.get("drift_std_c") or 0.05
    if mu is None:
        return None
    sigma = max(sigma, 0.03)
    out = []
    for t in thresholds_c:
        p = 1.0 - _normal_cdf((t - mu) / sigma)
        out.append({"threshold_c": t, "p_at_or_above": round(p, 3)})
    return {"thresholds": out, "mu_c": mu, "sigma_c": round(sigma, 3)}


def co2_threshold_probs(co2_proj: Optional[dict],
                        thresholds_ppm: tuple[float, ...] = (424, 425, 426, 427, 428, 429, 430)) -> Optional[dict]:
    """P(year-end ppm ≥ T) under normal centered on regression projection."""
    if not co2_proj:
        return None
    mu = co2_proj.get("projected_year_end_ppm")
    sigma = co2_proj.get("residual_std_ppm") or 0.5
    if mu is None:
        return None
    out = []
    for t in thresholds_ppm:
        p = 1.0 - _normal_cdf((t - mu) / sigma)
        out.append({"threshold_ppm": t, "p_at_or_above": round(p, 3)})
    return {"thresholds": out, "mu_ppm": mu, "sigma_ppm": sigma}


def gistemp_backtest(gistemp: Optional[dict], n_years: int = 5) -> list[dict]:
    """For the last ``n_years`` completed years, what would the year-end model
    have projected at each month-of-year? Reports the absolute error against
    the actual annual mean."""
    if not gistemp or not gistemp.get("monthly") or not gistemp.get("annual"):
        return []
    monthly = gistemp["monthly"]
    annual = {a["year"]: a["anomaly_c"] for a in gistemp["annual"]}
    cur_year = max(m["year"] for m in monthly)
    rows: list[dict] = []
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for target_year in range(cur_year - n_years, cur_year):
        if target_year not in annual:
            continue
        yr_months = sorted([m for m in monthly if m["year"] == target_year], key=lambda x: x["month"])
        if len(yr_months) < 12:
            continue
        # "As of June" projection: feed the model the first 6 months of target_year
        # and the entire history up to (but not including) target_year.
        as_of = 6
        ytd = sum(m["anomaly_c"] for m in yr_months[:as_of]) / as_of
        # Historical drift from June-YTD to year-end across years strictly before target_year
        diffs = []
        for y in [a for a in annual if a < target_year]:
            ms = sorted([m for m in monthly if m["year"] == y], key=lambda x: x["month"])
            if len(ms) < 12:
                continue
            ytd_y = sum(m["anomaly_c"] for m in ms[:as_of]) / as_of
            ann_y = sum(m["anomaly_c"] for m in ms) / 12
            diffs.append(ann_y - ytd_y)
        if not diffs:
            continue
        drift = sum(diffs) / len(diffs)
        projected = round(ytd + drift, 3)
        actual = annual[target_year]
        rows.append({
            "year": target_year,
            "as_of": months[as_of - 1],
            "projected_c": projected,
            "actual_c": round(actual, 3),
            "error_c": round(projected - actual, 3),
        })
    return rows


def co2_backtest(co2: Optional[dict], n_years: int = 5) -> list[dict]:
    """For each of the last ``n_years`` completed years, fit the same 24-month
    regression at mid-year and compare the year-end projection to the actual
    December reading."""
    if not co2 or not co2.get("monthly"):
        return []
    series = co2["monthly"]
    cur_year = max(s["year"] for s in series)
    rows: list[dict] = []
    for target_year in range(cur_year - n_years, cur_year):
        # "Mid-year" reference: latest available June reading of target_year
        mid = [s for s in series if s["year"] == target_year and s["month"] == 6]
        actual = [s for s in series if s["year"] == target_year and s["month"] == 12]
        if not mid or not actual:
            continue
        cutoff = mid[-1]["decimal_date"]
        tail = [s for s in series if s["decimal_date"] <= cutoff][-24:]
        if len(tail) < 12:
            continue
        xs = [s["decimal_date"] for s in tail]
        ys = [s["ppm"] for s in tail]
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
        den = sum((xs[i] - mx) ** 2 for i in range(n))
        if den == 0:
            continue
        slope = num / den
        intercept = my - slope * mx
        projected = intercept + slope * (target_year + 1.0)
        rows.append({
            "year": target_year,
            "as_of": "Jun",
            "projected_year_end_ppm": round(projected, 2),
            "actual_dec_ppm": round(actual[-1]["ppm"], 2),
            "error_ppm": round(projected - actual[-1]["ppm"], 2),
        })
    return rows


_RE_LT = re.compile(r"(?:less than|below|under)\s*([\d.]+)\s*m", re.I)
_RE_GE = re.compile(r"(?:at least|more than|above|over|exceed[a-z]*)\s*([\d.]+)\s*m", re.I)
_RE_BETWEEN = re.compile(r"between\s*([\d.]+)\s*m?\s*(?:&|and|to|-)\s*([\d.]+)\s*m", re.I)


def _ice_min_market_p(question: str, proj: dict) -> Optional[float]:
    """Compute probability for a 'minimum sea ice extent this summer/winter'
    market under a normal distribution centered on the projection."""
    mu = proj["projected_min_mkm2"]
    sigma = proj["residual_std_mkm2"]
    if sigma <= 0:
        return None
    q = question.lower()
    m = _RE_BETWEEN.search(q)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        return _normal_cdf((hi - mu) / sigma) - _normal_cdf((lo - mu) / sigma)
    m = _RE_LT.search(q)
    if m:
        thr = float(m.group(1))
        return _normal_cdf((thr - mu) / sigma)
    m = _RE_GE.search(q)
    if m:
        thr = float(m.group(1))
        return 1.0 - _normal_cdf((thr - mu) / sigma)
    return None


# Back-compat alias (kept so existing callers don't break)
_arctic_min_market_p = _ice_min_market_p


_RE_ANOMALY_GE = re.compile(r"(?:above|exceed[a-z]*|at least|over|greater than|more than)\s*\+?\s*([\d.]+)\s*°?\s*c", re.I)
_RE_ANOMALY_LT = re.compile(r"(?:below|under|less than)\s*\+?\s*([\d.]+)\s*°?\s*c", re.I)


def _temperature_anomaly_market_p(question: str, proj: dict) -> Optional[float]:
    """For markets like 'Will 2026 global anomaly be above 1.5°C?'"""
    mu = proj.get("projected_annual_anomaly_c")
    sigma = max(proj.get("drift_std_c") or 0.05, 0.03)
    if mu is None:
        return None
    q = question.lower()
    m = _RE_ANOMALY_GE.search(q)
    if m:
        thr = float(m.group(1))
        if 0.5 <= thr <= 3.0:  # only treat plausible anomaly thresholds
            return 1.0 - _normal_cdf((thr - mu) / sigma)
    m = _RE_ANOMALY_LT.search(q)
    if m:
        thr = float(m.group(1))
        if 0.5 <= thr <= 3.0:
            return _normal_cdf((thr - mu) / sigma)
    return None


def _co2_threshold_market_p(question: str, proj: dict) -> Optional[float]:
    """For markets like 'Will atmospheric CO2 exceed 430 ppm in 2026?'"""
    mu = proj.get("projected_year_end_ppm")
    sigma = max(proj.get("residual_std_ppm") or 0.5, 0.3)
    if mu is None:
        return None
    q = question.lower()
    m = re.search(r"(?:exceed[a-z]*|above|over|more than|at least|reach[a-z]*)\s*(\d{3}(?:\.\d+)?)\s*ppm", q)
    if m:
        thr = float(m.group(1))
        return 1.0 - _normal_cdf((thr - mu) / sigma)
    m = re.search(r"(?:below|under|less than)\s*(\d{3}(?:\.\d+)?)\s*ppm", q)
    if m:
        thr = float(m.group(1))
        return _normal_cdf((thr - mu) / sigma)
    return None


def _methane_threshold_market_p(question: str, proj: dict) -> Optional[float]:
    """For markets like 'Will atmospheric methane exceed 1950 ppb in 2026?'

    Methane thresholds in markets are typically expressed in ppb (3-4 digits)
    or occasionally ppm (1.9-2.0 with 'ppm' explicit). We accept both.
    """
    mu = proj.get("projected_year_end_ppb")
    sigma = max(proj.get("residual_std_ppb") or 5.0, 2.0)
    if mu is None:
        return None
    q = question.lower()
    # ppb pattern (4-digit threshold)
    m = re.search(r"(?:exceed[a-z]*|above|over|more than|at least|reach[a-z]*)\s*(\d{4}(?:\.\d+)?)\s*ppb", q)
    if m:
        thr = float(m.group(1))
        return 1.0 - _normal_cdf((thr - mu) / sigma)
    m = re.search(r"(?:below|under|less than)\s*(\d{4}(?:\.\d+)?)\s*ppb", q)
    if m:
        thr = float(m.group(1))
        return _normal_cdf((thr - mu) / sigma)
    # ppm pattern — convert to ppb (1.95 ppm == 1950 ppb)
    m = re.search(r"(?:exceed[a-z]*|above|over|more than|at least)\s*([12](?:\.\d{1,3})?)\s*ppm", q)
    if m and "methane" in q:
        thr = float(m.group(1)) * 1000.0
        return 1.0 - _normal_cdf((thr - mu) / sigma)
    return None


def edges_for_markets(markets: list[dict],
                       gistemp_proj: Optional[dict],
                       co2_proj: Optional[dict],
                       arctic_proj: Optional[dict] = None,
                       antarctic_proj: Optional[dict] = None,
                       methane_proj: Optional[dict] = None) -> list[dict]:
    """Attach a model probability + edge to markets where we can score them."""
    out = []
    for m in markets:
        # Combine event title + question so keyword matching works regardless of
        # which one carries the climate signal.
        title = ((m.get("_event_title") or "") + " " + (m.get("question") or "")).strip()
        tl = title.lower()
        try:
            implied = float(m.get("lastTradePrice") or m.get("bestBid") or 0)
        except (ValueError, TypeError):
            implied = None
        model_p: Optional[float] = None
        rationale = ""

        # 1) Warmest-year-on-record markets
        if gistemp_proj and ("warmest year" in tl or "hottest year" in tl
                              or "record" in tl and "temperature" in tl):
            model_p = gistemp_proj.get("p_breaks_record")
            rationale = (f"YTD {gistemp_proj['ytd_anomaly_c']}°C → projected "
                         f"{gistemp_proj['projected_annual_anomaly_c']}°C vs record "
                         f"{gistemp_proj['current_record']['anomaly_c']}°C "
                         f"({gistemp_proj['current_record']['year']})")

        # 2) Annual-anomaly threshold markets ("above 1.5°C", etc.)
        if model_p is None and gistemp_proj and ("anomaly" in tl or "global temperature" in tl
                                                   or "global average" in tl or "1.5" in tl
                                                   or "warming" in tl):
            p = _temperature_anomaly_market_p(title, gistemp_proj)
            if p is not None:
                model_p = max(0.0, min(1.0, p))
                rationale = (f"N(μ={gistemp_proj['projected_annual_anomaly_c']}°C, "
                             f"σ={gistemp_proj['drift_std_c']}°C) projection")

        # 3) Antarctic sea ice
        if model_p is None and antarctic_proj and ("antarctic" in tl and ("sea ice" in tl or "ice extent" in tl)):
            p = _ice_min_market_p(title, antarctic_proj)
            if p is not None:
                model_p = max(0.0, min(1.0, p))
                rationale = (f"Antarctic trend → {antarctic_proj['projected_min_mkm2']} Mkm² "
                             f"(σ={antarctic_proj['residual_std_mkm2']}, "
                             f"{antarctic_proj['trend_mkm2_per_year']:+.3f}/yr)")

        # 4) Arctic sea ice
        if model_p is None and arctic_proj and ("arctic sea ice" in tl
                                                  or "minimum arctic" in tl
                                                  or ("sea ice" in tl and "antarctic" not in tl)):
            p = _ice_min_market_p(title, arctic_proj)
            if p is not None:
                model_p = max(0.0, min(1.0, p))
                rationale = (f"Trend → {arctic_proj['projected_min_mkm2']} Mkm² "
                             f"(σ={arctic_proj['residual_std_mkm2']}, "
                             f"{arctic_proj['trend_mkm2_per_year']:+.3f}/yr)")

        # 5) CO₂ threshold markets
        if model_p is None and co2_proj and ("co2" in tl or "carbon dioxide" in tl or "ppm" in tl):
            p = _co2_threshold_market_p(title, co2_proj)
            if p is not None:
                model_p = max(0.0, min(1.0, p))
                rationale = (f"N(μ={co2_proj['projected_year_end_ppm']} ppm, "
                             f"σ={co2_proj['residual_std_ppm']} ppm), "
                             f"+{co2_proj['ppm_per_year']}/yr")

        # 6) Methane (CH4) threshold markets
        if model_p is None and methane_proj and ("methane" in tl or "ch4" in tl or "ppb" in tl):
            p = _methane_threshold_market_p(title, methane_proj)
            if p is not None:
                model_p = max(0.0, min(1.0, p))
                rationale = (f"N(μ={methane_proj['projected_year_end_ppb']} ppb, "
                             f"σ={methane_proj['residual_std_ppb']} ppb), "
                             f"+{methane_proj['ppb_per_year']}/yr")

        if implied is not None and model_p is not None:
            edge = round((model_p - implied) * 100, 1)
        else:
            edge = None

        out.append({
            **m,
            "_implied_p": implied,
            "_model_p": round(model_p, 3) if model_p is not None else None,
            "_edge_pp": edge,
            "_rationale": rationale,
        })
    return out


# ─── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "service": "climate-dashboard", "ts": time.time()})


@app.route("/api/markets")
def api_markets():
    markets = fetch_climate_markets()
    gist = fetch_gistemp()
    co2 = fetch_co2()
    sea = fetch_sea_ice()
    ch4 = fetch_methane()
    gp = annual_record_pace_projection(gist) if gist else None
    cp = co2_year_end_projection(co2) if co2 else None
    ap = arctic_min_projection(sea) if sea else None
    aap = antarctic_min_projection(sea) if sea else None
    mp = methane_year_end_projection(ch4) if ch4 else None
    enriched = edges_for_markets(markets, gp, cp, ap, aap, mp)
    return jsonify({
        "markets": enriched,
        "count": len(enriched),
        "gistemp_projection": gp,
        "co2_projection": cp,
        "methane_projection": mp,
        "arctic_min_projection": ap,
        "antarctic_min_projection": aap,
        "temperature_thresholds": temperature_threshold_probs(gp),
        "co2_thresholds": co2_threshold_probs(cp),
        "methane_thresholds": methane_threshold_probs(mp),
    })


@app.route("/api/temperature")
def api_temperature():
    g = fetch_gistemp()
    if not g:
        return jsonify({"error": "GISTEMP fetch failed"}), 503
    proj = annual_record_pace_projection(g)
    return jsonify({**g, "projection": proj})


@app.route("/api/co2")
def api_co2():
    c = fetch_co2()
    if not c:
        return jsonify({"error": "CO2 fetch failed"}), 503
    proj = co2_year_end_projection(c)
    return jsonify({**c, "projection": proj})


@app.route("/api/methane")
def api_methane():
    m = fetch_methane()
    if not m:
        return jsonify({"error": "Methane fetch failed"}), 503
    proj = methane_year_end_projection(m)
    thresholds = methane_threshold_probs(proj)
    return jsonify({**m, "projection": proj, "thresholds": thresholds})


@app.route("/api/sea-ice")
def api_sea_ice():
    s = fetch_sea_ice()
    if not s:
        return jsonify({"error": "Sea ice fetch failed"}), 503
    rec = sea_ice_record_check(s)
    # Trim arrays sent to the client — frontend only needs last ~3y for plots
    arctic = s.get("arctic") or []
    antarctic = s.get("antarctic") or []
    return jsonify({
        "source": s["source"],
        "units": s["units"],
        "fetched_at": s["fetched_at"],
        "arctic_recent": arctic[-1100:],
        "antarctic_recent": antarctic[-1100:],
        "record_check": rec,
    })


@app.route("/api/sst")
def api_sst():
    s = fetch_sst()
    if not s:
        return jsonify({"error": "SST fetch failed"}), 503
    return jsonify(s)


@app.route("/api/regime")
def api_regime():
    o = fetch_oni()
    if not o:
        return jsonify({"error": "ONI fetch failed"}), 503
    return jsonify(o)


@app.route("/api/summary")
def api_summary():
    """Single endpoint giving the front page everything it needs in one shot."""
    g = fetch_gistemp()
    c = fetch_co2()
    s = fetch_sea_ice()
    o = fetch_oni()
    ch4 = fetch_methane()
    gp = annual_record_pace_projection(g) if g else None
    cp = co2_year_end_projection(c) if c else None
    ap = arctic_min_projection(s) if s else None
    aap = antarctic_min_projection(s) if s else None
    mp = methane_year_end_projection(ch4) if ch4 else None
    return jsonify({
        "gistemp": {
            "latest_annual": g["annual"][-1] if g and g.get("annual") else None,
            "projection": gp,
            "thresholds": temperature_threshold_probs(gp),
        },
        "co2": {
            "latest": c["latest"] if c else None,
            "projection": cp,
            "thresholds": co2_threshold_probs(cp),
        },
        "methane": {
            "latest": ch4["latest"] if ch4 else None,
            "projection": mp,
            "thresholds": methane_threshold_probs(mp),
        },
        "sea_ice": {
            "record_check": sea_ice_record_check(s) if s else None,
            "arctic_projection": ap,
            "antarctic_projection": aap,
        },
        "regime": {
            "latest": o["latest"] if o else None,
            "state": o["state"] if o else None,
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/backtest")
def api_backtest():
    """Recent-history projection-vs-actual for our temperature & CO₂ models."""
    g = fetch_gistemp()
    c = fetch_co2()
    ch4 = fetch_methane()
    return jsonify({
        "gistemp": gistemp_backtest(g) if g else [],
        "co2": co2_backtest(c) if c else [],
        "methane": methane_backtest(ch4) if ch4 else [],
        "method": {
            "gistemp": "Replays the YTD-anomaly + historical-drift model 'as of June' for each year, scored vs the actual J-D mean.",
            "co2": "Refits the 24-month linear regression at June of each year, scored vs the actual December reading.",
            "methane": "Same June-cutoff 24-month regression as CO₂, scored vs the actual December reading.",
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


# ─── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("Starting climate dashboard on :%d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

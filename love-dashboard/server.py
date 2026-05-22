#!/usr/bin/env python3
"""State of Love dashboard — Flask backend (v1).

Computes the Love Index per the methodology in README.md:

  Composite = 0.35*Connection + 0.30*Partnership + 0.25*Stability + 0.10*Activity

Each subscore is a percentile rank within World Bank income tier (low /
lower-mid / upper-mid / high). Countries need >=2 of 3 Tier-A/B subscores to
be ranked; Activity alone is never enough.

v1 wires real fetchers for:
  - Eurostat demo_nind (crude marriage rate, crude divorce rate) - EU + EFTA
  - World Bank WDI SP.ADO.TFRT (adolescent fertility - Stability indicator)
  - World Bank country metadata (ISO codes + income classification)

v1.1 will add:
  - World Happiness Report appendix CSV  (Connection - Tier B)
  - Google Trends "love"/"date" basket   (Activity - Tier C)
  - UN DESA Demographic Yearbook XLSX    (Partnership/Stability worldwide)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from collections import OrderedDict
from typing import Any, Callable

import requests
from flask import Flask, jsonify, request, send_from_directory

import insights as insights_module
import og as og_module
import sensitivity as sensitivity_module
import snapshots as snapshots_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("love")

# ---------------------------------------------------------------------------
# Methodology constants (must match README.md)
# ---------------------------------------------------------------------------

WEIGHTS = {
    "connection": 0.35,
    "partnership": 0.30,
    "stability": 0.25,
    "activity": 0.10,
}
TIER_AB_SUBSCORES = ("connection", "partnership", "stability")  # Activity is Tier C
MIN_TIER_AB_PRESENT = 2
PARTNERSHIP_CAP_PCT = 80  # methodology: cap partnership rate at 80th percentile

INCOME_TIERS = ("L", "LM", "UM", "H")
WB_INCOME_MAP = {"LIC": "L", "LMC": "LM", "UMC": "UM", "HIC": "H"}

# Eurostat ISO2 idiosyncrasies (Greece is EL, UK is UK in Eurostat)
EUROSTAT_ISO2_FIX = {"EL": "GR", "UK": "GB"}

# ---------------------------------------------------------------------------
# Cache (in-memory OrderedDict with per-key TTL, matches climate-dashboard)
# ---------------------------------------------------------------------------

_cache: "OrderedDict[str, dict]" = OrderedDict()
_cache_lock = threading.Lock()
_TTL_DEFAULT = 60 * 60  # 1h
_TTL = {
    "country_meta":         7 * 24 * 3600,   # 7d  (income tiers update annually)
    "eurostat_marriage":    24 * 3600,
    "eurostat_divorce":     24 * 3600,
    "wb_adolescent":        24 * 3600,
    "whr_social_support":   7 * 24 * 3600,   # WHR is annual
    "un_marriage_divorce":  7 * 24 * 3600,   # UN DESA is annual
    "loneliness_csv":       7 * 24 * 3600,   # Meta-Gallup is annual
    "activity_csv":         24 * 3600,
    "wb_tfr":               24 * 3600,
    "wb_flfp":              24 * 3600,
    "wb_gdp_pc":            24 * 3600,
    "wb_life_exp":          24 * 3600,
    "un_wpp_smam_w":        7 * 24 * 3600,
    "ilga_rainbow":         7 * 24 * 3600,
    "summary":              60 * 60,
    "index":                60 * 60,
    "index_map":            60 * 60,
    "subscore_layers":      60 * 60,
    "sensitivity":          60 * 60,
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


# Per-key locks dedupe concurrent loaders: two requests that miss the cache
# for the same key don't both fire the (often-network) loader. The owning
# thread populates the cache; followers re-read it after the lock releases.
_key_locks: dict[str, threading.Lock] = {}
_key_locks_lock = threading.Lock()


def _get_key_lock(key: str) -> threading.Lock:
    with _key_locks_lock:
        lk = _key_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _key_locks[key] = lk
        return lk


def cached(key: str, loader: Callable[[], Any]) -> Any:
    hit = cache_get(key)
    if hit is not None:
        return hit
    with _get_key_lock(key):
        # double-check: another thread may have populated while we waited
        hit = cache_get(key)
        if hit is not None:
            return hit
        val = loader()
        cache_set(key, val)
        return val


# ---------------------------------------------------------------------------
# World Bank: country metadata + income tier mapping
# ---------------------------------------------------------------------------

WB_BASE = "https://api.worldbank.org/v2"


def fetch_country_meta() -> dict[str, dict]:
    """ISO3 -> {name, iso2, income_tier, region}. Aggregates excluded."""
    r = requests.get(
        f"{WB_BASE}/country",
        params={"format": "json", "per_page": 400},
        timeout=30,
    )
    r.raise_for_status()
    _meta, rows = r.json()
    out: dict[str, dict] = {}
    for c in rows or []:
        income_id = (c.get("incomeLevel") or {}).get("id")
        if income_id not in WB_INCOME_MAP:
            continue  # skips aggregates ("World", "Europe & Central Asia", etc.)
        iso3 = c.get("id")
        if not iso3 or len(iso3) != 3:
            continue
        out[iso3] = {
            "iso3": iso3,
            "iso2": c.get("iso2Code"),
            "name": c.get("name"),
            "income_tier": WB_INCOME_MAP[income_id],
            "region": (c.get("region") or {}).get("value", ""),
        }
    log.info("country_meta: %d countries", len(out))
    return out


def get_country_meta() -> dict[str, dict]:
    try:
        return cached("country_meta", fetch_country_meta)
    except Exception as exc:
        log.warning("country_meta fetch failed: %s", exc)
        return {}


def iso2_to_iso3() -> dict[str, str]:
    out: dict[str, str] = {}
    for iso3, c in get_country_meta().items():
        iso2 = c.get("iso2")
        if iso2:
            out[iso2] = iso3
    return out


# ---------------------------------------------------------------------------
# World Bank WDI fetcher
# ---------------------------------------------------------------------------

def fetch_wb_indicator(indicator: str) -> dict[str, float]:
    """ISO3 -> latest non-null value for a WB WDI indicator."""
    out: dict[str, float] = {}
    page = 1
    while True:
        r = requests.get(
            f"{WB_BASE}/country/all/indicator/{indicator}",
            params={"format": "json", "per_page": 20000, "page": page, "mrnev": 1},
            timeout=30,
        )
        r.raise_for_status()
        meta, data = r.json()
        if not data:
            break
        for row in data:
            iso = row.get("countryiso3code")
            v = row.get("value")
            if iso and len(iso) == 3 and v is not None and iso not in out:
                out[iso] = float(v)
        if page >= int(meta.get("pages", 1) or 1):
            break
        page += 1
    log.info("wb %s: %d countries", indicator, len(out))
    return out


def fetch_wb_indicator_history(indicator: str) -> dict[str, dict[int, float]]:
    """ISO3 -> {year: value} for the full historical series of a WB indicator."""
    out: dict[str, dict[int, float]] = {}
    page = 1
    while True:
        r = requests.get(
            f"{WB_BASE}/country/all/indicator/{indicator}",
            params={"format": "json", "per_page": 20000, "page": page},
            timeout=30,
        )
        r.raise_for_status()
        meta, data = r.json()
        if not data:
            break
        for row in data:
            iso = row.get("countryiso3code")
            v = row.get("value")
            try:
                year = int(row.get("date"))
            except (TypeError, ValueError):
                continue
            if iso and len(iso) == 3 and v is not None:
                out.setdefault(iso, {})[year] = float(v)
        if page >= int(meta.get("pages", 1) or 1):
            break
        page += 1
    log.info("wb %s history: %d countries", indicator, len(out))
    return out


# ---------------------------------------------------------------------------
# Eurostat fetcher (JSON-stat 2.0 dissemination API)
# ---------------------------------------------------------------------------

EUROSTAT_BASE = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"


def fetch_eurostat(dataset: str, filters: dict[str, str]) -> dict:
    params = {"format": "JSON", "lang": "EN", **filters}
    r = requests.get(f"{EUROSTAT_BASE}/{dataset}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def parse_eurostat_geo_time(js: dict) -> dict[str, dict[str, float]]:
    """Parse a JSON-stat response with geo + time as the only varying dims.

    Returns {geo_iso2: {time_period: value}} with non-null entries only.
    All other dims must already be filtered to a single value via the URL.
    """
    dims = js.get("id") or []
    sizes = js.get("size") or []
    if "geo" not in dims or "time" not in dims:
        return {}
    # JSON-stat: dimension[d].category.index can be {key: pos} or {pos: key}.
    def index_to_key(d):
        idx = js["dimension"][d]["category"]["index"]
        if not idx:
            return {}
        sample = next(iter(idx.values()))
        if isinstance(sample, int):
            return {pos: key for key, pos in idx.items()}
        return {int(k): v for k, v in idx.items()}

    pos_key = {d: index_to_key(d) for d in dims}
    # Flat index decode using row-major layout
    geo_pos = dims.index("geo")
    time_pos = dims.index("time")
    strides = []
    s = 1
    for size in reversed(sizes):
        strides.insert(0, s)
        s *= size

    out: dict[str, dict[str, float]] = {}
    for flat_str, val in (js.get("value") or {}).items():
        if val is None:
            continue
        flat = int(flat_str)
        coords = []
        for stride, size in zip(strides, sizes):
            coords.append((flat // stride) % size)
        geo = pos_key["geo"].get(coords[geo_pos])
        period = pos_key["time"].get(coords[time_pos])
        if geo is None or period is None:
            continue
        out.setdefault(geo, {})[period] = float(val)
    return out


def latest_per_geo(matrix: dict[str, dict[str, float]]) -> dict[str, float]:
    return {g: vs[max(vs)] for g, vs in matrix.items() if vs}


def eurostat_history_by_iso3(matrix: dict[str, dict[str, float]]) -> dict[str, dict[int, float]]:
    """{geo_iso2: {period: value}} -> {iso3: {year: value}} for full history."""
    lookup = iso2_to_iso3()
    out: dict[str, dict[int, float]] = {}
    for code, by_period in matrix.items():
        iso2 = EUROSTAT_ISO2_FIX.get(code, code)
        iso3 = lookup.get(iso2)
        if not iso3:
            continue
        for period, value in by_period.items():
            try:
                year = int(str(period)[:4])
            except (TypeError, ValueError):
                continue
            out.setdefault(iso3, {})[year] = value
    return out


def fetch_eurostat_crude_rate_history(dataset: str, indic_de: str) -> dict[str, dict[int, float]]:
    """Full time series of a crude-rate indicator by ISO3 / year."""
    for params in (
        {"indic_de": indic_de},
        {"indic_de": indic_de, "sex": "T", "age": "TOTAL"},
        {},
    ):
        try:
            js = fetch_eurostat(dataset, params)
            matrix = parse_eurostat_geo_time(js)
            hist = eurostat_history_by_iso3(matrix)
            if hist:
                return hist
        except Exception:
            continue
    return {}


def map_eurostat_to_iso3(geo_values: dict[str, float]) -> dict[str, float]:
    lookup = iso2_to_iso3()
    out: dict[str, float] = {}
    for code, v in geo_values.items():
        iso2 = EUROSTAT_ISO2_FIX.get(code, code)
        iso3 = lookup.get(iso2)
        if iso3:
            out[iso3] = v
    return out


def fetch_eurostat_crude_rate(dataset: str, indic_de: str) -> dict[str, float]:
    """Try a few common Eurostat code shapes to be resilient to API drift."""
    last_err: Exception | None = None
    for params in (
        {"indic_de": indic_de},
        {"indic_de": indic_de, "sex": "T", "age": "TOTAL"},
        {},  # last resort: full dataset, may fail with 413
    ):
        try:
            js = fetch_eurostat(dataset, params)
            matrix = parse_eurostat_geo_time(js)
            latest = latest_per_geo(matrix)
            if latest:
                return map_eurostat_to_iso3(latest)
        except Exception as exc:
            last_err = exc
            continue
    if last_err:
        log.warning("eurostat %s/%s: %s", dataset, indic_de, last_err)
    return {}


def eurostat_marriage_rate() -> dict[str, float]:
    return fetch_eurostat_crude_rate("demo_nind", "GMARRA")


def eurostat_divorce_rate() -> dict[str, float]:
    return fetch_eurostat_crude_rate("demo_ndivind", "GDIVRT")


# ---------------------------------------------------------------------------
# World Happiness Report (Connection subscore — Tier B)
#
# WHR publishes its Figure 2.1 data table annually, but as XLS at unstable
# URLs that change with each year's report. Rather than ship an XLS parser
# plus speculative URLs, we read a CSV the operator drops at
# `data/whr.csv`. Expected columns (case-insensitive): `country` and
# `social support` (0-1 scale; auto-rescaled to 0-100 if needed).
#
# If the file is missing, Connection stays empty and the dashboard reports
# it as a coverage gap — honest behaviour, no fake numbers.
# ---------------------------------------------------------------------------

import csv
import io
import re
from pathlib import Path

WHR_LOCAL = Path(__file__).parent / "data" / "whr.csv"
SNAPSHOTS_DB = Path(__file__).parent / "data" / "snapshots.db"

# WHR uses informal country names; map the ones that don't match the WB name
# directly. Lookup is case-insensitive and ignores punctuation.
WHR_NAME_OVERRIDES = {
    "united states":            "USA",
    "united states of america": "USA",
    "russia":                   "RUS",
    "south korea":              "KOR",
    "north korea":              "PRK",
    "czech republic":           "CZE",
    "czechia":                  "CZE",
    "ivory coast":              "CIV",
    "cote d ivoire":            "CIV",
    "congo brazzaville":        "COG",
    "congo kinshasa":           "COD",
    "democratic republic of congo": "COD",
    "hong kong":                "HKG",
    "hong kong sar of china":   "HKG",
    "taiwan":                   "TWN",
    "taiwan province of china": "TWN",
    "palestinian territories":  "PSE",
    "state of palestine":       "PSE",
    "vietnam":                  "VNM",
    "iran":                     "IRN",
    "syria":                    "SYR",
    "venezuela":                "VEN",
    "tanzania":                 "TZA",
    "moldova":                  "MDA",
    "bolivia":                  "BOL",
    "laos":                     "LAO",
    "turkey":                   "TUR",
    "turkiye":                  "TUR",
    "swaziland":                "SWZ",
    "eswatini":                 "SWZ",
    "macedonia":                "MKD",
    "north macedonia":          "MKD",
}


def _normalize_country_name(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z ]+", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _whr_name_to_iso3(meta: dict[str, dict]) -> dict[str, str]:
    out: dict[str, str] = dict(WHR_NAME_OVERRIDES)
    for iso3, c in meta.items():
        name = c.get("name") or ""
        out[_normalize_country_name(name)] = iso3
    return out


def _parse_whr_csv(text: str) -> dict[str, float]:
    """Parse a WHR-style CSV with country + social-support columns.

    Returns {iso3: social_support_0_to_100}. Unmatched countries are dropped.
    """
    meta = get_country_meta()
    name_to_iso3 = _whr_name_to_iso3(meta)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return {}
    name_col = next((c for c in reader.fieldnames if "country" in c.lower()), None)
    ss_col = next((c for c in reader.fieldnames
                   if "social support" in c.lower() or c.lower() == "social_support"), None)
    if not name_col or not ss_col:
        return {}
    out: dict[str, float] = {}
    for row in reader:
        nm = _normalize_country_name(row.get(name_col) or "")
        if not nm:
            continue
        iso3 = name_to_iso3.get(nm)
        if not iso3:
            continue
        try:
            v = float(row.get(ss_col) or "")
        except ValueError:
            continue
        # WHR reports 0-1 share; rescale to 0-100 for parity with our subscores.
        out[iso3] = v * 100.0 if 0.0 <= v <= 1.0 else v
    return out


def fetch_whr_social_support() -> dict[str, float]:
    """Read WHR social-support data from data/whr.csv if present."""
    if not WHR_LOCAL.exists():
        log.info("WHR Connection fetcher: no data file at %s — Connection subscore will be empty", WHR_LOCAL)
        return {}
    try:
        return _parse_whr_csv(WHR_LOCAL.read_text())
    except Exception as exc:
        log.warning("WHR local file %s parse failed: %s", WHR_LOCAL, exc)
        return {}


# ---------------------------------------------------------------------------
# UN DESA Demographic Yearbook (global Partnership + Stability coverage)
#
# Eurostat covers only EU + EFTA. UN DESA publishes the same crude marriage
# and divorce rates worldwide but as XLSX at unstable URLs. Rather than ship
# a fragile scraper, we read a CSV the operator drops at `data/un_marriage.csv`.
# Eurostat takes precedence for any country present in both feeds (it's
# usually fresher and the rate definitions match per-1000-population).
#
# Expected columns (case-insensitive): `country`, `marriage_rate`,
# `divorce_rate`. Either rate may be blank.
# ---------------------------------------------------------------------------

UN_MARRIAGE_LOCAL = Path(__file__).parent / "data" / "un_marriage.csv"


def _parse_un_marriage_csv(text: str) -> tuple[dict[str, float], dict[str, float]]:
    """Returns (marriage_rate_by_iso3, divorce_rate_by_iso3) per 1000 pop."""
    meta = get_country_meta()
    name_to_iso3 = _whr_name_to_iso3(meta)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return {}, {}
    name_col = next((c for c in reader.fieldnames if "country" in c.lower()), None)
    m_col = next((c for c in reader.fieldnames if "marriage" in c.lower()), None)
    d_col = next((c for c in reader.fieldnames if "divorce" in c.lower()), None)
    if not name_col or (not m_col and not d_col):
        return {}, {}
    marriage: dict[str, float] = {}
    divorce: dict[str, float] = {}
    for row in reader:
        nm = _normalize_country_name(row.get(name_col) or "")
        if not nm:
            continue
        iso3 = name_to_iso3.get(nm)
        if not iso3:
            continue
        if m_col and row.get(m_col):
            try: marriage[iso3] = float(row[m_col])
            except ValueError: pass
        if d_col and row.get(d_col):
            try: divorce[iso3] = float(row[d_col])
            except ValueError: pass
    return marriage, divorce


def fetch_un_marriage_divorce() -> tuple[dict[str, float], dict[str, float]]:
    if not UN_MARRIAGE_LOCAL.exists():
        log.info("UN DESA fetcher: no data file at %s — Partnership/Stability rely on Eurostat only", UN_MARRIAGE_LOCAL)
        return {}, {}
    try:
        return _parse_un_marriage_csv(UN_MARRIAGE_LOCAL.read_text())
    except Exception as exc:
        log.warning("UN DESA local file %s parse failed: %s", UN_MARRIAGE_LOCAL, exc)
        return {}, {}


# ---------------------------------------------------------------------------
# Activity subscore (Tier C — proxy data, see methodology)
#
# Operator-supplied CSV. Suggested inputs combined and normalized 0-100
# offline (Google Trends "love"/"date" basket, dating-app penetration from
# investor decks). We deliberately keep the ETL outside the server: the
# methodology's "Tier C — indicative only" badge stays honest, and we don't
# add fragile scraper deps.
#
# Expected columns (case-insensitive): `country`, `activity` (any scale;
# gets percentile-ranked within income tier just like the other subscores).
# ---------------------------------------------------------------------------

ACTIVITY_LOCAL = Path(__file__).parent / "data" / "activity.csv"

# ---------------------------------------------------------------------------
# Meta-Gallup loneliness (Connection subscore — Tier B)
#
# Companion to WHR social-support: where WHR captures the "have someone to
# count on" side of Connection, this captures the inverse. Combine them so
# the subscore isn't single-sourced. Operator-supplied CSV with columns
# `country`, `loneliness` (0-100, *lower* = better; we invert before merging).
# ---------------------------------------------------------------------------

LONELINESS_LOCAL = Path(__file__).parent / "data" / "loneliness.csv"


def _parse_loneliness_csv(text: str) -> dict[str, float]:
    """Returns {iso3: connection_score_0_to_100} — already inverted from
    loneliness so it stacks the same direction as WHR social-support."""
    meta = get_country_meta()
    name_to_iso3 = _whr_name_to_iso3(meta)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return {}
    name_col = next((c for c in reader.fieldnames if "country" in c.lower()), None)
    val_col = next((c for c in reader.fieldnames if "lonel" in c.lower()), None)
    if not name_col or not val_col:
        return {}
    out: dict[str, float] = {}
    for row in reader:
        nm = _normalize_country_name(row.get(name_col) or "")
        if not nm:
            continue
        iso3 = name_to_iso3.get(nm)
        if not iso3:
            continue
        try:
            lonely = float(row[val_col])
        except (ValueError, TypeError):
            continue
        # Auto-rescale 0-1 fractions to 0-100; then invert so higher == better.
        if 0.0 <= lonely <= 1.0:
            lonely *= 100.0
        out[iso3] = 100.0 - max(0.0, min(100.0, lonely))
    return out


def fetch_loneliness_data() -> dict[str, float]:
    if not LONELINESS_LOCAL.exists():
        log.info("Loneliness fetcher: no data file at %s — Connection uses WHR only", LONELINESS_LOCAL)
        return {}
    try:
        return _parse_loneliness_csv(LONELINESS_LOCAL.read_text())
    except Exception as exc:
        log.warning("Loneliness local file %s parse failed: %s", LONELINESS_LOCAL, exc)
        return {}


# ---------------------------------------------------------------------------
# UN World Population Prospects (Tier A, JSON API)
#
# Singulate mean age at marriage — the demographically clean version of the
# age-at-first-union signal we currently proxy with adolescent fertility.
# Used as a context indicator (not yet in the composite); the LLM narrative
# layer picks it up and the country drill-down surfaces it.
# ---------------------------------------------------------------------------

UN_WPP_BASE = "https://population.un.org/dataportalapi/api/v1"
# SMAM = Singulate Mean Age at Marriage. Indicator ID 21 is women; 22 is men.
UN_WPP_INDICATOR_SMAM_WOMEN = 21


def fetch_un_wpp_indicator_latest(indicator_id: int, start_year: int = 2015) -> dict[str, float]:
    """Latest value per ISO3 for a UN WPP indicator. Walks the paginated
    `/data/indicators/{id}/locations/all/start/{y}/end/{y2}` endpoint."""
    out: dict[str, dict] = {}
    page = 1
    end_year = datetime.utcnow().year
    while True:
        try:
            r = requests.get(
                f"{UN_WPP_BASE}/data/indicators/{indicator_id}/locations/all/start/{start_year}/end/{end_year}",
                params={"pageNumber": page, "pageSize": 500},
                timeout=30,
            )
            r.raise_for_status()
            payload = r.json()
        except Exception as exc:
            log.warning("UN WPP indicator %s fetch failed (page %d): %s", indicator_id, page, exc)
            return {k: v["value"] for k, v in out.items()}
        rows = payload.get("data") or payload  # API has shifted; tolerate both shapes
        if not isinstance(rows, list):
            rows = payload.get("data", [])
        if not rows:
            break
        for row in rows:
            iso3 = row.get("iso3") or row.get("countryIso3Code")
            v = row.get("value")
            year = row.get("timeLabel") or row.get("year") or row.get("time")
            if not iso3 or v is None:
                continue
            try:
                year_i = int(str(year)[:4])
            except (TypeError, ValueError):
                continue
            cur = out.get(iso3)
            if cur is None or year_i > cur["year"]:
                out[iso3] = {"value": float(v), "year": year_i}
        total_pages = payload.get("pages") or payload.get("totalPages") or 1
        if page >= int(total_pages):
            break
        page += 1
    log.info("un_wpp %s: %d countries (latest year per country)", indicator_id, len(out))
    return {k: v["value"] for k, v in out.items()}


def fetch_un_wpp_age_at_marriage_women() -> dict[str, float]:
    return fetch_un_wpp_indicator_latest(UN_WPP_INDICATOR_SMAM_WOMEN)


# ---------------------------------------------------------------------------
# ILGA-Europe / Equaldex Rainbow Index (Freedom dimension — context only)
#
# Annual scoring of LGBTI rights per country, 0–100. Operator drops a CSV
# at data/rainbow.csv (columns: country, rainbow_score). Reported as a
# context indicator and used as fuel for rule_event_overlay when paired
# with year-of-change.
# ---------------------------------------------------------------------------

RAINBOW_LOCAL = Path(__file__).parent / "data" / "rainbow.csv"


def _parse_rainbow_csv(text: str) -> dict[str, float]:
    meta = get_country_meta()
    name_to_iso3 = _whr_name_to_iso3(meta)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return {}
    name_col = next((c for c in reader.fieldnames if "country" in c.lower()), None)
    val_col = next((c for c in reader.fieldnames if "rainbow" in c.lower() or "score" in c.lower()), None)
    if not name_col or not val_col:
        return {}
    out: dict[str, float] = {}
    for row in reader:
        nm = _normalize_country_name(row.get(name_col) or "")
        iso3 = name_to_iso3.get(nm)
        if not iso3:
            continue
        try:
            v = float(row[val_col])
        except (ValueError, TypeError):
            continue
        # Some publications use 0-1 fractions; rescale.
        if 0.0 <= v <= 1.0:
            v *= 100.0
        out[iso3] = max(0.0, min(100.0, v))
    return out


def fetch_rainbow_index() -> dict[str, float]:
    if not RAINBOW_LOCAL.exists():
        log.info("Rainbow Index: no data file at %s — Freedom context skipped", RAINBOW_LOCAL)
        return {}
    try:
        return _parse_rainbow_csv(RAINBOW_LOCAL.read_text())
    except Exception as exc:
        log.warning("Rainbow Index local file %s parse failed: %s", RAINBOW_LOCAL, exc)
        return {}


def _parse_activity_csv(text: str) -> dict[str, float]:
    meta = get_country_meta()
    name_to_iso3 = _whr_name_to_iso3(meta)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return {}
    name_col = next((c for c in reader.fieldnames if "country" in c.lower()), None)
    val_col = next((c for c in reader.fieldnames if "activity" in c.lower()), None)
    if not name_col or not val_col:
        return {}
    out: dict[str, float] = {}
    for row in reader:
        nm = _normalize_country_name(row.get(name_col) or "")
        if not nm:
            continue
        iso3 = name_to_iso3.get(nm)
        if not iso3:
            continue
        try:
            out[iso3] = float(row[val_col])
        except (ValueError, TypeError):
            continue
    return out


def fetch_activity_data() -> dict[str, float]:
    if not ACTIVITY_LOCAL.exists():
        log.info("Activity fetcher: no data file at %s — Activity subscore stays empty (Tier C / v1.1)", ACTIVITY_LOCAL)
        return {}
    try:
        return _parse_activity_csv(ACTIVITY_LOCAL.read_text())
    except Exception as exc:
        log.warning("Activity local file %s parse failed: %s", ACTIVITY_LOCAL, exc)
        return {}


def merge_prefer_first(*sources: dict[str, float]) -> dict[str, float]:
    """Union of dicts where the first non-None value wins (left-to-right)."""
    out: dict[str, float] = {}
    for src in sources:
        for k, v in src.items():
            if v is None:
                continue
            out.setdefault(k, v)
    return out


# ---------------------------------------------------------------------------
# Methodology: percentile rank within income tier
# ---------------------------------------------------------------------------

def percentile_rank_within_tier(
    values: dict[str, float],
    higher_is_better: bool = True,
    cap_pct: float | None = None,
) -> dict[str, float]:
    meta = get_country_meta()
    by_tier: dict[str, list[tuple[str, float]]] = {t: [] for t in INCOME_TIERS}
    for iso3, v in values.items():
        tier = (meta.get(iso3) or {}).get("income_tier")
        if tier in by_tier:
            by_tier[tier].append((iso3, v))

    out: dict[str, float] = {}
    for tier, items in by_tier.items():
        if len(items) < 3:
            continue  # not enough peers for a meaningful rank
        items.sort(key=lambda kv: kv[1])
        n = len(items)
        # n is guaranteed >= 3 by the len(items) < 3 guard above.
        for rank, (iso3, _v) in enumerate(items):
            pct = (rank / (n - 1)) * 100
            if not higher_is_better:
                pct = 100.0 - pct
            if cap_pct is not None:
                pct = min(pct, cap_pct)
            out[iso3] = pct
    return out


def avg_present(*xs: float | None) -> float | None:
    pres = [x for x in xs if x is not None]
    return sum(pres) / len(pres) if pres else None


# ---------------------------------------------------------------------------
# Index computation
# ---------------------------------------------------------------------------

def _safe_fetch(key: str, loader: Callable[[], dict[str, float]]) -> dict[str, float]:
    try:
        return cached(key, loader)
    except Exception as exc:
        log.warning("%s fetch failed: %s", key, exc)
        return {}


def _build_subscore_layers() -> dict[str, Any]:
    """Fetch and normalize every input. Pure data; no weight choices.

    Cached separately from the composite so weight customization doesn't
    re-trigger network fetches.
    """
    meta                 = get_country_meta()
    eurostat_marriage    = _safe_fetch("eurostat_marriage", eurostat_marriage_rate)
    eurostat_divorce     = _safe_fetch("eurostat_divorce",  eurostat_divorce_rate)
    adolescent_fertility = _safe_fetch("wb_adolescent",
                                       lambda: fetch_wb_indicator("SP.ADO.TFRT"))
    social_support       = _safe_fetch("whr_social_support", fetch_whr_social_support)
    loneliness_inv       = _safe_fetch("loneliness_csv",     fetch_loneliness_data)

    # Context indicators — six trustworthy sources that don't (yet) feed the
    # composite, but show up in /api/country/<iso>.context and are the raw
    # material the LLM narrative endpoint summarizes.
    context_layers: dict[str, dict[str, float]] = {
        "fertility_rate":            _safe_fetch("wb_tfr",         lambda: fetch_wb_indicator("SP.DYN.TFRT.IN")),
        "female_labour_force_pct":   _safe_fetch("wb_flfp",        lambda: fetch_wb_indicator("SL.TLF.CACT.FE.ZS")),
        "gdp_per_capita_usd":        _safe_fetch("wb_gdp_pc",      lambda: fetch_wb_indicator("NY.GDP.PCAP.CD")),
        "life_expectancy_years":     _safe_fetch("wb_life_exp",    lambda: fetch_wb_indicator("SP.DYN.LE00.IN")),
        "age_at_first_marriage_w":   _safe_fetch("un_wpp_smam_w",  fetch_un_wpp_age_at_marriage_women),
        "rainbow_index_0_100":       _safe_fetch("ilga_rainbow",   fetch_rainbow_index),
    }

    # UN DESA covers the world; Eurostat covers EU+EFTA with fresher numbers.
    # Use Eurostat where available, fall back to UN for everyone else.
    try:
        un_pair = cached("un_marriage_divorce", fetch_un_marriage_divorce)
    except Exception as exc:
        log.warning("un_marriage_divorce fetch failed: %s", exc)
        un_pair = ({}, {})
    un_marriage, un_divorce = un_pair if isinstance(un_pair, tuple) else ({}, {})

    marriage_rate = merge_prefer_first(eurostat_marriage, un_marriage)
    divorce_rate  = merge_prefer_first(eurostat_divorce,  un_divorce)

    activity_raw = _safe_fetch("activity_csv", fetch_activity_data)

    # Connection: average of two ranked indicators where available — WHR
    # social-support and inverted Meta-Gallup loneliness. Each is percentile-
    # ranked within tier separately so a country with only one of the two
    # still scores cleanly.
    whr_pct        = percentile_rank_within_tier(social_support, higher_is_better=True)
    loneliness_pct = percentile_rank_within_tier(loneliness_inv, higher_is_better=True)
    connection_pct: dict[str, float] = {}
    for iso in set(whr_pct) | set(loneliness_pct):
        v = avg_present(whr_pct.get(iso), loneliness_pct.get(iso))
        if v is not None:
            connection_pct[iso] = v

    # Partnership: marriage rate (v1 proxy for partnership rate); cap at 80th pct.
    # We also keep the uncapped version on the side so rule_cap_impact can
    # surface countries the cap reduced.
    partnership_pct = percentile_rank_within_tier(
        marriage_rate, higher_is_better=True, cap_pct=PARTNERSHIP_CAP_PCT,
    )
    partnership_pct_uncapped = percentile_rank_within_tier(
        marriage_rate, higher_is_better=True,
    )

    # Stability: divorce rate (lower=better) + adolescent fertility (lower=better
    # because very high values flag early/coerced unions).
    stability_div_pct = percentile_rank_within_tier(divorce_rate, higher_is_better=False)
    stability_ado_pct = percentile_rank_within_tier(adolescent_fertility, higher_is_better=False)
    stability_pct: dict[str, float] = {}
    for iso in set(stability_div_pct) | set(stability_ado_pct):
        v = avg_present(stability_div_pct.get(iso), stability_ado_pct.get(iso))
        if v is not None:
            stability_pct[iso] = v

    # Activity (Tier C — indicative only). Percentile-rank within tier just
    # like the other subscores; the methodology weight is 10% so even a noisy
    # signal can't dominate the index.
    activity_pct = percentile_rank_within_tier(activity_raw, higher_is_better=True)

    return {
        "meta": meta,
        "subscores": {
            "connection":  connection_pct,
            "partnership": partnership_pct,
            "stability":   stability_pct,
            "activity":    activity_pct,
        },
        # Side channel for the cap_impact insight rule; not exposed via the
        # composite, but available to inspect via /api/insights.
        "extras": {
            "partnership_uncapped": partnership_pct_uncapped,
        },
        "raw": {
            "marriage_rate_per_1000":         marriage_rate,
            "divorce_rate_per_1000":          divorce_rate,
            "adolescent_fertility_per_1000":  adolescent_fertility,
            "whr_social_support_pct":         social_support,
        },
        "context": context_layers,
    }


def _normalize_weights(weights: dict[str, float] | None) -> dict[str, float]:
    if not weights:
        return dict(WEIGHTS)
    out = {k: max(0.0, float(weights.get(k, WEIGHTS[k]))) for k in WEIGHTS}
    s = sum(out.values())
    if s <= 0:
        return dict(WEIGHTS)
    return {k: v / s for k, v in out.items()}


def composite_from_layers(layers: dict[str, Any], weights: dict[str, float] | None = None) -> dict[str, dict]:
    """Pure composite math given an already-built `layers` dict.

    Split out from compute_subscores so the backfill can build layers for a
    specific historical year (instead of "the latest" baked into
    _build_subscore_layers) and run the exact same scoring pipeline.
    """
    meta = layers["meta"]
    subs_layers = layers["subscores"]
    raw_layers = layers["raw"]
    w = _normalize_weights(weights)

    out: dict[str, dict] = {}
    all_iso: set[str] = set(meta)
    for sub in subs_layers.values():
        all_iso |= set(sub)

    for iso in all_iso:
        country_meta = meta.get(iso)
        if not country_meta:
            continue
        subs = {k: subs_layers[k].get(iso) for k in WEIGHTS}
        present_ab = sum(1 for k in TIER_AB_SUBSCORES if subs.get(k) is not None)
        if present_ab < MIN_TIER_AB_PRESENT:
            continue
        num = den = 0.0
        for k, v in subs.items():
            if v is None:
                continue
            num += w[k] * v
            den += w[k]
        composite = num / den if den > 0 else None
        context_layers = layers.get("context") or {}
        country_context = {key: layer.get(iso) for key, layer in context_layers.items()
                           if layer.get(iso) is not None}
        out[iso] = {
            "iso3": iso,
            "iso2": country_meta.get("iso2"),
            "name": country_meta.get("name"),
            "income_tier": country_meta.get("income_tier"),
            "region": country_meta.get("region"),
            "subscores": subs,
            "composite": round(composite, 1) if composite is not None else None,
            "used": [k for k, v in subs.items() if v is not None],
            "raw": {key: layer.get(iso) for key, layer in raw_layers.items()},
            "context": country_context,
        }
    return out


def compute_subscores(weights: dict[str, float] | None = None) -> dict[str, dict]:
    return composite_from_layers(cached("subscore_layers", _build_subscore_layers), weights)


def build_summary(weights: dict[str, float] | None = None) -> dict:
    w = _normalize_weights(weights)
    countries = compute_subscores(w)
    ranked = [c for c in countries.values() if c["composite"] is not None]
    ranked.sort(key=lambda c: c["composite"], reverse=True)
    composites = [c["composite"] for c in ranked]
    avg = round(sum(composites) / len(composites), 1) if composites else None

    subs_avg: dict[str, float | None] = {}
    for k in WEIGHTS:
        vals = [c["subscores"].get(k) for c in countries.values()
                if c["subscores"].get(k) is not None]
        subs_avg[k] = round(sum(vals) / len(vals), 1) if vals else None

    layers = cached("subscore_layers", _build_subscore_layers)
    sub_coverage = {k: len(v) for k, v in layers["subscores"].items()}

    return {
        "as_of": time.strftime("%Y-%m-%d"),
        "global_index": avg,
        "n_countries": len(ranked),
        "n_meta": len(layers["meta"]),
        "subscores_avg": subs_avg,
        "subscore_coverage": sub_coverage,
        "weights": w,
        "default_weights": dict(WEIGHTS),
        "weights_customized": w != WEIGHTS,
        "top": ranked[:10],
        "bottom": ranked[-10:][::-1],
        "coverage_note": (
            f"Live data from {sum(1 for v in layers['subscores'].values() if v)} subscore "
            f"layers. Connection (WHR) and Activity (Trends) populate when their "
            f"fetchers reach data; the index reweights over present subscores."
        ),
    }


def _parse_weight_params(args) -> dict[str, float] | None:
    """Read /api/...?w_connection=0.5&w_partnership=0.3&... query params."""
    out: dict[str, float] = {}
    for k in WEIGHTS:
        v = args.get(f"w_{k}")
        if v is None:
            continue
        try:
            out[k] = float(v)
        except ValueError:
            continue
    return out or None


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")


@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "ts": time.time()})


def _record_daily_snapshot() -> None:
    """Opportunistic snapshot: writes today's rankings to the sqlite history
    store if we don't already have a row for today. Cheap (one indexed
    query) and only fires the actual insert when the UTC day rolls over."""
    try:
        if snapshots_module.has_snapshot_for(SNAPSHOTS_DB):
            return
        countries = cached("index_map", lambda: compute_subscores())
        ranked = [c for c in countries.values() if c.get("composite") is not None]
        if not ranked:
            return
        n = snapshots_module.record_snapshot(ranked, SNAPSHOTS_DB)
        if n:
            log.info("snapshots: wrote %d rows for today", n)
    except Exception as exc:
        # History is non-critical; never let a snapshot error fail a request.
        log.warning("snapshot write failed: %s", exc)


@app.get("/api/summary")
def summary():
    weights = _parse_weight_params(request.args)
    if weights:
        return jsonify(build_summary(weights))
    payload = cached("summary", build_summary)
    _record_daily_snapshot()
    return jsonify(payload)


@app.get("/api/index")
def index_route():
    weights = _parse_weight_params(request.args)
    if weights:
        return jsonify(list(compute_subscores(weights).values()))
    return jsonify(cached("index", lambda: list(compute_subscores().values())))


def _peer_compare(c: dict, all_countries: list[dict]) -> dict:
    """Mean+std of each subscore within the country's income tier."""
    tier = c.get("income_tier")
    peers = [r for r in all_countries if r.get("income_tier") == tier and r["iso3"] != c["iso3"]]
    out: dict[str, dict] = {}
    for sub in WEIGHTS:
        vals = [r["subscores"].get(sub) for r in peers if r["subscores"].get(sub) is not None]
        if not vals:
            continue
        m = sum(vals) / len(vals)
        out[sub] = {
            "tier_mean": round(m, 1),
            "tier_n":    len(vals),
            "value":     c["subscores"].get(sub),
            "delta":     round(c["subscores"].get(sub) - m, 1) if c["subscores"].get(sub) is not None else None,
        }
    return out


def _sensitivity_payload() -> dict:
    return sensitivity_module.compute_sensitivity(
        lambda w: compute_subscores(w), dict(WEIGHTS),
    )


@app.get("/api/country/<iso>")
def country(iso: str):
    iso = iso.upper()
    weights = _parse_weight_params(request.args)
    countries = compute_subscores(weights) if weights else cached("index_map", lambda: compute_subscores())
    if iso not in countries:
        return jsonify({"error": f"country {iso} has no Love Index (insufficient data)"}), 404
    detail = dict(countries[iso])
    detail["peer_compare"] = _peer_compare(detail, list(countries.values()))
    # Sensitivity is computed against the default weights only — attaching it
    # to a custom-weight response would put a default-weights badge next to a
    # custom-weights index and silently mislead the reader.
    if not weights:
        sens = cached("sensitivity", _sensitivity_payload).get("countries", {}).get(iso)
        if sens:
            detail["sensitivity"] = sens
    return jsonify(detail)


@app.get("/api/sensitivity")
def sensitivity_route():
    return jsonify(cached("sensitivity", _sensitivity_payload))


@app.get("/api/countries")
def countries_route():
    """Full country list, sortable client-side. Includes ranked + unranked rows."""
    weights = _parse_weight_params(request.args)
    countries = compute_subscores(weights) if weights else cached("index_map", lambda: compute_subscores())
    layers = cached("subscore_layers", _build_subscore_layers)
    out = list(countries.values())
    ranked = {c["iso3"] for c in out}
    # also include unranked countries that have at least some data, with composite=None
    for iso3, m in layers["meta"].items():
        if iso3 in ranked:
            continue
        subs = {k: layers["subscores"][k].get(iso3) for k in WEIGHTS}
        if not any(v is not None for v in subs.values()):
            continue
        out.append({
            "iso3": iso3,
            "iso2": m.get("iso2"),
            "name": m.get("name"),
            "income_tier": m.get("income_tier"),
            "region": m.get("region"),
            "subscores": subs,
            "composite": None,
            "used": [k for k, v in subs.items() if v is not None],
            "raw": {key: layer.get(iso3) for key, layer in layers["raw"].items()},
            "unranked_reason": "fewer than 2 of 3 Tier-A/B subscores present",
        })
    out.sort(key=lambda c: (c["composite"] is None, -(c["composite"] or 0)))
    return jsonify(out)


@app.get("/api/insights")
def insights_route():
    weights = _parse_weight_params(request.args)
    countries = compute_subscores(weights) if weights else cached("index_map", lambda: compute_subscores())
    layers = cached("subscore_layers", _build_subscore_layers)
    partnership_uncapped = (layers.get("extras") or {}).get("partnership_uncapped") or {}

    # Time-series rule_mover needs the history store. Bind the path now so
    # the rule stays unit-testable with a synthetic accessor.
    def history_for(iso3: str) -> list[dict]:
        return snapshots_module.get_country_history(iso3, SNAPSHOTS_DB)

    return jsonify(insights_module.generate_insights(
        list(countries.values()),
        layers["meta"],
        partnership_uncapped=partnership_uncapped,
        history_accessor=history_for,
        events=insights_module.STARTER_EVENTS,
    ))


@app.get("/api/history/<iso>")
def history_country(iso: str):
    iso = iso.upper()
    days = request.args.get("days", default=365, type=int)
    days = max(1, min(days, 3650))
    return jsonify({
        "iso3":   iso,
        "days":   days,
        "points": snapshots_module.get_country_history(iso, SNAPSHOTS_DB, days=days),
    })


@app.get("/api/history/global")
def history_global():
    days = request.args.get("days", default=365, type=int)
    days = max(1, min(days, 3650))
    return jsonify({
        "days":   days,
        "points": snapshots_module.get_global_history(SNAPSHOTS_DB, days=days),
        "store":  snapshots_module.n_snapshots(SNAPSHOTS_DB),
    })


@app.get("/api/og/global.svg")
@app.get("/api/og/global.png")  # alias so Facebook crawlers attempting .png still get a response
def og_global():
    payload = cached("summary", build_summary)
    svg = og_module.render_global_card(payload)
    return svg, 200, {"Content-Type": "image/svg+xml", "Cache-Control": "public, max-age=3600"}


@app.get("/api/og/<iso>.svg")
@app.get("/api/og/<iso>.png")
def og_country(iso: str):
    iso = iso.upper()
    countries = cached("index_map", lambda: compute_subscores())
    if iso not in countries:
        # Fall back to the global card so social crawlers always get *something*.
        return og_global()
    svg = og_module.render_country_card(countries[iso])
    return svg, 200, {"Content-Type": "image/svg+xml", "Cache-Control": "public, max-age=3600"}


@app.get("/methodology.html")
def methodology_page():
    return send_from_directory(STATIC_DIR, "methodology.html")


@app.get("/docs.html")
def docs_page():
    return send_from_directory(STATIC_DIR, "docs.html")


@app.get("/api/sources")
def sources():
    layers = cached("subscore_layers", _build_subscore_layers)
    return jsonify({
        "weights":              WEIGHTS,
        "min_tier_ab_present":  MIN_TIER_AB_PRESENT,
        "partnership_cap_pct":  PARTNERSHIP_CAP_PCT,
        "subscore_coverage":    {k: len(v) for k, v in layers["subscores"].items()},
        "feeds": {
            "eurostat_demo_nind":     {"tier": "A", "covers": "EU + EFTA",      "in_use": True,  "feeds": "partnership"},
            "eurostat_demo_ndivind":  {"tier": "A", "covers": "EU + EFTA",      "in_use": True,  "feeds": "stability"},
            "world_bank_wdi":         {"tier": "A", "covers": "global",         "in_use": True,  "feeds": "stability + meta"},
            "world_happiness_report": {"tier": "B", "covers": "~150 countries", "in_use": bool(layers["subscores"]["connection"]), "feeds": "connection"},
            "meta_gallup_loneliness": {"tier": "B", "covers": "~140 countries", "in_use": LONELINESS_LOCAL.exists(), "feeds": "connection (combined with WHR)"},
            "wb_fertility_rate":      {"tier": "A", "covers": "global", "in_use": bool((layers.get("context") or {}).get("fertility_rate")),             "feeds": "context"},
            "wb_female_lfp":          {"tier": "A", "covers": "global", "in_use": bool((layers.get("context") or {}).get("female_labour_force_pct")),    "feeds": "context"},
            "wb_gdp_per_capita":      {"tier": "A", "covers": "global", "in_use": bool((layers.get("context") or {}).get("gdp_per_capita_usd")),         "feeds": "context"},
            "wb_life_expectancy":     {"tier": "A", "covers": "global", "in_use": bool((layers.get("context") or {}).get("life_expectancy_years")),      "feeds": "context"},
            "un_wpp_age_at_marriage": {"tier": "A", "covers": "global", "in_use": bool((layers.get("context") or {}).get("age_at_first_marriage_w")),    "feeds": "context (Stability candidate)"},
            "ilga_rainbow_index":     {"tier": "A", "covers": "~50 countries", "in_use": RAINBOW_LOCAL.exists(),                                          "feeds": "context (Freedom dimension)"},
            "un_desa":                {"tier": "A", "covers": "global",         "in_use": UN_MARRIAGE_LOCAL.exists(), "feeds": "partnership + stability worldwide (fallback after Eurostat)"},
            "activity_csv":           {"tier": "C", "covers": "operator-supplied", "in_use": bool(layers["subscores"]["activity"]), "feeds": "activity"},
        },
    })


@app.get("/")
def root():
    return send_from_directory(STATIC_DIR, "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7060"))
    app.run(host="0.0.0.0", port=port, debug=False)

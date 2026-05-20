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
from collections import OrderedDict
from typing import Any, Callable

import requests
from flask import Flask, jsonify, request, send_from_directory

import insights as insights_module
import sensitivity as sensitivity_module

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
    "country_meta":      7 * 24 * 3600,   # 7d  (income tiers update annually)
    "eurostat_marriage": 24 * 3600,
    "eurostat_divorce":  24 * 3600,
    "wb_adolescent":     24 * 3600,
    "whr_social_support": 7 * 24 * 3600,  # WHR is annual
    "summary":           60 * 60,
    "index":             60 * 60,
    "index_map":         60 * 60,
    "subscore_layers":   60 * 60,
    "sensitivity":       60 * 60,
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
    marriage_rate        = _safe_fetch("eurostat_marriage", eurostat_marriage_rate)
    divorce_rate         = _safe_fetch("eurostat_divorce",  eurostat_divorce_rate)
    adolescent_fertility = _safe_fetch("wb_adolescent",
                                       lambda: fetch_wb_indicator("SP.ADO.TFRT"))
    social_support       = _safe_fetch("whr_social_support", fetch_whr_social_support)

    # Connection: WHR social-support index (0-100), already on the right scale.
    # Rank within income tier so cross-tier comparisons stay fair.
    connection_pct = percentile_rank_within_tier(social_support, higher_is_better=True)

    # Partnership: marriage rate (v1 proxy for partnership rate); cap at 80th pct.
    partnership_pct = percentile_rank_within_tier(
        marriage_rate, higher_is_better=True, cap_pct=PARTNERSHIP_CAP_PCT,
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

    # Activity: still stubbed in v2.
    activity_pct: dict[str, float] = {}

    return {
        "meta": meta,
        "subscores": {
            "connection":  connection_pct,
            "partnership": partnership_pct,
            "stability":   stability_pct,
            "activity":    activity_pct,
        },
        "raw": {
            "marriage_rate_per_1000":         marriage_rate,
            "divorce_rate_per_1000":          divorce_rate,
            "adolescent_fertility_per_1000":  adolescent_fertility,
            "whr_social_support_pct":         social_support,
        },
    }


def _normalize_weights(weights: dict[str, float] | None) -> dict[str, float]:
    if not weights:
        return dict(WEIGHTS)
    out = {k: max(0.0, float(weights.get(k, WEIGHTS[k]))) for k in WEIGHTS}
    s = sum(out.values())
    if s <= 0:
        return dict(WEIGHTS)
    return {k: v / s for k, v in out.items()}


def compute_subscores(weights: dict[str, float] | None = None) -> dict[str, dict]:
    layers = cached("subscore_layers", _build_subscore_layers)
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
        }
    return out


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


@app.get("/api/summary")
def summary():
    weights = _parse_weight_params(request.args)
    if weights:
        return jsonify(build_summary(weights))
    return jsonify(cached("summary", build_summary))


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
    return jsonify(insights_module.generate_insights(list(countries.values()), layers["meta"]))


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
            "google_trends":          {"tier": "C", "covers": "global",         "in_use": False, "feeds": "activity"},
            "un_desa":                {"tier": "A", "covers": "global",         "in_use": False, "feeds": "partnership + stability worldwide"},
        },
    })


@app.get("/")
def root():
    return send_from_directory(STATIC_DIR, "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7060"))
    app.run(host="0.0.0.0", port=port, debug=False)

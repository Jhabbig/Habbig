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
from flask import Flask, jsonify, send_from_directory

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
    "summary":           60 * 60,
    "index":             60 * 60,
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


def cached(key: str, loader: Callable[[], Any]) -> Any:
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
        for rank, (iso3, _v) in enumerate(items):
            pct = (rank / (n - 1)) * 100 if n > 1 else 50.0
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


def compute_subscores() -> dict[str, dict]:
    meta = get_country_meta()

    marriage_rate        = _safe_fetch("eurostat_marriage", eurostat_marriage_rate)
    divorce_rate         = _safe_fetch("eurostat_divorce",  eurostat_divorce_rate)
    adolescent_fertility = _safe_fetch("wb_adolescent",
                                       lambda: fetch_wb_indicator("SP.ADO.TFRT"))

    # Partnership: marriage rate (v1 proxy for partnership rate)
    partnership_pct = percentile_rank_within_tier(
        marriage_rate, higher_is_better=True, cap_pct=PARTNERSHIP_CAP_PCT,
    )

    # Stability: combine divorce rate (lower=better) + adolescent fertility
    # (lower=better — high values flag early/coerced unions).
    stability_div_pct = percentile_rank_within_tier(divorce_rate, higher_is_better=False)
    stability_ado_pct = percentile_rank_within_tier(adolescent_fertility, higher_is_better=False)
    stability = {}
    for iso in set(stability_div_pct) | set(stability_ado_pct):
        stability[iso] = avg_present(stability_div_pct.get(iso), stability_ado_pct.get(iso))

    # Connection + Activity: stubbed in v1
    connection: dict[str, float] = {}
    activity: dict[str, float] = {}

    out: dict[str, dict] = {}
    all_iso = set(connection) | set(partnership_pct) | set(stability) | set(activity) | set(meta)
    for iso in all_iso:
        if iso not in meta:
            continue
        subs = {
            "connection":  connection.get(iso),
            "partnership": partnership_pct.get(iso),
            "stability":   stability.get(iso),
            "activity":    activity.get(iso),
        }
        present_ab = sum(1 for k in TIER_AB_SUBSCORES if subs.get(k) is not None)
        if present_ab < MIN_TIER_AB_PRESENT:
            continue
        # weighted average over present subscores (weights renormalize)
        num = den = 0.0
        for k, v in subs.items():
            if v is None:
                continue
            num += WEIGHTS[k] * v
            den += WEIGHTS[k]
        composite = num / den if den > 0 else None
        out[iso] = {
            "iso3": iso,
            "iso2": meta[iso].get("iso2"),
            "name": meta[iso].get("name"),
            "income_tier": meta[iso].get("income_tier"),
            "region": meta[iso].get("region"),
            "subscores": subs,
            "composite": round(composite, 1) if composite is not None else None,
            "used": [k for k, v in subs.items() if v is not None],
            "raw": {
                "marriage_rate_per_1000":      marriage_rate.get(iso),
                "divorce_rate_per_1000":       divorce_rate.get(iso),
                "adolescent_fertility_per_1000": adolescent_fertility.get(iso),
            },
        }
    return out


def build_summary() -> dict:
    countries = compute_subscores()
    rows = sorted(
        countries.values(),
        key=lambda c: (c["composite"] is not None, c["composite"] or 0),
        reverse=True,
    )
    composites = [c["composite"] for c in countries.values() if c["composite"] is not None]
    avg = round(sum(composites) / len(composites), 1) if composites else None

    subs_avg: dict[str, float | None] = {}
    for k in WEIGHTS:
        vals = [c["subscores"].get(k) for c in countries.values()
                if c["subscores"].get(k) is not None]
        subs_avg[k] = round(sum(vals) / len(vals), 1) if vals else None

    return {
        "as_of": time.strftime("%Y-%m-%d"),
        "global_index": avg,
        "n_countries": len(composites),
        "subscores_avg": subs_avg,
        "weights": WEIGHTS,
        "top": rows[:10],
        "bottom": [r for r in rows if r["composite"] is not None][-10:][::-1],
        "coverage_note": (
            "v1 covers Europe (Eurostat marriage + divorce) plus a global "
            "Stability indicator (adolescent fertility, World Bank). "
            "Connection (WHR) and Activity (Trends) land in v1.1."
        ),
    }


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
    return jsonify(cached("summary", build_summary))


@app.get("/api/index")
def index_route():
    return jsonify(cached("index", lambda: list(compute_subscores().values())))


@app.get("/api/country/<iso>")
def country(iso: str):
    iso = iso.upper()
    countries = compute_subscores()
    if iso not in countries:
        return jsonify({"error": f"country {iso} has no Love Index (insufficient data)"}), 404
    return jsonify(countries[iso])


@app.get("/api/sources")
def sources():
    return jsonify({
        "weights": WEIGHTS,
        "min_tier_ab_present": MIN_TIER_AB_PRESENT,
        "partnership_cap_pct": PARTNERSHIP_CAP_PCT,
        "feeds": {
            "eurostat_demo_nind":     {"tier": "A", "covers": "EU + EFTA",     "in_use": True},
            "eurostat_demo_ndivind":  {"tier": "A", "covers": "EU + EFTA",     "in_use": True},
            "world_bank_wdi":         {"tier": "A", "covers": "global",        "in_use": True},
            "world_happiness_report": {"tier": "B", "covers": "~150 countries","in_use": False},
            "google_trends":          {"tier": "C", "covers": "global",        "in_use": False},
            "un_desa":                {"tier": "A", "covers": "global",        "in_use": False},
        },
    })


@app.get("/")
def root():
    return send_from_directory(STATIC_DIR, "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7060"))
    app.run(host="0.0.0.0", port=port, debug=False)

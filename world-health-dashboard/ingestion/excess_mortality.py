"""Excess mortality from Our World in Data.

Source: github.com/owid/covid-19-data, file
    public/data/excess_mortality/excess_mortality.csv

Provides P-scores (% deviation from a 2015-2019 baseline) plus projected
excess deaths since 2020. Location names in the CSV are mostly clean English
country names; OWID does not provide ISO3 in this file directly so we resolve
by name.

Cached on disk for 12 hours — OWID updates the file roughly weekly when new
country reports drop.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
import urllib.request
from pathlib import Path
from threading import Lock

from .country_codes import INDEX as COUNTRY_INDEX

log = logging.getLogger(__name__)

CSV_URL = "https://raw.githubusercontent.com/owid/covid-19-data/master/public/data/excess_mortality/excess_mortality.csv"
CACHE_DIR = Path(__file__).parent.parent / "cache" / "excess_mortality"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

CACHE_TTL_SECONDS = 12 * 3600

_lock = Lock()

NAME_TO_ISO: dict[str, str] = {n.lower(): iso for iso, (n, _) in COUNTRY_INDEX.items()}
NAME_ALIASES: dict[str, str] = {
    "United States": "USA",
    "United Kingdom": "GBR",
    "Russia": "RUS",
    "Iran": "IRN",
    "South Korea": "KOR",
    "Czech Republic": "CZE",
    "Slovak Republic": "SVK",
    "Bolivia": "BOL",
    "Venezuela": "VEN",
    "Vietnam": "VNM",
    "Hong Kong": "CHN",
    "Macao": "CHN",
    "Taiwan": "TWN",
    "Faroe Islands": "DNK",
    "Greenland": "DNK",
    "Gibraltar": "GBR",
    "Reunion": "FRA",
    "Martinique": "FRA",
    "Guadeloupe": "FRA",
    "French Guiana": "FRA",
    "Mayotte": "FRA",
    "Aruba": "NLD",
    "Curacao": "NLD",
    "Bermuda": "GBR",
    "Cape Verde": "CPV",
    "Cabo Verde": "CPV",
    "England & Wales": "GBR",
}
NAME_TO_ISO.update({k.lower(): v for k, v in NAME_ALIASES.items()})


def _resolve(name: str) -> str | None:
    if not name:
        return None
    return NAME_TO_ISO.get(name.strip().lower())


def _cache_path() -> Path:
    return CACHE_DIR / "excess_mortality.json"


def _read_cache() -> dict | None:
    p = _cache_path()
    if not p.exists():
        return None
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Excess mortality cache unreadable: %s", exc)
        return None
    if (time.time() - body.get("fetched_at", 0)) > CACHE_TTL_SECONDS:
        return None
    return body


def _write_cache(payload: dict) -> None:
    try:
        _cache_path().write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:
        log.warning("Excess mortality cache write failed: %s", exc)


def _fetch_csv(timeout: float = 30.0) -> str:
    req = urllib.request.Request(CSV_URL, headers={
        "User-Agent": "world-health-dashboard/0.2",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted host)
        return resp.read().decode("utf-8", errors="replace")


def _parse_csv(body: str) -> dict:
    """Return {iso3: [{date, p_score, excess_per_million, cum_excess_per_million}]}."""
    reader = csv.DictReader(io.StringIO(body))
    by_country: dict[str, list[dict]] = {}
    skipped: set[str] = set()

    for row in reader:
        loc = (row.get("location") or "").strip()
        iso = _resolve(loc)
        if not iso:
            skipped.add(loc)
            continue
        date = row.get("date")
        if not date:
            continue
        try:
            p = float(row["p_scores_all_ages"]) if row.get("p_scores_all_ages") else None
        except (ValueError, TypeError):
            p = None
        try:
            excess_pm = float(row["excess_per_million_proj_all_ages"]) if row.get("excess_per_million_proj_all_ages") else None
        except (ValueError, TypeError):
            excess_pm = None
        try:
            cum_pm = float(row["cum_excess_per_million_proj_all_ages"]) if row.get("cum_excess_per_million_proj_all_ages") else None
        except (ValueError, TypeError):
            cum_pm = None
        by_country.setdefault(iso, []).append({
            "date": date,
            "p_score": p,
            "excess_per_million": excess_pm,
            "cum_excess_per_million": cum_pm,
        })

    # Sort each country's series by date, and compute latest cumulative.
    latest: dict[str, dict] = {}
    for iso, pts in by_country.items():
        pts.sort(key=lambda p: p["date"])
        non_null = [p for p in pts if p.get("cum_excess_per_million") is not None]
        if non_null:
            latest[iso] = non_null[-1]

    if skipped:
        log.info("Excess mortality: %d unresolved locations (samples: %s)",
                 len(skipped), ", ".join(sorted(skipped)[:5]))
    return {"by_country": by_country, "latest": latest}


def fetch(force: bool = False) -> dict:
    with _lock:
        if not force:
            cached = _read_cache()
            if cached:
                return cached

    try:
        body = _fetch_csv()
    except Exception as exc:
        log.warning("Excess mortality fetch failed: %s", exc)
        p = _cache_path()
        if p.exists():
            try:
                stale = json.loads(p.read_text(encoding="utf-8"))
                stale["stale"] = True
                return stale
            except Exception:
                pass
        return {"by_country": {}, "latest": {}, "fetched_at": time.time(), "error": str(exc)}

    parsed = _parse_csv(body)
    payload = {
        "by_country": parsed["by_country"],
        "latest": parsed["latest"],
        "fetched_at": time.time(),
        "stale": False,
        "source": "OWID via github.com/owid/covid-19-data",
    }
    with _lock:
        _write_cache(payload)
    log.info("Excess mortality: %d countries, %d total points",
             len(parsed["by_country"]),
             sum(len(v) for v in parsed["by_country"].values()))
    return payload


def country_series(iso3: str) -> list[dict]:
    payload = fetch()
    return payload.get("by_country", {}).get(iso3.upper(), [])


def latest_globe_layer() -> dict:
    """Return {iso3: cum_excess_per_million} for the latest available date
    (typically 2023-2024) — used for the globe choropleth tab."""
    payload = fetch()
    out: dict[str, dict] = {}
    for iso, pt in payload.get("latest", {}).items():
        if pt.get("cum_excess_per_million") is not None:
            out[iso] = {
                "value": pt["cum_excess_per_million"],
                "date": pt["date"],
            }
    return {
        "by_iso3": out,
        "fetched_at": payload.get("fetched_at"),
        "stale": payload.get("stale", False),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    p = fetch(force=True)
    print(f"Countries: {len(p['by_country'])}")
    print(f"USA latest: {p['latest'].get('USA')}")
    print(f"DEU latest: {p['latest'].get('DEU')}")
    print(f"GBR latest: {p['latest'].get('GBR')}")

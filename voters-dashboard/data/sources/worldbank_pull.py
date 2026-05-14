#!/usr/bin/env python3
"""
World Bank ETL.

Pulls macro indicators per country from the World Bank API and writes
both a JSON cache (`data/cache/worldbank.json`, hot-path) and a committed
YAML snapshot (`data/snapshot_worldbank.yaml`, fallback).

Endpoint shape:
    https://api.worldbank.org/v2/country/{iso}/indicator/{code}?format=json

Indicators (spec-driven):
    NY.GDP.PCAP.CD      - GDP per capita, current USD
    SP.POP.TOTL         - Population, total
    IT.NET.USER.ZS      - Internet penetration, % of population
    EG.USE.PCAP.KG.OE   - Energy use per capita, kg oil-equivalent
    SE.SEC.ENRR         - Gross secondary-education enrollment, %

Additional macro indicators we already display:
    SP.URB.TOTL.IN.ZS   - Urban population, %
    SP.DYN.AMRT.MA      - Adult male mortality
    SL.UEM.TOTL.ZS      - Unemployment, %
    FP.CPI.TOTL.ZG      - Inflation, CPI %

Cadence: monthly. Run via cron, GitHub Action, or scheduled task.
Falls back to last-known-good on any network failure (per indicator),
and to the committed YAML snapshot if the JSON cache is missing.

Usage:
    python3 data/sources/worldbank_pull.py
"""
from __future__ import annotations

import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import rate_limit, read_existing, write_overlay  # noqa: E402

INDICATORS = [
    ("NY.GDP.PCAP.CD",      "gdp_per_capita_usd"),
    ("SP.POP.TOTL",         "population_total"),
    ("IT.NET.USER.ZS",      "internet_pct"),
    ("EG.USE.PCAP.KG.OE",   "energy_use_per_capita_kgoe"),
    ("SE.SEC.ENRR",         "secondary_enrollment_pct"),
    ("SP.URB.TOTL.IN.ZS",   "urban_pct"),
    ("SP.DYN.AMRT.MA",      "adult_mortality_male"),
    ("SL.UEM.TOTL.ZS",      "unemployment_pct"),
    ("FP.CPI.TOTL.ZG",      "inflation_cpi_pct"),
]

ISOS = [
    "USA", "GBR", "DEU", "FRA", "IND", "BRA", "MEX", "ARG", "TUR",
    "ISR", "JPN", "KOR", "IDN", "ITA", "POL", "CAN", "AUS",
    "NGA", "ZAF", "PHL", "PAK", "UKR", "TWN", "IRN",
    "EGY", "VEN", "THA",
]


def fetch_indicator(iso, code):
    """Latest non-null observation for one ISO + indicator. Rate-limited."""
    rate_limit()
    url = "https://api.worldbank.org/v2/country/" + iso + "/indicator/" + code + "?format=json&per_page=10"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "narve-voters/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, socket.timeout, TimeoutError,
            json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, list) or len(data) < 2 or not isinstance(data[1], list):
        return None
    for row in data[1]:
        v = row.get("value")
        if v is not None:
            return float(v)
    return None


def shape_value(name, v):
    if name == "population_total":
        return round(v / 1_000_000, 1)
    if name in ("urban_pct", "unemployment_pct", "inflation_cpi_pct",
                "internet_pct", "secondary_enrollment_pct"):
        return round(v, 1)
    if name in ("gdp_per_capita_usd", "energy_use_per_capita_kgoe", "adult_mortality_male"):
        return round(v, 0)
    return v


def shape_field(name):
    return "population_m" if name == "population_total" else name


def main():
    by_iso = {}
    failures = 0
    for iso in ISOS:
        rec = {}
        for code, name in INDICATORS:
            v = fetch_indicator(iso, code)
            if v is None:
                failures += 1
                continue
            rec[shape_field(name)] = shape_value(name, v)
        if rec:
            by_iso[iso] = rec

    if not by_iso:
        existing = read_existing("worldbank")
        if existing:
            print("worldbank_pull: all fetches failed, keeping previous cache", file=sys.stderr)
            return 1
        print("worldbank_pull: all fetches failed and no prior cache", file=sys.stderr)
        return 2

    path = write_overlay(
        "worldbank",
        {
            "source": "worldbank",
            "source_url": "https://api.worldbank.org/v2/",
            "cadence": "monthly",
            "indicators": [shape_field(n) for _, n in INDICATORS],
            "by_iso": by_iso,
        },
    )
    print("worldbank_pull: wrote " + str(path) + " (" + str(len(by_iso)) + " countries, " + str(failures) + " indicator failures)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

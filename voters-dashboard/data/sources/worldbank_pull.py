#!/usr/bin/env python3
"""
World Bank ETL — population, median age, urbanisation, GDP per capita.

Idempotent. Run nightly. Falls back to last-known-good on network failure.

Usage:
    python3 data/sources/worldbank_pull.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import read_existing, write_overlay  # noqa: E402

# (indicator code, our field name)
INDICATORS = [
    ("SP.POP.TOTL",         "population_total"),
    ("SP.URB.TOTL.IN.ZS",   "urban_pct"),
    ("SP.DYN.AMRT.MA",      "adult_mortality_male"),
    ("NY.GDP.PCAP.CD",      "gdp_per_capita_usd"),
    ("SL.UEM.TOTL.ZS",      "unemployment_pct"),
    ("FP.CPI.TOTL.ZG",      "inflation_cpi_pct"),
]

ISOS = [
    "USA", "GBR", "DEU", "FRA", "IND", "BRA", "MEX", "ARG", "TUR",
    "ISR", "JPN", "KOR", "IDN", "ITA", "POL", "CAN", "AUS",
    "NGA", "ZAF", "PHL", "PAK", "UKR", "TWN", "IRN",
    "EGY", "VEN", "THA",
]


def fetch_indicator(iso: str, code: str) -> float | None:
    """Latest non-null observation for one ISO + indicator."""
    url = f"https://api.worldbank.org/v2/country/{iso}/indicator/{code}?format=json&per_page=10"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    if not isinstance(data, list) or len(data) < 2 or not isinstance(data[1], list):
        return None
    for row in data[1]:
        v = row.get("value")
        if v is not None:
            return float(v)
    return None


def main() -> int:
    by_iso: dict[str, dict] = {}
    failures = 0
    for iso in ISOS:
        rec: dict = {}
        for code, name in INDICATORS:
            v = fetch_indicator(iso, code)
            if v is None:
                failures += 1
                continue
            if name == "population_total":
                rec["population_m"] = round(v / 1_000_000, 1)
            elif name in ("urban_pct", "unemployment_pct", "inflation_cpi_pct"):
                rec[name] = round(v, 1)
            elif name == "gdp_per_capita_usd":
                rec[name] = round(v, 0)
            else:
                rec[name] = v
        if rec:
            by_iso[iso] = rec

    if not by_iso:
        existing = read_existing("worldbank")
        if existing:
            print("worldbank_pull: all fetches failed, keeping previous cache", file=sys.stderr)
            return 1
        print("worldbank_pull: all fetches failed and no prior cache", file=sys.stderr)
        return 2

    path = write_overlay("worldbank", {"by_iso": by_iso, "indicators": [n for _, n in INDICATORS]})
    print(f"worldbank_pull: wrote {path} ({len(by_iso)} countries, {failures} indicator failures)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

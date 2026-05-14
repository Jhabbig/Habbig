#!/usr/bin/env python3
"""
V-Dem ETL - democracy indicators (Electoral Democracy Index v2x_polyarchy,
Liberal Democracy Index v2x_libdem).

V-Dem releases its full dataset as an annual CSV/Stata export (their R
package is canonical but ships the same data). There is no public live API,
so we use a two-tier strategy:

  1. Bundled fallback values for both indices (V-Dem v14, country-year 2023).
     This gives the dashboard a stable last-known-good for every priority
     country without any network call.

  2. Optional CSV override: pass --csv path/to/v-dem.csv (or set V_DEM_CSV)
     pointing at a fresher V-Dem export - the script will read both columns
     and refresh the overlay.

CSV download (~150MB, manual refresh):
    https://v-dem.net/static/website/img/v-dem_data_v13.csv

Cadence: monthly (manual once a year when V-Dem publishes a new release).

Output: data/cache/vdem.json (hot path) + data/snapshot_vdem.yaml
(human-readable last-known-good, committed to the repo).

Usage:
    python3 data/sources/vdem_pull.py [--csv path/to/v-dem.csv]
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import read_existing, write_overlay  # noqa: E402

POLYARCHY_FALLBACK = {
    "USA": 0.78, "GBR": 0.81, "DEU": 0.85, "FRA": 0.79, "IND": 0.36,
    "BRA": 0.62, "MEX": 0.55, "ARG": 0.69, "TUR": 0.27, "ISR": 0.59,
    "JPN": 0.74, "KOR": 0.79, "IDN": 0.49, "ITA": 0.74, "POL": 0.61,
    "CAN": 0.84, "AUS": 0.83, "NGA": 0.41, "ZAF": 0.64, "PHL": 0.42,
    "PAK": 0.27, "UKR": 0.43, "TWN": 0.81, "IRN": 0.16, "EGY": 0.20,
    "VEN": 0.24, "THA": 0.39,
}

LIBDEM_FALLBACK = {
    "USA": 0.66, "GBR": 0.71, "DEU": 0.78, "FRA": 0.67, "IND": 0.21,
    "BRA": 0.49, "MEX": 0.41, "ARG": 0.55, "TUR": 0.13, "ISR": 0.41,
    "JPN": 0.65, "KOR": 0.69, "IDN": 0.32, "ITA": 0.65, "POL": 0.42,
    "CAN": 0.76, "AUS": 0.76, "NGA": 0.27, "ZAF": 0.52, "PHL": 0.29,
    "PAK": 0.14, "UKR": 0.27, "TWN": 0.72, "IRN": 0.05, "EGY": 0.08,
    "VEN": 0.10, "THA": 0.21,
}


def load_csv(path):
    """Parse a V-Dem CSV. Extracts v2x_polyarchy and v2x_libdem per ISO3,
    keeping the latest year encountered.
    """
    out = {}
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            iso = (row.get("country_text_id") or "").strip().upper()
            if not iso:
                continue
            rec = {}
            for csv_col, our_field in (("v2x_polyarchy", "vdem_edi"),
                                       ("v2x_libdem",    "vdem_libdem")):
                raw = (row.get(csv_col) or "").strip()
                if not raw:
                    continue
                try:
                    v = float(raw)
                except ValueError:
                    continue
                if 0 <= v <= 1:
                    rec[our_field] = round(v, 3)
            if rec:
                out[iso] = rec
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default=os.environ.get("V_DEM_CSV", ""))
    args = p.parse_args()

    by_iso = {}
    csv_loaded = False
    if args.csv:
        path = Path(args.csv)
        if not path.exists():
            print("vdem_pull: --csv " + str(path) + " not found, falling back to bundled", file=sys.stderr)
        else:
            by_iso = load_csv(path)
            csv_loaded = True
            print("vdem_pull: parsed " + str(len(by_iso)) + " countries from " + str(path))

    if not by_iso:
        print("vdem_pull: no CSV provided, using bundled fallback (V-Dem v14, 2023)")
        all_isos = set(POLYARCHY_FALLBACK) | set(LIBDEM_FALLBACK)
        for iso in all_isos:
            rec = {}
            if iso in POLYARCHY_FALLBACK:
                rec["vdem_edi"] = POLYARCHY_FALLBACK[iso]
            if iso in LIBDEM_FALLBACK:
                rec["vdem_libdem"] = LIBDEM_FALLBACK[iso]
            if rec:
                by_iso[iso] = rec

    if not by_iso:
        existing = read_existing("vdem")
        if existing:
            print("vdem_pull: empty output, keeping previous cache", file=sys.stderr)
            return 1
        return 2

    path = write_overlay(
        "vdem",
        {
            "source": "vdem",
            "source_url": "https://v-dem.net/data/the-v-dem-dataset/",
            "cadence": "monthly",
            "release": "v14" if not csv_loaded else "csv_override",
            "indicators": ["vdem_edi", "vdem_libdem"],
            "by_iso": by_iso,
        },
    )
    print("vdem_pull: wrote " + str(path) + " (" + str(len(by_iso)) + " countries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
V-Dem ETL — democracy indicators (electoral democracy index, liberal democracy
index, regime classification).

V-Dem ships an annual dataset rather than a live API. The robust path is:
  1. Maintain a small bundled CSV of the latest year's per-country values.
  2. Allow optional override via V_DEM_CSV env var pointing at a fresher CSV.
  3. On fresh-data day each year, drop in the new CSV, run this script,
     and the dashboard picks it up via the overlay.

This script is idempotent and produces the same shape as worldbank_pull.

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

# Bundled fallback values (from V-Dem v14, year 2023 release; replace by
# pointing --csv at a fresher V-Dem export). Values are the country_year
# polyarchy ("Electoral Democracy Index") in [0,1].
BUNDLED_FALLBACK = {
    "USA": 0.78, "GBR": 0.81, "DEU": 0.85, "FRA": 0.79, "IND": 0.36,
    "BRA": 0.62, "MEX": 0.55, "ARG": 0.69, "TUR": 0.27, "ISR": 0.59,
    "JPN": 0.74, "KOR": 0.79, "IDN": 0.49, "ITA": 0.74, "POL": 0.61,
    "CAN": 0.84, "AUS": 0.83, "NGA": 0.41, "ZAF": 0.64, "PHL": 0.42,
    "PAK": 0.27, "UKR": 0.43, "TWN": 0.81, "IRN": 0.16, "EGY": 0.20,
    "VEN": 0.24, "THA": 0.39,
}


def load_csv(path: Path) -> dict[str, float]:
    """Parse a V-Dem CSV with at minimum columns: country_text_id, v2x_polyarchy."""
    out: dict[str, float] = {}
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            iso = (row.get("country_text_id") or "").strip().upper()
            try:
                v = float(row.get("v2x_polyarchy") or "")
            except ValueError:
                continue
            if iso and 0 <= v <= 1:
                # Keep the latest year encountered (CSV is usually sorted)
                out[iso] = v
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default=os.environ.get("V_DEM_CSV", ""))
    args = p.parse_args()

    edi: dict[str, float] = {}
    if args.csv:
        path = Path(args.csv)
        if not path.exists():
            print(f"vdem_pull: --csv {path} not found", file=sys.stderr)
            return 2
        edi = load_csv(path)
        print(f"vdem_pull: parsed {len(edi)} countries from {path}")

    if not edi:
        print("vdem_pull: no CSV provided, using bundled fallback")
        edi = BUNDLED_FALLBACK

    by_iso = {iso: {"vdem_edi": v} for iso, v in edi.items()}
    path = write_overlay("vdem", {"by_iso": by_iso})
    print(f"vdem_pull: wrote {path} ({len(by_iso)} countries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

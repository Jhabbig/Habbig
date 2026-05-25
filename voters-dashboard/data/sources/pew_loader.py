#!/usr/bin/env python3
"""
Pew Global Attitudes loader.

Pew releases survey datasets as XLSX/CSV downloads, not via API. This loader:

  - Reads any CSVs placed in `data/sources/pew_drops/` matching the
    convention `<topic>_<year>.csv` with columns `iso, issue, pct, year`.
  - Aggregates and writes an overlay keyed by ISO3.

If no drops are present, writes an empty overlay so the rest of the
pipeline keeps a stable shape.

Convention for drop CSVs:
    iso,issue,pct,year,source_url
    USA,Inflation / cost of living,41,2026,https://...

Usage:
    python3 data/sources/pew_loader.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import write_overlay  # noqa: E402

DROP_DIR = Path(__file__).resolve().parent / "pew_drops"


def main() -> int:
    by_iso: dict[str, list[dict]] = {}
    if not DROP_DIR.exists():
        DROP_DIR.mkdir(parents=True, exist_ok=True)
        print(f"pew_loader: created empty drop dir {DROP_DIR}")

    for csv_path in sorted(DROP_DIR.glob("*.csv")):
        with csv_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                iso = (row.get("iso") or "").strip().upper()
                issue = (row.get("issue") or "").strip()
                try:
                    pct = float(row.get("pct") or 0)
                except ValueError:
                    continue
                if not (iso and issue and 0 <= pct <= 100):
                    continue
                by_iso.setdefault(iso, []).append({
                    "issue": issue,
                    "pct": pct,
                    "year": (row.get("year") or "").strip(),
                    "source_url": (row.get("source_url") or "").strip(),
                    "drop_file": csv_path.name,
                })

    path = write_overlay("pew", {"by_iso": by_iso})
    n = sum(len(v) for v in by_iso.values())
    print(f"pew_loader: wrote {path} ({n} rows across {len(by_iso)} countries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

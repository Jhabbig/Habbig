#!/usr/bin/env python3
"""
Elections calendar ETL.

There is no single clean public API for "every election in every country."
The two best public references are:
  - IFES Election Guide (https://www.electionguide.org/)
  - International IDEA's elections calendar

Both serve HTML, not JSON. To avoid brittle scraping in v1, this script
ships a hand-curated `bundled_calendar` that is the source of truth for
slice 1 (the same dates already encoded in countries.yaml under each
country's `elections` block — but here as a flat list for dedicated
calendar overlay use).

When a slice 2 stretch goal is to wire real ETL, replace `bundled_calendar`
with a parser; the rest of the pipeline (write_overlay, server side
merging) stays the same.

Usage:
    python3 data/sources/elections_calendar.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import write_overlay  # noqa: E402

# Elections beyond what's already in countries.yaml — sub-national and
# secondary contests we want shown on the calendar but didn't bloat the
# country YAML with. Format matches the elections[] entry shape.
SUPPLEMENTARY_BY_ISO: dict[str, list[dict]] = {
    "USA": [
        {"date": "2026-11-03", "type": "Gubernatorial races (36 states)",
         "stakes": "Includes CA, FL, TX, NY, GA, OH, PA"},
    ],
    "DEU": [
        {"date": "2026-09-13", "type": "Berlin & Mecklenburg-Vorpommern Landtag",
         "stakes": "AfD eastern test"},
    ],
    "IND": [
        {"date": "2026-04-01", "type": "Tamil Nadu state election",
         "stakes": "DMK reelection bid; AIADMK-BJP alliance"},
        {"date": "2026-04-01", "type": "West Bengal state election",
         "stakes": "TMC vs. BJP; Mamata Banerjee third term"},
        {"date": "2027-02-01", "type": "Uttar Pradesh state election",
         "stakes": "BJP heartland; UCC implementation"},
    ],
    "GBR": [
        {"date": "2027-05-06", "type": "Local elections + Mayoral",
         "stakes": "Reform UK breakthrough test"},
    ],
}


def main() -> int:
    by_iso = {iso: items[:] for iso, items in SUPPLEMENTARY_BY_ISO.items()}
    path = write_overlay("elections_calendar", {"by_iso": by_iso})
    n = sum(len(v) for v in by_iso.values())
    print(f"elections_calendar: wrote {path} ({n} entries across {len(by_iso)} countries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

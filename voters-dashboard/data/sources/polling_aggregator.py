#!/usr/bin/env python3
"""
Polling aggregator.

Slice 2: loads `data/polls.yaml` (hand-curated time-series for priority
countries) and writes a normalised overlay to `data/cache/polling.json`.
The dashboard server reads polls.yaml directly for its endpoints; this
overlay exists so sibling dashboards (or future scrapers) can ingest the
same shape.

Slice 3 stretch goal: append scraped polls from Wikipedia opinion-polling
pages for stable countries (US, UK, DE, FR) before writing the overlay.
That replaces the curated entries with a wider, fresher set; everything
else in the pipeline stays the same.

Usage:
    python3 data/sources/polling_aggregator.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import write_overlay  # noqa: E402

POLLS_PATH = Path(__file__).resolve().parents[1] / "polls.yaml"


def main() -> int:
    if not POLLS_PATH.exists():
        print(f"polling_aggregator: {POLLS_PATH} not found, writing empty overlay", file=sys.stderr)
        write_overlay("polling", {"by_iso": {}, "_status": "no polls.yaml"})
        return 0

    with POLLS_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    by_iso: dict = {}
    total = 0
    for iso, block in (data.get("countries") or {}).items():
        polls = block.get("polls") or []
        polls.sort(key=lambda p: (p.get("date") or ""))
        if not polls:
            continue
        by_iso[iso] = {
            "election_label": block.get("election_label"),
            "polls": polls,
            "latest_date": polls[-1].get("date"),
            "poll_count": len(polls),
        }
        total += len(polls)

    payload = {
        "by_iso": by_iso,
        "schema_version": data.get("schema_version", 1),
        "last_curated": data.get("last_curated"),
    }
    path = write_overlay("polling", payload)
    print(f"polling_aggregator: wrote {path} ({len(by_iso)} countries, {total} polls)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

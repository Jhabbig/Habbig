#!/usr/bin/env python3
"""
Polling aggregator.

FiveThirtyEight retired its public polling API in 2023, so the MVP path
is a hand-curated `data/polls.yaml` of recent national polls for the
priority elections, refreshed weekly-to-monthly.

Future sources to wire (each blocked on a separate engineering task):
  - The Economist polling tracker     - JSON, but obfuscated; needs a parser
  - Wikipedia "Opinion polling for the {country} general election" pages
    - HTML wikitables; robots.txt allows scraping with a UA + delay
  - 270toWin.com aggregated data       - consistent HTML, no API
  - Politico Europe Poll of Polls      - HTML
  - RealClearPolitics                  - HTML (US-specific)

Cadence: hourly (the dashboard reloads its in-memory cache every 60s, so
running this hourly is sufficient).

This run does two things:
  1. Normalises `data/polls.yaml` into the standard overlay shape.
  2. Writes `data/cache/polling.json` + `data/snapshot_polling.yaml` so
     sibling dashboards (and the gateway) can consume the same payload
     without re-parsing polls.yaml.

The dashboard server itself reads `polls.yaml` directly for its
/api/country/{iso}/polling endpoint - the overlay is for *other* consumers.

Usage:
    python3 data/sources/polling_aggregator.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import read_existing, write_overlay  # noqa: E402

POLLS_PATH = Path(__file__).resolve().parents[1] / "polls.yaml"


def main():
    if not POLLS_PATH.exists():
        existing = read_existing("polling")
        if existing:
            print(
                "polling_aggregator: " + str(POLLS_PATH) + " missing, keeping last cache",
                file=sys.stderr,
            )
            return 1
        write_overlay(
            "polling",
            {
                "source": "curated",
                "source_url": "data/polls.yaml",
                "cadence": "hourly",
                "_status": "no polls.yaml",
                "by_iso": {},
            },
        )
        return 0

    with POLLS_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    by_iso = {}
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
        "source": "curated",
        "source_url": "data/polls.yaml",
        "cadence": "hourly",
        "schema_version": data.get("schema_version", 1),
        "last_curated": data.get("last_curated"),
        "by_iso": by_iso,
    }
    path = write_overlay("polling", payload)
    print("polling_aggregator: wrote " + str(path) + " (" + str(len(by_iso)) + " countries, " + str(total) + " polls)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

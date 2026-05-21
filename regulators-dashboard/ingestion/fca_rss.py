"""FCA news feed (UK) — config now in `sources.py`."""

from __future__ import annotations

from ._rss import fetch_source
from .sources import get

SOURCE = get("FCA")


def fetch(max_items: int = 50, since_days: int | None = 90) -> list[dict]:
    return fetch_source(SOURCE, max_items=max_items, since_days=since_days)


if __name__ == "__main__":
    import json
    import logging
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(fetch(max_items=5), indent=2)[:2000])

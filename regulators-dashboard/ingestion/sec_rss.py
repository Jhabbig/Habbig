"""SEC press-release RSS (US).

Source: https://www.sec.gov/news/pressreleases.rss

The press-releases feed covers enforcement actions, rule proposals, statements,
and personnel announcements. Litigation releases live on a separate feed —
deferred to a later version (split out as `sec_litigation.py`) so v0 stays
focused on the highest-signal items.
"""

from __future__ import annotations

from ._rss import RssSource, fetch_source

SOURCE = RssSource(
    code="SEC",
    name="U.S. Securities and Exchange Commission",
    jurisdiction="US",
    rss_url="https://www.sec.gov/news/pressreleases.rss",
)


def fetch(max_items: int = 50, since_days: int | None = 90) -> list[dict]:
    return fetch_source(SOURCE, max_items=max_items, since_days=since_days)


if __name__ == "__main__":
    import json
    import logging
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(fetch(max_items=5), indent=2)[:2000])

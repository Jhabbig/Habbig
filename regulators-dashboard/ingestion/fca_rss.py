"""FCA news RSS (UK).

Source: https://www.fca.org.uk/news/rss.xml

Covers enforcement notices, policy statements, consultations, and speeches
from the Financial Conduct Authority. The PRA (separate body, also UK)
publishes on the Bank of England website and is deferred until we wire up a
PRA-specific source.
"""

from __future__ import annotations

from ._rss import RssSource, fetch_source

SOURCE = RssSource(
    code="FCA",
    name="Financial Conduct Authority",
    jurisdiction="UK",
    rss_url="https://www.fca.org.uk/news/rss.xml",
)


def fetch(max_items: int = 50, since_days: int | None = 90) -> list[dict]:
    return fetch_source(SOURCE, max_items=max_items, since_days=since_days)


if __name__ == "__main__":
    import json
    import logging
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(fetch(max_items=5), indent=2)[:2000])

"""ESMA news RSS (EU).

Source: https://www.esma.europa.eu/press-news/esma-news/rss.xml

Covers ESMA statements, Q&As, peer reviews, and MiCA-related guidance. The
EBA and EIOPA publish separately and will land as their own modules.

Note: ESMA has changed its RSS path before. If this URL stops working, the
graceful-degradation path means the dashboard still loads with SEC + FCA
showing data — confirm the new URL on https://www.esma.europa.eu/news-publications
and update the constant here.
"""

from __future__ import annotations

from ._rss import RssSource, fetch_source

SOURCE = RssSource(
    code="ESMA",
    name="European Securities and Markets Authority",
    jurisdiction="EU",
    rss_url="https://www.esma.europa.eu/press-news/esma-news/rss.xml",
)


def fetch(max_items: int = 50, since_days: int | None = 90) -> list[dict]:
    return fetch_source(SOURCE, max_items=max_items, since_days=since_days)


if __name__ == "__main__":
    import json
    import logging
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(fetch(max_items=5), indent=2)[:2000])

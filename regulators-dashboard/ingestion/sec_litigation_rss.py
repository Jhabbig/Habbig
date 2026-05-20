"""SEC Litigation Releases — v1.3.

Pulls SEC's litigation-release feed — formal civil-case announcements
distinct from the press-release stream covered in `sec_rss.py`. Each
release corresponds to a federal-court filing (complaint, judgment,
default order, subpoena enforcement, etc.); these are the SEC's most
detailed enforcement records.

When fed through the v0.1 → v0.5 pipeline, LR items naturally classify
as `enforcement` (titles use "Charges", "Obtains Judgment", "Files
Action Against …"), pick up severity from the dollar amounts in their
summaries, get topic-tagged, and get matched to Polymarket / Kalshi
markets just like the regular SEC press-release stream.

PACER scope note:
  The original v1.3 spec called for "PACER scraper for SEC litigation
  releases — paid feed, deferred." This module delivers the FREE half:
  SEC's own LR feed gives the same headline-level signal without per-
  page PACER billing or anti-scraping headaches. Deep PACER per-case
  access (complaints, motions, exhibits) stays deferred until budget
  and a clear cost-justified use case appear.
"""

from __future__ import annotations

from ._rss import RssSource, fetch_source

SOURCE = RssSource(
    code="SEC-LIT",
    name="SEC Litigation Releases",
    jurisdiction="US",
    rss_url="https://www.sec.gov/rss/litigation/litreleases.xml",
)


def fetch(max_items: int = 50, since_days: int | None = 90) -> list[dict]:
    return fetch_source(SOURCE, max_items=max_items, since_days=since_days)


if __name__ == "__main__":
    import json
    import logging
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(fetch(max_items=5), indent=2)[:2000])

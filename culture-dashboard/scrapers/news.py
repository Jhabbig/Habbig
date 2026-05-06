"""Top headlines + lightweight sentiment.

Uses RSS feeds (no key needed). Sentiment is a tiny dictionary heuristic
— good enough for a dashboard rollup, not for a research paper.
"""

from __future__ import annotations

import logging

import feedparser
from defusedxml.ElementTree import fromstring as xml_fromstring  # noqa: F401

from models import Item
from ._http import client

NAME = "news_top"
SECTION = "news"
REFRESH_SECONDS = 30 * 60

log = logging.getLogger(__name__)

FEEDS = [
    ("BBC", "http://feeds.bbci.co.uk/news/rss.xml"),
    ("NYT", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
    ("Guardian", "https://www.theguardian.com/uk/rss"),
    ("AP", "https://feeds.apnews.com/rss/apf-topnews"),
    ("Reuters", "https://feeds.reuters.com/reuters/topNews"),
]

POSITIVE = {"win", "wins", "record", "celebrate", "love", "hit", "viral",
            "breakthrough", "launch", "praise", "rally", "soar", "best", "top"}
NEGATIVE = {"dies", "killed", "death", "war", "crash", "scandal", "fired",
            "loss", "fall", "sue", "ban", "shocking", "controversy", "outrage"}


def _sentiment(title: str) -> int:
    t = title.lower()
    return sum(1 for w in POSITIVE if w in t) - sum(1 for w in NEGATIVE if w in t)


async def fetch() -> list[Item]:
    items: list[Item] = []
    async with client() as c:
        for src, url in FEEDS:
            try:
                r = await c.get(url)
                r.raise_for_status()
            except Exception as e:  # noqa: BLE001
                log.warning("rss %s failed: %s", src, e)
                continue
            parsed = feedparser.parse(r.text)
            for e in parsed.entries[:10]:
                title = e.get("title") or "(untitled)"
                items.append(Item(
                    section=SECTION,
                    source=NAME,
                    title=title,
                    url=e.get("link"),
                    summary=(e.get("summary") or "")[:300],
                    score=float(_sentiment(title)),
                    extra={"feed": src, "published": e.get("published")},
                ))
    return items

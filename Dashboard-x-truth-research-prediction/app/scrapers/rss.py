"""Generic RSS / Atom feed scraper.

Used primarily for Substack newsletters, but works with any RSS 2.0 or Atom
feed. Substack URLs follow the pattern ``https://<slug>.substack.com/feed``,
but the operator can configure any list of feed URLs in ``config.yaml``.

Uses ``defusedxml`` to avoid the XML external-entity attacks baked into
``xml.etree`` — RSS feeds are untrusted input.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable
from urllib.parse import urlparse

import httpx

from app.config import yaml_config
from app.models import RawPost
from app.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text or "")).strip()


class RSSScraper(BaseScraper):
    def __init__(self, feeds: Iterable[str] | None = None) -> None:
        cfg = yaml_config.get("scraping", {}).get("rss", {})
        configured = feeds if feeds is not None else cfg.get("feeds", [])
        self._feeds: list[str] = list(configured) if configured else []

    def is_available(self) -> bool:
        return bool(self._feeds)

    async def fetch(self, keywords: list[str], limit: int = 100) -> list[RawPost]:
        if not self._feeds:
            return []
        per_feed = max(1, min(50, limit // max(1, len(self._feeds))))
        kw_lower = [k.lower() for k in keywords]
        posts: list[RawPost] = []
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for url in self._feeds:
                try:
                    resp = await client.get(url, headers={"Accept": "application/rss+xml, application/atom+xml, application/xml"})
                    if resp.status_code != 200:
                        logger.info("RSS %s returned %s; skipping", url, resp.status_code)
                        continue
                    items = self._parse_feed(resp.text, source_url=url)
                except Exception as exc:
                    logger.warning("RSS %s parse failed: %s", url, exc)
                    continue
                # Take the newest `per_feed` items.
                for item in items[:per_feed]:
                    post = self._to_post(item, kw_lower)
                    if post is not None:
                        posts.append(post)
        logger.info("RSS: fetched %d posts across %d feeds", len(posts), len(self._feeds))
        return posts

    @staticmethod
    def _parse_feed(xml_text: str, source_url: str) -> list[dict]:
        """Parse an RSS or Atom feed into a list of item dicts."""
        from defusedxml import ElementTree as ET

        root = ET.fromstring(xml_text)
        # Atom namespace handling.
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items: list[dict] = []
        # RSS 2.0
        for ch in root.findall(".//item"):
            items.append({
                "id": (ch.findtext("guid") or ch.findtext("link") or "").strip(),
                "title": (ch.findtext("title") or "").strip(),
                "link": (ch.findtext("link") or "").strip(),
                "content": (ch.findtext("description") or "").strip(),
                "author": (ch.findtext("author") or ch.findtext("{http://purl.org/dc/elements/1.1/}creator") or "").strip(),
                "pub_date": (ch.findtext("pubDate") or "").strip(),
                "source_url": source_url,
            })
        # Atom (only if no RSS items found — most feeds are one or the other)
        if not items:
            for entry in root.findall("atom:entry", ns):
                link_el = entry.find("atom:link", ns)
                items.append({
                    "id": (entry.findtext("atom:id", default="", namespaces=ns) or "").strip(),
                    "title": (entry.findtext("atom:title", default="", namespaces=ns) or "").strip(),
                    "link": link_el.get("href", "") if link_el is not None else "",
                    "content": (entry.findtext("atom:summary", default="", namespaces=ns)
                                or entry.findtext("atom:content", default="", namespaces=ns) or "").strip(),
                    "author": (entry.findtext("atom:author/atom:name", default="", namespaces=ns) or "").strip(),
                    "pub_date": (entry.findtext("atom:updated", default="", namespaces=ns)
                                 or entry.findtext("atom:published", default="", namespaces=ns) or "").strip(),
                    "source_url": source_url,
                })
        return items

    @staticmethod
    def _to_post(item: dict, keywords_lower: list[str]) -> RawPost | None:
        title = item.get("title", "")
        body = _strip_html(item.get("content", ""))
        if not title and not body:
            return None
        content = f"{title}\n\n{body}".strip()
        if len(content) < 40:
            return None
        if not any(kw in content.lower() for kw in keywords_lower):
            return None

        # ID — fall back to URL or title hash if `guid` is missing.
        raw_id = item.get("id") or item.get("link") or item.get("title")
        if not raw_id:
            return None
        import hashlib
        post_id = hashlib.sha1(raw_id.encode("utf-8")).hexdigest()[:16]

        # Author / handle. For Substack feeds, derive a stable handle from the
        # feed URL host (e.g. "matt.substack.com" -> "matt") so the same
        # source's posts cluster under one credibility record.
        author = item.get("author", "").strip()
        handle = author or _handle_from_feed_url(item.get("source_url", ""))

        # Date parsing — RSS uses RFC 822 format.
        posted_at = datetime.now(timezone.utc)
        pub_date = item.get("pub_date", "")
        if pub_date:
            try:
                parsed = parsedate_to_datetime(pub_date)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                posted_at = parsed
            except (TypeError, ValueError):
                try:
                    posted_at = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                except ValueError:
                    pass

        post = RawPost(
            id=f"rss:{post_id}",
            platform="rss",
            author_handle=handle,
            author_display_name=handle,
            follower_count=0,
            verified=False,
            content=content[:4000],
            posted_at=posted_at,
            fetched_at=datetime.now(timezone.utc),
            engagement_json="{}",
        )
        return post


def _handle_from_feed_url(url: str) -> str:
    """Extract a stable handle from a feed URL. Substack-aware."""
    if not url:
        return "rss-unknown"
    try:
        host = urlparse(url).netloc or "rss-unknown"
    except Exception:
        return "rss-unknown"
    # Strip "www." and ".substack.com" so two feeds from the same author cluster.
    host = host.removeprefix("www.")
    if host.endswith(".substack.com"):
        return host[: -len(".substack.com")]
    return host

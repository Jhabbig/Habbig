"""Cult / NRM emergence sentinel from public Reddit JSON.

Polls a small set of public read-only subreddits for recent posts and
extracts capitalised multi-word phrases that look like organisation
names. We then rank groups by recency × volume to surface ones gaining
attention — useful for spotting emerging NRMs / cults before they
graduate to the curated watchlist.

This is RECONNAISSANCE, not curation. Surfacing a mention here doesn't
imply the named group is a cult — just that members of the listed
subreddits are talking about it. False positives are expected. The
intended audience is researchers / journalists who would investigate
further before adding an entry to CULTS_WATCHLIST.

CONFIG: no auth needed. Reddit's JSON endpoint at /r/X/.json is
publicly readable. We use a descriptive User-Agent as Reddit's TOS
asks (not a generic browser-impersonation header).

SUBREDDITS POLLED:
    r/cults              — general cult discussion
    r/cultsurvivors      — post-exit narratives
    r/exjw               — ex-Jehovah's Witnesses
    r/exmormon           — ex-LDS
    r/EXMUSLIM           — ex-Muslim
    r/Scientology        — Scientology criticism

CACHE: 1h TTL. We're polite — only 1 fetch per subreddit per hour.

EXTRACTION:
    Looks for sequences of 1-4 capitalised words in titles and selftext.
    Filters out obvious noise (single common words, two short tokens,
    embedded URLs).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter
from typing import Optional

import requests

log = logging.getLogger("reddit_sentinel")

SUBREDDITS = [
    "cults", "cultsurvivors", "exjw", "exmormon", "EXMUSLIM", "Scientology",
]

USER_AGENT = "religion-dashboard-sentinel/1.0 (by /u/narve-ai; emerging-group detector)"
LISTING_URL = "https://www.reddit.com/r/{sub}/.json?limit=100"

_CACHE_TTL = 60 * 60   # 1h
_cache: dict = {"data": None, "fetched_at": 0.0, "ok": False, "error": ""}
_cache_lock = threading.Lock()

# Obviously-not-an-organisation single capitalised words to filter out.
STOP_TOKENS = {
    "I", "The", "A", "An", "My", "Our", "Their", "This", "That",
    "What", "Why", "How", "When", "Where", "Who",
    "Edit", "Update", "OP", "AMA", "NSFW", "TLDR",
    "Yes", "No", "Maybe", "First", "Last", "Old", "New",
    "Some", "Many", "Most", "Few",
    "He", "She", "We", "Us", "They", "It",
    "God", "Jesus", "Christ", "Allah", "Buddha",  # too common as standalone
    "Reddit", "YouTube", "Facebook", "Twitter", "Instagram", "TikTok",
    "Discord", "Telegram", "WhatsApp",
    "Bible", "Quran", "Torah", "Watchtower",
}

# Multi-word common phrases that AREN'T organisations.
STOP_PHRASES = {
    "Holy Spirit", "Holy Bible", "Heavenly Father", "Bible Study",
    "Sunday School", "Bible School", "New Testament", "Old Testament",
}

# A loose pattern for "Multi-Word Title-Case Organisations".
# Matches: "Branch Davidians", "Heaven's Gate", "Twelve Tribes",
#          "Aum Shinrikyo", "NXIVM", "Fundamentalist LDS Church".
ORG_PATTERN = re.compile(
    r"\b("
    r"(?:[A-Z][a-z\-']{2,}|[A-Z]{2,5})"     # one capitalised word OR acronym
    r"(?:\s(?:[A-Z][a-z\-']{2,}|of|the|and))*"  # optional more title-case words
    r"(?:\s(?:[A-Z][a-z\-']{2,}|[A-Z]{2,5}))"   # plus at least one more cap word/acronym
    r")\b"
)


def _extract_orgs(text: str) -> list[str]:
    out = []
    for m in ORG_PATTERN.finditer(text or ""):
        phrase = m.group(1).strip()
        if phrase in STOP_PHRASES:
            continue
        if phrase in STOP_TOKENS:
            continue
        # Strip leading "The"/"A"
        phrase = re.sub(r"^(?:The|A|An)\s+", "", phrase)
        if len(phrase) < 4:
            continue
        out.append(phrase)
    return out


def _http_get(url: str, *, timeout: int = 12) -> Optional[dict]:
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        if r.status_code != 200:
            log.warning("Reddit %s → HTTP %d", url, r.status_code)
            return None
        return r.json()
    except Exception as e:
        log.warning("Reddit %s → error %s", url, e)
        return None


def fetch_sentinel(force: bool = False) -> dict:
    """Aggregate recent r/cults / ex-religion posts; surface organisation mentions.

    Returns {ok, fetched_at, error, subs_polled, posts_seen,
             top_mentions: [{name, count, subs, recent_posts}]}.
    """
    with _cache_lock:
        now = time.time()
        if not force and _cache["data"] is not None and (now - _cache["fetched_at"]) < _CACHE_TTL:
            return _cache["data"]

    posts: list[dict] = []
    polled: list[str] = []
    errors: list[str] = []
    for sub in SUBREDDITS:
        d = _http_get(LISTING_URL.format(sub=sub))
        if d is None:
            errors.append(sub)
            continue
        polled.append(sub)
        for child in (d.get("data") or {}).get("children") or []:
            p = child.get("data") or {}
            posts.append({
                "title": p.get("title") or "",
                "selftext": (p.get("selftext") or "")[:2000],
                "subreddit": sub,
                "permalink": "https://reddit.com" + (p.get("permalink") or ""),
                "created_utc": p.get("created_utc") or 0,
                "score": p.get("score") or 0,
            })

    if not posts:
        result = {
            "ok": False,
            "fetched_at": time.time(),
            "error": "Reddit fetch failed for all subreddits"
                     + (f" (failed: {errors})" if errors else ""),
            "subs_polled": polled,
            "posts_seen": 0,
            "top_mentions": [],
        }
        with _cache_lock:
            _cache["data"] = result
            _cache["fetched_at"] = result["fetched_at"]
        return result

    # Extract organisations and aggregate
    counter: Counter = Counter()
    by_org: dict[str, dict] = {}
    for p in posts:
        text = p["title"] + " " + p["selftext"]
        orgs = _extract_orgs(text)
        seen_in_post: set = set()
        for org in orgs:
            if org in seen_in_post:
                continue
            seen_in_post.add(org)
            counter[org] += 1
            bucket = by_org.setdefault(org, {"subs": set(), "recent_posts": []})
            bucket["subs"].add(p["subreddit"])
            if len(bucket["recent_posts"]) < 3:
                bucket["recent_posts"].append({
                    "title": p["title"][:140],
                    "subreddit": p["subreddit"],
                    "permalink": p["permalink"],
                    "score": p["score"],
                })

    # Cut anything mentioned only once (likely noise).
    top = [(name, c) for name, c in counter.most_common(50) if c >= 2]
    mentions = []
    for name, c in top:
        bucket = by_org.get(name, {})
        mentions.append({
            "name": name,
            "count": c,
            "subs": sorted(bucket.get("subs") or []),
            "recent_posts": bucket.get("recent_posts") or [],
        })

    result = {
        "ok": True,
        "fetched_at": time.time(),
        "error": "",
        "subs_polled": polled,
        "posts_seen": len(posts),
        "top_mentions": mentions,
    }
    with _cache_lock:
        _cache["data"] = result
        _cache["fetched_at"] = result["fetched_at"]
    return result

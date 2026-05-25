"""Drafts outreach copy for a lead, scores intent, and rejects obvious junk.

Scoring layers, in order:
  1. Keyword match against the topic's keyword list (title weighted higher).
  2. Intent phrases — "anyone know", "looking for", "wish there was" — these
     turn a passive mention into an actual prospect.
  3. Engagement + recency bonus.
  4. Negative-phrase penalty / outright rejection (existing mentions of narve,
     scam/shill/promo accusations, NSFW). Returns -1 to mean "drop the lead".

Drafts append a per-lead referral code so conversions can be attributed
later via the existing `/?ref=` landing-page handler.
"""

from __future__ import annotations

import hashlib
import time as _t

from customer_bot.config import DashboardTopic
from customer_bot.lead import RawLead


# Phrases that turn a "talking about X" post into an actual prospect.
INTENT_PHRASES: tuple[str, ...] = (
    "anyone know", "looking for", "looking to", "wish there was",
    "any recommend", "recommendation for", "any good", "any decent",
    "alternatives to", "alternative to", "best way to", "best tool",
    "what tool", "what dashboard", "what site", "is there a",
    "does anyone", "how do you", "anyone tried", "anyone using",
    "where can i", "want to track", "how can i track",
)

# Phrases that disqualify a lead. Heavy penalty; HARD hits drop the lead.
NEGATIVE_PHRASES_SOFT: tuple[str, ...] = (
    "not financial advice", "shilling", "advertisement",
    "promo code", "referral code", "sign up bonus", "affiliate link",
)
NEGATIVE_PHRASES_HARD: tuple[str, ...] = (
    "narve.ai", "narve ai",
    "scam", "rug pull", "phishing", "pump and dump", "ponzi",
    "onlyfans", "nsfw",
)


def _ref_code(source_id: str) -> str:
    """Short deterministic ref code per lead (5 hex chars)."""
    h = hashlib.blake2s(source_id.encode("utf-8"), digest_size=4).hexdigest()
    return h[:5]


def ref_code(source_id: str) -> str:
    """Public wrapper so the runner can persist the same code we draft with."""
    return _ref_code(source_id)


def _first_name(author: str) -> str:
    a = (author or "").strip()
    if not a or a.startswith("0x") or a.startswith("u/") or "_" in a or any(c.isdigit() for c in a):
        return "hey"
    return f"hi {a}"


def _dash_url(topic: DashboardTopic, ref: str) -> str:
    sub_map = {
        "crypto": "crypto", "midterm": "midterm", "weather": "weather",
        "sports": "sports", "top_traders": "traders", "world": "world",
        "climate": "climate", "centralbank": "cb",
        "disasters": "disasters", "crypto_trackers": "trackers",
    }
    sub = sub_map.get(topic.key, topic.key)
    return f"https://{sub}.narve.ai/?ref={ref}"


def draft_for(lead: RawLead, topic: DashboardTopic) -> str:
    ref = _ref_code(lead.source_id)
    url = _dash_url(topic, ref)
    opener = _first_name(lead.author)

    if lead.source == "reddit":
        return (
            f"{opener} — saw your post and thought you'd find this useful.\n\n"
            f"{topic.pitch}\n\n"
            f"It's a personal project (narve.ai) — feel free to ignore if not relevant. "
            f"Direct link: {url}\n\n"
            f"— Julian"
        )
    if lead.source == "reddit_comment":
        return (
            f"{opener} — answering your thread question: {topic.pitch} "
            f"Built it myself: {url}. No signup needed to look around."
        )
    if lead.source == "hn":
        return (
            f"{opener}, your comment matched something I built. {topic.pitch} "
            f"Live at {url} — built it for myself, opened it up recently. "
            f"Curious what you'd want it to do that it doesn't."
        )
    if lead.source == "polymarket":
        return (
            f"Cross-reference: this address shows up trading "
            f"{topic.key.replace('_', ' ')}-related markets. If you find them "
            f"elsewhere (X, Discord) the pitch is: {topic.pitch} ({url})."
        )
    return f"{topic.pitch} — {url}"


def score_for(lead: RawLead, topic: DashboardTopic) -> int:
    """Return 0–100, or -1 if the lead should be dropped entirely."""
    lower_title = (lead.title or "").lower()
    lower_body = (lead.body or "").lower()
    combined = f"{lower_title} {lower_body}"

    if any(p in combined for p in NEGATIVE_PHRASES_HARD):
        return -1

    title_hits = sum(1 for kw in topic.keywords if kw.lower() in lower_title)
    body_hits = sum(1 for kw in topic.keywords if kw.lower() in lower_body)
    s = title_hits * 25 + body_hits * 8

    intent_title = sum(1 for p in INTENT_PHRASES if p in lower_title)
    intent_body = sum(1 for p in INTENT_PHRASES if p in lower_body)
    s += intent_title * 20 + intent_body * 10

    # Co-occurrence bonus: a lead that mentions a topic keyword AND an
    # intent phrase is the bullseye — someone explicitly asking for a
    # tool we sell. Reward the conjunction, not the sum of weak hits.
    has_kw = (title_hits + body_hits) > 0
    has_intent = (intent_title + intent_body) > 0
    if has_kw and has_intent:
        s += 20

    s += min(lead.engagement, 100) // 4
    if lead.posted_at and (_t.time() - lead.posted_at) < 86400:
        s += 15

    s -= 15 * sum(1 for p in NEGATIVE_PHRASES_SOFT if p in combined)

    return max(0, min(s, 100))

"""Drafts outreach copy for a lead.

Templates per source. Kept conservative: short, mentions narve.ai once,
declares it's a personal project rather than corporate spam, and leaves
the rest to the human reviewer to refine before sending.
"""

from __future__ import annotations

from customer_bot.config import DashboardTopic
from customer_bot.lead import RawLead


def _first_name(author: str) -> str:
    """Best-effort 'hi <name>' opener. Reddit/HN usernames are not real names,
    so we fall back to 'hey'."""
    a = (author or "").strip()
    if not a or a.startswith("0x") or a.startswith("u/") or "_" in a or any(c.isdigit() for c in a):
        return "hey"
    return f"hi {a}"


def draft_for(lead: RawLead, topic: DashboardTopic) -> str:
    opener = _first_name(lead.author)
    dash_url = f"https://{topic.key.replace('_', '')}.narve.ai" if topic.key != "top_traders" else "https://traders.narve.ai"

    if lead.source == "reddit":
        return (
            f"{opener} — saw your post and thought you'd find this useful.\n\n"
            f"{topic.pitch}\n\n"
            f"It's a personal project (narve.ai) — feel free to ignore if not relevant. "
            f"Direct link: {dash_url}\n\n"
            f"— Julian"
        )
    if lead.source == "hn":
        return (
            f"{opener}, your comment matched something I built. {topic.pitch} "
            f"Live at {dash_url} — built it for myself, opened it up recently. "
            f"Curious what you'd want it to do that it doesn't."
        )
    if lead.source == "polymarket":
        return (
            f"Cross-reference: this address shows up trading {topic.key.replace('_', ' ')}-related markets. "
            f"If you find them elsewhere (X, Discord) the pitch is: {topic.pitch} ({dash_url})."
        )
    return f"{topic.pitch} — {dash_url}"


def score_for(lead: RawLead, topic: DashboardTopic) -> int:
    """Coarse score 0-100. Higher = better. Weights:
      • keyword hits in title (worth more than body)
      • engagement (capped)
      • recency (last 24h gets a boost)
    """
    import time as _t

    lower_title = (lead.title or "").lower()
    lower_body = (lead.body or "").lower()
    title_hits = sum(1 for kw in topic.keywords if kw.lower() in lower_title)
    body_hits = sum(1 for kw in topic.keywords if kw.lower() in lower_body)

    s = title_hits * 25 + body_hits * 8
    s += min(lead.engagement, 100) // 4
    if lead.posted_at and (_t.time() - lead.posted_at) < 86400:
        s += 15
    return min(s, 100)

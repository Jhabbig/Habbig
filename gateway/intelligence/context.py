"""Context builder for the Intelligence assistant.

Inspects the user's most recent message to decide which slices of platform
data to load. Uses simple keyword matching (no vector DB needed at this scale).
The result is rendered into the system prompt under "Current context:".
"""

from __future__ import annotations

import re
import time
from typing import Any

import db


_MARKET_HINTS = re.compile(r"\b(market|polymarket|kalshi|odds|spread|price)\b", re.I)
_SOURCE_HINTS = re.compile(r"@([A-Za-z0-9_]+)|\bsource(?:s)?\b|\bhandle\b", re.I)
_TOPIC_HINTS = re.compile(r"\btopic(?:s)?\b|\bmy (?:saved )?(?:topics|searches)\b", re.I)
_WHAT_TO_BET = re.compile(r"\b(what should I bet|best bets?|highest[- ]EV|recommend)\b", re.I)
_HISTORY_HINTS = re.compile(r"\b(my history|my predictions|i viewed|earlier today)\b", re.I)
_ENV_HINTS = re.compile(
    r"\b(environment|climate|carbon|emissions?|co2|greenhouse|net[- ]zero|"
    r"paris|ipcc|emit|sustainab|renewable|fossil|coal|solar|wind|ev|electric vehicle)\b",
    re.I,
)
_INSIDER_HINTS = re.compile(
    r"\b(insider|congress|senator|representative|sec filing|form[- ]?4|"
    r"executive trad|stock act|capitol trade|campaign financ|fec|"
    r"lobbying|political trad|insider signal|insider alert)\b",
    re.I,
)
_CATEGORY_HINTS = {
    "politics": re.compile(r"\b(politic|election|primary|senate|congress|president|trump|biden)\b", re.I),
    "sports": re.compile(r"\b(sport|nba|nfl|mlb|football|basketball|soccer|world cup|playoffs?)\b", re.I),
    "crypto": re.compile(r"\b(crypto|bitcoin|btc|eth|coin|defi|fed|rate decision)\b", re.I),
    "geopolitics": re.compile(r"\b(geopolit|war|sanction|treaty|borders?|china|russia|israel|gaza)\b", re.I),
}


def _extract_handles(message: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"@([A-Za-z0-9_]+)", message)]


def _truncate(text: str, n: int = 320) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


async def build_intelligence_context(user: dict, message: str, history: list) -> dict:
    """Build a structured context dict + a rendered text block.

    Returns {"text": str, "metadata": dict} so callers can both inject the
    text and persist the metadata for auditing.
    """
    parts: list[str] = []
    metadata: dict[str, Any] = {"sections": []}

    user_id = user["user_id"]
    tier = "none"
    try:
        tier = db.get_user_subscription_tier(user_id)
    except Exception:
        pass

    parts.append(f"## User profile\nUser ID: {user_id}\nTier: {tier}")
    metadata["sections"].append("user_profile")

    # Always include the user's saved topics — small list, useful nearly every time.
    try:
        topics = db.list_topics(user_id)
    except Exception:
        topics = []
    if topics:
        topic_lines = []
        for t in topics[:10]:
            kw = t["keywords"] if "keywords" in t.keys() else "[]"
            topic_lines.append(f"- {t['name']} ({kw})")
        parts.append("## Your saved topics (Signal Search)\n" + "\n".join(topic_lines))
        metadata["sections"].append("topics")
        metadata["topics_count"] = len(topics)

    parts.append(f"## Current time\n{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")

    # Recent platform-wide predictions are useful for "best bets" / general queries.
    if _WHAT_TO_BET.search(message) or "best" in message.lower():
        try:
            preds = db.list_recent_predictions(limit=8)
        except Exception:
            preds = []
        if preds:
            lines = []
            for p in preds:
                cred = p["global_credibility"] if "global_credibility" in p.keys() else None
                cred_str = f"cred={cred:.2f}" if cred is not None else "unrated"
                lines.append(f"- @{p['source_handle']} on {p['category']}: {_truncate(p['content'], 140)} ({cred_str})")
            parts.append("## Recent high-signal predictions\n" + "\n".join(lines))
            metadata["sections"].append("recent_predictions")
            metadata["predictions_count"] = len(preds)

    # Source-specific lookups when the user mentions @handles or "source".
    handles = _extract_handles(message)
    for handle in handles[:3]:
        try:
            cred = db.get_source_credibility(handle)
        except Exception:
            cred = None
        if cred:
            parts.append(
                f"## Source profile: @{handle}\n"
                f"Global credibility: {cred['global_credibility']:.3f}\n"
                f"Total predictions: {cred['total_predictions']}\n"
                f"Correct: {cred['correct_predictions']}\n"
                f"Accuracy unlocked: {bool(cred['accuracy_unlocked'])}\n"
                f"Categories active: {cred['categories_active']}"
            )
            metadata["sections"].append(f"source:{handle}")

    # Category-specific data when the message clearly references one.
    matched_category = None
    for cat, pattern in _CATEGORY_HINTS.items():
        if pattern.search(message):
            matched_category = cat
            break
    if matched_category:
        try:
            cat_preds = db.list_recent_predictions(limit=6, category=matched_category)
        except Exception:
            cat_preds = []
        if cat_preds:
            lines = []
            for p in cat_preds:
                cred = p["global_credibility"] if "global_credibility" in p.keys() else None
                cred_str = f"cred={cred:.2f}" if cred is not None else "unrated"
                lines.append(f"- @{p['source_handle']}: {_truncate(p['content'], 160)} ({cred_str})")
            parts.append(f"## Recent {matched_category} signals\n" + "\n".join(lines))
            metadata["sections"].append(f"category:{matched_category}")

    # Topic-specific analysis when user asks about "my topics" / "Signal Search"
    if _TOPIC_HINTS.search(message) and topics:
        for t in topics[:3]:
            try:
                analysis = db.get_latest_topic_analysis(t["id"])
            except Exception:
                analysis = None
            if analysis:
                parts.append(
                    f"## Topic analysis: {t['name']}\n"
                    f"Signal: {analysis['signal_direction']}\n"
                    f"Confidence: {analysis['confidence']}\n"
                    f"Summary: {_truncate(analysis['summary'], 280)}"
                )
                metadata["sections"].append(f"topic_analysis:{t['id']}")

    # Environmental impact context when the user asks about climate/carbon/etc.
    # Reads only from the cache populated by intelligence/environmental.py —
    # never triggers Claude generation from inside the context builder.
    if _ENV_HINTS.search(message):
        try:
            env_impacts = db.list_top_environmental_impacts(limit=5)
        except Exception:
            env_impacts = []
        if env_impacts:
            lines = []
            for imp in env_impacts:
                yes_mt = imp["yes_co2_impact_mt"]
                no_mt = imp["no_co2_impact_mt"]
                yes_str = f"{yes_mt:+.2f} MT CO2" if yes_mt is not None else "—"
                no_str = f"{no_mt:+.2f} MT CO2" if no_mt is not None else "—"
                lines.append(
                    f"- {_truncate(imp['market_question'], 100)}\n"
                    f"  YES → {yes_str} ({imp['yes_impact_timeframe'] or 'unknown timeframe'})\n"
                    f"  NO  → {no_str} ({imp['no_impact_timeframe'] or 'unknown timeframe'})\n"
                    f"  Confidence: {imp['confidence'] or 'speculative'}"
                )
            parts.append("## Environmental impact context\n" + "\n".join(lines))
            metadata["sections"].append("environmental")
            metadata["env_impacts_count"] = len(env_impacts)

    # Insider trading signal context — congressional trades, SEC filings, etc.
    if _INSIDER_HINTS.search(message):
        try:
            insider_signals = db.get_insider_signals(days=30, limit=8)
        except Exception:
            insider_signals = []
        if insider_signals:
            lines = []
            for sig in insider_signals:
                amount_str = f"${sig['amount_usd']:,.0f}" if sig.get("amount_usd") else "undisclosed"
                lines.append(
                    f"- [{sig['signal_strength'].upper()}] {sig['signal_type'].replace('_', ' ').title()}: "
                    f"{sig['source_name']} {sig['action']} {sig['asset_or_entity']} ({amount_str})"
                )
                # Include correlations if available
                try:
                    corrs = db.get_insider_correlations_for_signal(sig["id"])
                    for corr in corrs[:2]:
                        lines.append(
                            f"  → Correlated: {_truncate(corr['market_question'] or '', 80)} "
                            f"(implied {corr['implied_direction']}, score: {corr['insider_score']:.2f})"
                        )
                except Exception:
                    pass
            parts.append("## Insider trading signals (last 30 days)\n" + "\n".join(lines))
            metadata["sections"].append("insider_signals")
            metadata["insider_signals_count"] = len(insider_signals)

    text = "\n\n".join(parts)
    metadata["context_length"] = len(text)
    return {"text": text, "metadata": metadata}

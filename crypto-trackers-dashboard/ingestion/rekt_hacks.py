"""Rekt News hack tracker.

Rekt curates a "leaderboard" of the biggest crypto hacks with losses,
attack vector, target protocol/chain, and a write-up URL. Their main
data is at https://rekt.news/leaderboard — RSS is at /rss but only
exposes new post titles, not the leaderboard.

We use a hand-curated baseline of the top hacks since 2022 (large,
slow-moving list — actually changes maybe twice a year when a major
exploit lands), plus the Rekt RSS for the live "what's new" feed.

A future iteration can swap in the rekt.news scraper (HTML); for v0
the curated list gives directionally-correct context with zero scrape
fragility.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from defusedxml import ElementTree as DET

from . import _cache
from ._http import get as http_get

log = logging.getLogger("ct.rekt")

RSS_URL = "https://rekt.news/rss/feed.xml"

# Top hacks since 2022 (USD losses ≥ $50M). Curated from Rekt News +
# Chainalysis annual reports. Static baseline — refresh quarterly.
TOP_HACKS = [
    {"date": "2022-03-23", "name": "Ronin Bridge",         "loss_usd": 624_000_000, "chain": "Ronin", "vector": "Validator key compromise"},
    {"date": "2022-08-02", "name": "Nomad Bridge",         "loss_usd": 190_000_000, "chain": "Multi", "vector": "Replay-vulnerability + free-for-all"},
    {"date": "2022-09-20", "name": "Wintermute",           "loss_usd": 162_000_000, "chain": "Multi", "vector": "Vanity-address compromise"},
    {"date": "2022-10-07", "name": "Binance BNB Bridge",   "loss_usd": 570_000_000, "chain": "BNB",   "vector": "Cross-chain proof forgery"},
    {"date": "2022-11-11", "name": "FTX (US user funds)",  "loss_usd": 600_000_000, "chain": "Multi", "vector": "Internal exfiltration during collapse"},
    {"date": "2023-03-13", "name": "Euler Finance",        "loss_usd": 200_000_000, "chain": "ETH",   "vector": "Donation attack (partly returned)"},
    {"date": "2023-07-30", "name": "Curve (pools)",        "loss_usd": 73_000_000,  "chain": "ETH",   "vector": "Vyper compiler reentrancy"},
    {"date": "2023-11-22", "name": "HTX / Heco Bridge",    "loss_usd": 113_000_000, "chain": "Multi", "vector": "Hot wallet compromise"},
    {"date": "2024-01-13", "name": "Anchorage / Orbit",    "loss_usd": 81_500_000,  "chain": "Multi", "vector": "Cross-chain bridge exploit"},
    {"date": "2024-02-23", "name": "PlayDapp",             "loss_usd": 290_000_000, "chain": "ETH",   "vector": "Private key compromise → mint"},
    {"date": "2024-05-31", "name": "DMM Bitcoin",          "loss_usd": 305_000_000, "chain": "BTC",   "vector": "Hot wallet compromise"},
    {"date": "2024-07-18", "name": "WazirX",               "loss_usd": 234_000_000, "chain": "ETH",   "vector": "Multisig key compromise"},
    {"date": "2024-09-26", "name": "Indodax",              "loss_usd": 20_000_000,  "chain": "Multi", "vector": "Hot wallet compromise"},
    {"date": "2025-02-21", "name": "Bybit",                "loss_usd": 1_460_000_000, "chain": "ETH", "vector": "Cold-wallet UI-spoof (DPRK)"},
    {"date": "2025-04-11", "name": "Phemex",               "loss_usd": 85_000_000,  "chain": "ETH",   "vector": "Hot wallet compromise"},
    {"date": "2025-08-03", "name": "Cetus (Sui DEX)",      "loss_usd": 200_000_000, "chain": "Sui",   "vector": "Math overflow in price function"},
]


def _parse_rss_entry(item) -> dict:
    title = (item.findtext("title") or "").strip()
    link = (item.findtext("link") or "").strip()
    pub = (item.findtext("pubDate") or "").strip()
    desc = re.sub(r"<[^>]+>", " ", (item.findtext("description") or ""))
    desc = re.sub(r"\s+", " ", desc).strip()[:300]
    return {"title": title, "url": link, "pub_date": pub, "summary": desc}


def latest_posts(limit: int = 10) -> list[dict]:
    """Most-recent Rekt.news posts via RSS."""
    hit = _cache.get(f"rekt_rss_{limit}", ttl_s=3600)
    if hit is not None:
        return hit
    r = http_get(RSS_URL, timeout=12,
                 headers={"Accept": "application/rss+xml,text/xml,application/xml"})
    if not r:
        return []
    try:
        root = DET.fromstring(r.content)
    except DET.ParseError as e:  # type: ignore[attr-defined]
        log.warning("Rekt RSS parse failed: %s", e)
        return []
    items = root.findall(".//item")
    out = [_parse_rss_entry(it) for it in items[:limit]]
    _cache.put(f"rekt_rss_{limit}", out)
    return out


def hacks_overview() -> dict:
    """Combined view: curated top hacks + RSS-driven latest posts."""
    posts = latest_posts(8)
    rows = sorted(TOP_HACKS, key=lambda r: r["loss_usd"], reverse=True)
    by_chain: dict[str, dict] = {}
    by_vector: dict[str, dict] = {}
    for r in rows:
        bc = by_chain.setdefault(r["chain"], {"chain": r["chain"], "loss_usd": 0, "hacks": 0})
        bc["loss_usd"] += r["loss_usd"]
        bc["hacks"] += 1
        bv = by_vector.setdefault(r["vector"], {"vector": r["vector"], "loss_usd": 0, "hacks": 0})
        bv["loss_usd"] += r["loss_usd"]
        bv["hacks"] += 1
    return {
        "source": "Curated 2022-2025 top hacks + Rekt News RSS",
        "as_of": "2025-09-30 baseline",
        "total_hacks_tracked": len(TOP_HACKS),
        "total_losses_usd": sum(r["loss_usd"] for r in TOP_HACKS),
        "biggest_hack": rows[0] if rows else None,
        "top_hacks": rows,
        "by_chain": sorted(by_chain.values(), key=lambda b: b["loss_usd"], reverse=True)[:8],
        "by_vector": sorted(by_vector.values(), key=lambda b: b["loss_usd"], reverse=True)[:8],
        "latest_posts": posts,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(hacks_overview(), indent=2)[:2000])

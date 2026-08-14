#!/usr/bin/env python3
"""
Ticker → Polymarket markets index.

Given a stock ticker (NVDA, AAPL, TSLA…), find Polymarket markets that
reference the same underlying. Used by `correlation.py` to join insider
events to prediction-market price moves.

Two-stage match:
  1. Explicit ticker mention in slug or question  (e.g. "...nvda...")
  2. Company-name mention via STOCK_ALIASES        (e.g. "tesla" → TSLA)

We deliberately skip pure regex over the question text because random
2–3 letter words (USD, AI, EV) collide with real tickers. The alias map
is small, hand-curated, and easy to extend.

Cached for INDEX_TTL seconds — Polymarket markets churn slowly enough
that a 30-min refresh is plenty.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
HTTP_TIMEOUT = 15.0
INDEX_TTL = 1800  # 30 min

# ─── Curated company → ticker map ─────────────────────────────────────
# Keys are lowercased aliases that appear in market questions/slugs.
# Add entries as new markets show up; this is intentionally small to
# keep precision high.
STOCK_ALIASES: dict[str, str] = {
    # Mega-caps
    "apple": "AAPL", "aapl": "AAPL",
    "microsoft": "MSFT", "msft": "MSFT",
    "nvidia": "NVDA", "nvda": "NVDA",
    "amazon": "AMZN", "amzn": "AMZN",
    "google": "GOOGL", "alphabet": "GOOGL", "googl": "GOOGL", "goog": "GOOGL",
    "meta": "META", "facebook": "META",
    "tesla": "TSLA", "tsla": "TSLA",
    "berkshire": "BRK.B",
    # Trump-adjacent / meme / political
    "trump media": "DJT", "djt": "DJT", "tmtg": "DJT",
    "gamestop": "GME", "gme": "GME",
    "amc": "AMC",
    "coinbase": "COIN", "coin": "COIN",
    "robinhood": "HOOD", "hood": "HOOD",
    "palantir": "PLTR", "pltr": "PLTR",
    "spacex": "SPACEX_PRIVATE",  # private, but markets exist
    "tiktok": "BYTEDANCE_PRIVATE",
    # Other commonly traded on PM
    "boeing": "BA", "intel": "INTC", "intc": "INTC",
    "amd": "AMD", "netflix": "NFLX", "nflx": "NFLX",
    "openai": "OPENAI_PRIVATE",
    "tsmc": "TSM", "asml": "ASML",
    "moderna": "MRNA", "mrna": "MRNA",
    "pfizer": "PFE", "pfe": "PFE",
    "eli lilly": "LLY", "lly": "LLY",
    "novo nordisk": "NVO",
    "spy": "SPY", "s&p 500": "SPY", "s&p500": "SPY",
    "nasdaq": "QQQ", "qqq": "QQQ",
    "bitcoin": "BTC", "btc": "BTC", "ethereum": "ETH", "eth": "ETH",
}

# Stop-list to keep alias matches sane (don't fire on substring "amc" inside
# "campaign", etc). We use word-boundary regex so this is mostly defensive.


# ─── In-memory cache ──────────────────────────────────────────────────
_INDEX_CACHE: dict = {"data": None, "fetched_at": 0.0}


def _safe_json_list(s: Any) -> list:
    """clobTokenIds and outcomes come back as JSON-encoded strings."""
    if isinstance(s, list):
        return s
    if isinstance(s, str):
        try:
            v = json.loads(s)
            return v if isinstance(v, list) else []
        except (ValueError, TypeError):
            return []
    return []


def _fetch_active_markets(limit: int = 500) -> list[dict]:
    """Pull active Polymarket events with their inner markets."""
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            r = client.get(
                f"{GAMMA_API}/events",
                params={"active": "true", "closed": "false", "limit": limit},
            )
            r.raise_for_status()
            events = r.json()
    except Exception as e:
        logger.warning("gamma events fetch failed: %s", e)
        return []
    out: list[dict] = []
    for ev in events:
        ev_title = ev.get("title") or ""
        ev_slug = ev.get("slug") or ""
        for m in (ev.get("markets") or []):
            out.append({
                "condition_id": m.get("conditionId") or m.get("condition_id") or "",
                "question": m.get("question") or ev_title,
                "slug": m.get("slug") or ev_slug,
                "event_title": ev_title,
                "end_date": m.get("endDate") or ev.get("endDate") or "",
                "volume": float(m.get("volumeNum") or m.get("volume") or 0),
                "outcomes": _safe_json_list(m.get("outcomes")),
                "clob_token_ids": _safe_json_list(m.get("clobTokenIds")),
            })
    return out


def _extract_tickers(text: str) -> set[str]:
    """Match aliases (case-insensitive, word-boundary) against text."""
    if not text:
        return set()
    t = text.lower()
    found: set[str] = set()
    for alias, ticker in STOCK_ALIASES.items():
        # Word boundary; '\b' doesn't fire around '.' / '&', so we DIY for
        # multi-word aliases like "trump media" or "s&p 500".
        if " " in alias or "&" in alias:
            if alias in t:
                found.add(ticker)
        else:
            if re.search(rf"\b{re.escape(alias)}\b", t):
                found.add(ticker)
    return found


def _build_index(markets: list[dict]) -> dict[str, list[dict]]:
    idx: dict[str, list[dict]] = {}
    for m in markets:
        haystack = " ".join([m.get("question", ""), m.get("slug", ""), m.get("event_title", "")])
        for ticker in _extract_tickers(haystack):
            idx.setdefault(ticker, []).append(m)
    # Sort each ticker's market list by volume desc — biggest market first
    for ticker, lst in idx.items():
        lst.sort(key=lambda mm: mm.get("volume", 0), reverse=True)
    return idx


def get_index(force_refresh: bool = False) -> dict[str, list[dict]]:
    """ticker → list of matching Polymarket markets, cached."""
    now = time.time()
    if (not force_refresh
            and _INDEX_CACHE["data"] is not None
            and (now - _INDEX_CACHE["fetched_at"]) < INDEX_TTL):
        return _INDEX_CACHE["data"]
    markets = _fetch_active_markets()
    idx = _build_index(markets)
    _INDEX_CACHE["data"] = idx
    _INDEX_CACHE["fetched_at"] = now
    logger.info("ticker_to_market index built: %d tickers, %d markets",
                len(idx), sum(len(v) for v in idx.values()))
    return idx


def markets_for_ticker(ticker: str, limit: int = 5) -> list[dict]:
    """Top-N matching markets for a ticker (by volume)."""
    if not ticker:
        return []
    idx = get_index()
    return idx.get(ticker.upper(), [])[:limit]


def index_summary() -> dict:
    idx = _INDEX_CACHE["data"] or {}
    return {
        "fetched_at": int(_INDEX_CACHE["fetched_at"]) if _INDEX_CACHE["fetched_at"] else None,
        "ticker_count": len(idx),
        "market_count": sum(len(v) for v in idx.values()),
        "top_tickers": sorted(
            [{"ticker": t, "markets": len(v)} for t, v in idx.items()],
            key=lambda x: x["markets"], reverse=True,
        )[:20],
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    idx = get_index(force_refresh=True)
    print(json.dumps(index_summary(), indent=2))
    for t in ("NVDA", "TSLA", "DJT"):
        ms = markets_for_ticker(t)
        print(f"\n{t}: {len(ms)} markets")
        for m in ms[:3]:
            print(f"  - {m['question']}  vol={m['volume']:,.0f}")

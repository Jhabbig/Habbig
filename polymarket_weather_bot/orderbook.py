"""Order-book utilities for executable-price-aware edges.

The book-walking logic is a vendored compact copy of simulate_market_buy from
polymarket-bot/polymarket_bot.py — copied, not imported, so this bot stays
self-contained.
"""

from __future__ import annotations

import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

CLOB_API = "https://clob.polymarket.com"


async def fetch_clob_book(session: aiohttp.ClientSession, token_id: str) -> Optional[dict]:
    """Fetch the CLOB order book for a token. Returns {"bids": [...], "asks": [...]}
    sorted best-first, or None on any failure."""
    if not token_id:
        return None
    try:
        async with session.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return None
            book = await resp.json()
    except Exception as e:
        logger.debug("Book fetch failed for token %s…: %s", str(token_id)[:16], e)
        return None
    bids = sorted(book.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
    asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
    return {"bids": bids, "asks": asks}


def simulate_market_buy(book: Optional[dict], spend_amount: float) -> Optional[dict]:
    """Simulate a market buy by walking the ask side of the order book.

    Returns a fill summary dict or None if nothing can be filled.
    """
    if not book:
        return None
    asks = book.get("asks", [])
    if not asks:
        return None

    remaining = spend_amount
    total_shares = 0.0
    fills = []

    for level in asks:
        price = float(level["price"])
        available = float(level["size"])
        if price <= 0 or price >= 1.0:
            continue

        max_shares_at_price = remaining / price
        filled = min(max_shares_at_price, available)

        cost = filled * price
        total_shares += filled
        remaining -= cost
        fills.append({"price": price, "shares": round(filled, 2), "cost": round(cost, 2)})

        if remaining < 0.01:
            break

    if total_shares == 0:
        return None

    spent = spend_amount - remaining
    avg_price = spent / total_shares

    return {
        "avg_price": round(avg_price, 4),
        "total_shares": round(total_shares, 2),
        "total_cost": round(spent, 2),
        "unfilled": round(remaining, 2),
        "levels_hit": len(fills),
        "fills": fills,
        "best_ask": float(asks[0]["price"]),
        "slippage": round(avg_price - float(asks[0]["price"]), 4),
    }


def book_midpoint(book: Optional[dict]) -> Optional[float]:
    """Bid/ask midpoint, or None when either side is missing or crossed nonsensically."""
    if not book or not book.get("bids") or not book.get("asks"):
        return None
    best_bid = float(book["bids"][0]["price"])
    best_ask = float(book["asks"][0]["price"])
    if best_bid <= 0 or best_ask >= 1 or best_ask <= 0 or best_bid >= 1:
        return None
    return (best_bid + best_ask) / 2.0


def synthesize_no_book(yes_book: Optional[dict]) -> Optional[dict]:
    """Derive the NO-side book from the YES book.

    In a binary market a YES bid at p is exactly a NO ask at 1-p (and vice
    versa), so a missing NO book can be reconstructed loss-free.
    """
    if not yes_book:
        return None
    asks = [{"price": str(round(1.0 - float(b["price"]), 6)), "size": b["size"]} for b in yes_book.get("bids", [])]
    bids = [{"price": str(round(1.0 - float(a["price"]), 6)), "size": a["size"]} for a in yes_book.get("asks", [])]
    return {
        "bids": sorted(bids, key=lambda x: float(x["price"]), reverse=True),
        "asks": sorted(asks, key=lambda x: float(x["price"])),
    }


def effective_buy_price(book: Optional[dict], spend_amount: float, last_price: float) -> tuple[float, str]:
    """Best-available executable price for buying one side of a market.

    Tiers, best-first: "book" (walk the asks), "midpoint" (bid/ask mid),
    "last" (caller-supplied last/quoted price).
    """
    if book:
        fill = simulate_market_buy(book, spend_amount)
        if fill:
            return fill["avg_price"], "book"
        mid = book_midpoint(book)
        if mid is not None:
            return mid, "midpoint"
    return last_price, "last"

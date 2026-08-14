"""Polymarket CLOB (Central Limit Order Book) client.

The gamma-api gives us the midpoint price; the CLOB gives us the actual order
book — bids and asks at each price level. That's what we need to compute
size-adjusted EV: "this edge is worth +0.40 at $100 stake, +0.18 at $1k, and
disappears past $5k".

This client is intentionally a read-only thin layer over the public CLOB HTTP
endpoint. We don't sign orders or place trades — paper-trading only.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"


@dataclass
class OrderBook:
    """A simple snapshot of a market's order book.

    ``bids`` and ``asks`` are lists of (price, size) tuples in price-priority
    order: bids descending (best bid first), asks ascending (best ask first).
    Sizes are in shares; prices are decimals in [0, 1].
    """
    market_token_id: str
    bids: list[tuple[float, float]]  # buy-side
    asks: list[tuple[float, float]]  # sell-side

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0


def _parse_book_side(rows: list) -> list[tuple[float, float]]:
    """CLOB sends each side as a list of dicts ``{"price": "0.42", "size": "100"}``
    (or sometimes ``[[price, size], ...]``). Normalise both shapes."""
    out: list[tuple[float, float]] = []
    for r in rows or []:
        try:
            if isinstance(r, dict):
                price = float(r.get("price", 0) or 0)
                size = float(r.get("size", 0) or 0)
            elif isinstance(r, (list, tuple)) and len(r) >= 2:
                price = float(r[0])
                size = float(r[1])
            else:
                continue
            if price > 0 and size > 0:
                out.append((price, size))
        except (TypeError, ValueError):
            continue
    return out


def avg_fill_price(book_side: list[tuple[float, float]], stake_usd: float) -> Optional[float]:
    """Walk the book to find the volume-weighted average price for a $stake_usd order.

    Returns ``None`` if the book doesn't have enough depth to fill the order.

    On Polymarket, every YES contract pays out $1 if YES wins. So buying $100
    worth of YES at average price $p means we own $100/p shares — that's the
    measure of size we care about, not "shares ordered". This walks the book
    accumulating $-spent until we've covered the full stake.
    """
    if stake_usd <= 0 or not book_side:
        return None
    remaining = stake_usd
    total_shares = 0.0
    total_spent = 0.0
    for price, size in book_side:
        if price <= 0:
            continue
        # $-value available at this level (shares × price)
        level_value = size * price
        if level_value >= remaining:
            shares_filled = remaining / price
            total_shares += shares_filled
            total_spent += remaining
            remaining = 0.0
            break
        # Take the whole level and keep going
        total_shares += size
        total_spent += level_value
        remaining -= level_value
    if remaining > 0:
        return None  # not enough depth in the book
    if total_shares <= 0:
        return None
    return total_spent / total_shares


def slippage_bps(mid_price: Optional[float], fill_price: Optional[float]) -> Optional[int]:
    """Slippage in basis points (1/100 of a percentage point) vs mid."""
    if mid_price is None or fill_price is None or mid_price <= 0:
        return None
    return int(round(10000 * (fill_price - mid_price) / mid_price))


class PolymarketCLOBClient:
    def __init__(self) -> None:
        self._gamma_base = GAMMA_BASE
        self._clob_base = CLOB_BASE
        # In-memory token_id cache. The gamma-api maps slug -> clobTokenIds is
        # stable for the life of the market, so caching avoids one HTTP hop per
        # order-book fetch.
        self._token_cache: dict[str, tuple[str, str]] = {}

    async def get_token_ids(self, market_slug: str) -> Optional[tuple[str, str]]:
        """Return (yes_token_id, no_token_id) for a market slug, or None on miss."""
        cached = self._token_cache.get(market_slug)
        if cached is not None:
            return cached
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                resp = await client.get(f"{self._gamma_base}/markets", params={"slug": market_slug, "limit": 1})
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning("CLOB token_ids fetch failed for %s: %s", market_slug, exc)
                return None
        if not data:
            return None
        raw = (data[0] if isinstance(data, list) else data).get("clobTokenIds")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return None
        if not isinstance(raw, list) or len(raw) < 2:
            return None
        yes_id, no_id = str(raw[0]), str(raw[1])
        self._token_cache[market_slug] = (yes_id, no_id)
        return yes_id, no_id

    async def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                resp = await client.get(f"{self._clob_base}/book", params={"token_id": token_id})
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning("CLOB book fetch failed for token %s: %s", token_id[:12], exc)
                return None
        # Sort bids desc, asks asc (CLOB usually returns sorted, but verify).
        bids = sorted(_parse_book_side(data.get("bids", [])), key=lambda x: -x[0])
        asks = sorted(_parse_book_side(data.get("asks", [])), key=lambda x: x[0])
        return OrderBook(market_token_id=token_id, bids=bids, asks=asks)

    async def book_for_side(self, market_slug: str, side: str) -> Optional[OrderBook]:
        """Convenience: get the order book for the YES or NO side of a market by slug."""
        tokens = await self.get_token_ids(market_slug)
        if tokens is None:
            return None
        yes_id, no_id = tokens
        token = yes_id if side.upper() == "YES" else no_id
        return await self.get_order_book(token)

"""
Normalized trading abstraction across Polymarket and Kalshi.

Gateway endpoints and background workers call this module;
it delegates to the platform-specific clients and returns
platform-agnostic data structures.

All prices are decimal 0.00–1.00.  All dollar amounts are USD.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Optional

import polymarket_client as poly
import kalshi_client as kalshi
import alpaca_client as alpaca

log = logging.getLogger("gateway.trading")

PLATFORMS = ("polymarket", "kalshi", "alpaca")


# ═══════════════════════════════════════════════════════════════════════════════
#  Normalized types
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class NormalizedPosition:
    platform: str           # "polymarket" | "kalshi"
    external_id: str        # condition_id | ticker
    token_or_side: str      # token_id | "yes" | "no"
    title: str
    qty: float              # shares / contracts held
    avg_entry_price: float  # 0–1 decimal
    current_price: float    # last mark (0 if unknown)
    unrealized_pnl: float   # USD
    realized_pnl: float     # USD
    fees_paid: float        # USD
    status: str             # "open" | "closed" | "settled"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NormalizedFill:
    platform: str
    external_id: str        # condition_id | ticker
    token_or_side: str
    side: str               # "yes" | "no"
    action: str             # "buy" | "sell"
    qty: float
    price: float            # 0–1 decimal
    fees: float             # USD (0 if unknown)
    timestamp: str          # ISO-8601 or Unix

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NormalizedBalance:
    platform: str
    available_usd: float
    total_usd: float        # available + in-position value

    def to_dict(self) -> dict:
        return asdict(self)


# ═══════════════════════════════════════════════════════════════════════════════
#  Dispatch: place_order
# ═══════════════════════════════════════════════════════════════════════════════

async def place_order(
    platform: str,
    creds: dict,
    *,
    slug: str = "",
    token_id: str = "",
    side: str = "yes",
    action: str = "buy",
    amount: float = 0.0,
    price: float = 0.0,
) -> dict:
    """Place a trade on the given platform.

    For Polymarket: pass token_id + side + action + amount + price.
    For Kalshi:     pass slug (=ticker) + side + action + amount + price.

    Returns the platform client's result dict (always has "status" key).
    """
    if platform == "polymarket":
        return await poly.place_order(creds, token_id, side, action, amount, price)
    if platform == "kalshi":
        return await kalshi.place_order(creds, slug, side, action, amount, price)
    if platform == "alpaca":
        # For stock trades: slug=symbol, side=buy/sell, amount=qty, price=limit_price (0=market)
        order_type = "limit" if price and price > 0 else "market"
        return await alpaca.place_order(
            creds, symbol=slug, qty=amount, side=action,
            order_type=order_type,
            limit_price=price if order_type == "limit" else None,
        )
    return {"status": "error", "error": f"Unknown platform: {platform}"}


# ═══════════════════════════════════════════════════════════════════════════════
#  Dispatch: get_positions  (authoritative from each platform)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_positions(platform: str, creds: dict) -> list[NormalizedPosition]:
    """Fetch the user's current positions from the platform and normalize."""
    if platform == "kalshi":
        return await _kalshi_positions(creds)
    if platform == "polymarket":
        return await _poly_positions(creds)
    if platform == "alpaca":
        return await _alpaca_positions(creds)
    return []


async def _kalshi_positions(creds: dict) -> list[NormalizedPosition]:
    data = await kalshi.get_positions(creds)
    if isinstance(data, dict) and "error" in data:
        log.warning("Kalshi positions fetch error: %s", data["error"])
        return []
    market_pos = data.get("market_positions", []) if isinstance(data, dict) else []
    out: list[NormalizedPosition] = []
    for p in market_pos:
        qty = p.get("position", 0)
        if qty == 0:
            continue
        realized = (p.get("realized_pnl") or 0) / 100.0
        exposure = (p.get("market_exposure") or 0) / 100.0
        avg_cost = (p.get("average_cost") or 0) / 100.0
        fees = (p.get("fees_paid") or 0) / 100.0
        out.append(NormalizedPosition(
            platform="kalshi",
            external_id=p.get("ticker", ""),
            token_or_side="yes" if qty > 0 else "no",
            title=p.get("market_title") or p.get("ticker", ""),
            qty=abs(qty),
            avg_entry_price=avg_cost,
            current_price=0.0,  # filled by mark-to-market worker
            unrealized_pnl=exposure,
            realized_pnl=realized,
            fees_paid=fees,
            status="open",
        ))
    return out


async def _poly_positions(creds: dict) -> list[NormalizedPosition]:
    """Derive positions from py-clob-client trade history.

    py-clob-client doesn't expose a positions endpoint directly, so we
    return the trade list as a simple proxy.  The mark-to-market worker
    will reconcile these into proper positions in user_positions later.
    """
    trades = await poly.get_trades(creds)
    if not trades:
        return []
    # Group by (token_id → net position).  py-clob-client returns trades
    # with asset, side, price, size, etc.
    aggregated: dict[str, dict] = {}
    for t in trades:
        asset = t.get("asset_id") or t.get("token_id") or ""
        if not asset:
            continue
        agg = aggregated.setdefault(asset, {
            "token_id": asset,
            "title": t.get("market", asset[:16]),
            "net_qty": 0.0,
            "cost": 0.0,
            "fills": 0,
        })
        size = float(t.get("size", 0))
        price = float(t.get("price", 0))
        if t.get("side", "").upper() == "BUY":
            agg["net_qty"] += size
            agg["cost"] += size * price
        else:
            agg["net_qty"] -= size
            agg["cost"] -= size * price
        agg["fills"] += 1

    out: list[NormalizedPosition] = []
    for asset, agg in aggregated.items():
        qty = agg["net_qty"]
        if abs(qty) < 0.001:
            continue
        avg_price = abs(agg["cost"] / qty) if qty else 0.0
        out.append(NormalizedPosition(
            platform="polymarket",
            external_id=asset,
            token_or_side=asset,  # token_id IS the side on Polymarket
            title=agg["title"],
            qty=abs(qty),
            avg_entry_price=round(avg_price, 4),
            current_price=0.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            fees_paid=0.0,
            status="open",
        ))
    return out


async def _alpaca_positions(creds: dict) -> list[NormalizedPosition]:
    raw = await alpaca.get_positions(creds)
    out: list[NormalizedPosition] = []
    for p in raw:
        qty = float(p.get("qty", 0))
        if abs(qty) < 0.0001:
            continue
        out.append(NormalizedPosition(
            platform="alpaca",
            external_id=p.get("symbol", ""),
            token_or_side=p.get("side", "long"),
            title=p.get("symbol", ""),
            qty=abs(qty),
            avg_entry_price=float(p.get("avg_entry_price", 0)),
            current_price=float(p.get("current_price", 0)),
            unrealized_pnl=float(p.get("unrealized_pl", 0)),
            realized_pnl=0.0,
            fees_paid=0.0,
            status="open",
        ))
    return out


# ═══════════════════════════════════════════════════════════════════════════════
#  Dispatch: get_fills
# ═══════════════════════════════════════════════════════════════════════════════

async def get_fills(platform: str, creds: dict, limit: int = 50) -> list[NormalizedFill]:
    if platform == "kalshi":
        return await _kalshi_fills(creds, limit)
    if platform == "polymarket":
        return await _poly_fills(creds)
    if platform == "alpaca":
        return await _alpaca_fills(creds, limit)
    return []


async def _kalshi_fills(creds: dict, limit: int) -> list[NormalizedFill]:
    raw = await kalshi.get_fills(creds, limit=limit)
    out: list[NormalizedFill] = []
    for f in raw:
        price_cents = f.get("yes_price") or f.get("no_price") or 0
        out.append(NormalizedFill(
            platform="kalshi",
            external_id=f.get("ticker", ""),
            token_or_side=f.get("side", "yes"),
            side=f.get("side", "yes"),
            action=f.get("action", "buy"),
            qty=float(f.get("count", 0)),
            price=kalshi.normalize_price(price_cents),
            fees=0.0,  # Kalshi doesn't return per-fill fees
            timestamp=f.get("created_time", ""),
        ))
    return out


async def _poly_fills(creds: dict) -> list[NormalizedFill]:
    raw = await poly.get_trades(creds)
    out: list[NormalizedFill] = []
    for t in raw:
        out.append(NormalizedFill(
            platform="polymarket",
            external_id=t.get("asset_id") or t.get("token_id") or "",
            token_or_side=t.get("asset_id") or "",
            side="yes",  # Polymarket sides are encoded as separate tokens
            action="buy" if t.get("side", "").upper() == "BUY" else "sell",
            qty=float(t.get("size", 0)),
            price=float(t.get("price", 0)),
            fees=float(t.get("fee_rate_bps", 0)) * float(t.get("size", 0)) * float(t.get("price", 0)) / 10000,
            timestamp=t.get("created_at") or t.get("timestamp") or "",
        ))
    return out


async def _alpaca_fills(creds: dict, limit: int) -> list[NormalizedFill]:
    """Alpaca orders that have been filled serve as fill history."""
    raw = await alpaca.get_orders(creds, status="closed", limit=limit)
    out: list[NormalizedFill] = []
    for o in raw:
        filled_qty = float(o.get("filled_qty") or 0)
        if filled_qty <= 0:
            continue
        out.append(NormalizedFill(
            platform="alpaca",
            external_id=o.get("symbol", ""),
            token_or_side=o.get("side", "buy"),
            side=o.get("side", "buy"),
            action=o.get("side", "buy"),
            qty=filled_qty,
            price=float(o.get("filled_avg_price") or 0),
            fees=0.0,
            timestamp=o.get("filled_at") or o.get("created_at") or "",
        ))
    return out


# ═══════════════════════════════════════════════════════════════════════════════
#  Dispatch: get_balance
# ═══════════════════════════════════════════════════════════════════════════════

async def get_balance(platform: str, creds: dict) -> NormalizedBalance:
    if platform == "kalshi":
        data = await kalshi.get_balance(creds)
        cents = data.get("balance", 0) if isinstance(data, dict) and "error" not in data else 0
        usd = cents / 100.0 if isinstance(cents, (int, float)) else 0.0
        return NormalizedBalance(platform="kalshi", available_usd=round(usd, 2), total_usd=round(usd, 2))
    if platform == "polymarket":
        data = await poly.get_balance(creds)
        # py-clob-client returns allowances; the "available" USDC is in
        # the allowance data (structure varies by version).
        if isinstance(data, dict) and "error" not in data:
            # Try common keys
            avail = float(data.get("balance", 0) or data.get("allowance", 0) or 0)
            return NormalizedBalance(platform="polymarket", available_usd=round(avail, 2), total_usd=round(avail, 2))
        return NormalizedBalance(platform="polymarket", available_usd=0.0, total_usd=0.0)
    if platform == "alpaca":
        data = await alpaca.get_balance(creds)
        if isinstance(data, dict) and "error" not in data:
            return NormalizedBalance(
                platform="alpaca",
                available_usd=round(data.get("cash", 0), 2),
                total_usd=round(data.get("portfolio_value", 0), 2),
            )
        return NormalizedBalance(platform="alpaca", available_usd=0.0, total_usd=0.0)
    return NormalizedBalance(platform=platform, available_usd=0.0, total_usd=0.0)


# ═══════════════════════════════════════════════════════════════════════════════
#  Dispatch: get_mark_price  (for mark-to-market worker)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_mark_price(platform: str, external_id: str, token_or_side: str = "yes") -> Optional[float]:
    """Fetch a live mid-price for an open position.

    Returns decimal 0–1, or None on failure.
    """
    if platform == "polymarket":
        return await poly.get_midpoint(external_id)
    if platform == "kalshi":
        book = await kalshi.get_orderbook(external_id)
        if not book:
            return None
        # Kalshi orderbook: {yes: [[price, qty], ...], no: [[price, qty], ...]}
        yes_levels = book.get("orderbook", {}).get("yes", []) or book.get("yes", [])
        no_levels = book.get("orderbook", {}).get("no", []) or book.get("no", [])
        if yes_levels:
            best_yes = kalshi.normalize_price(yes_levels[0][0]) if yes_levels[0] else None
            if best_yes is not None:
                return best_yes
        if no_levels:
            best_no = kalshi.normalize_price(no_levels[0][0]) if no_levels[0] else None
            if best_no is not None:
                return round(1.0 - best_no, 4)
        return None
    return None

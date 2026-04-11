"""Combines positions from both Polymarket and Kalshi into a unified portfolio."""

from __future__ import annotations

import logging
from typing import Optional

from .kalshi_client import KalshiClient
from .polymarket_client import PolymarketClient

log = logging.getLogger("gateway.portfolio")


async def get_combined_portfolio(
    poly_client: PolymarketClient,
    kalshi_client: KalshiClient,
    *,
    polymarket_address: Optional[str] = None,
    kalshi_token: Optional[str] = None,
) -> dict:
    """Aggregate positions from both connected accounts.

    Returns: {
        kalshi: {positions, balance, total_value},
        polymarket: {positions, balance_usdc, total_value},
        combined_total_usd: float
    }
    """
    result = {
        "kalshi": {"connected": False, "positions": [], "balance": 0.0, "total_value": 0.0},
        "polymarket": {"connected": False, "positions": [], "balance_usdc": 0.0, "total_value": 0.0},
        "combined_total_usd": 0.0,
    }

    # Kalshi positions
    if kalshi_token:
        result["kalshi"]["connected"] = True
        try:
            balance_data = await kalshi_client.get_balance(kalshi_token)
            if "error" not in balance_data:
                result["kalshi"]["balance"] = float(balance_data.get("balance", 0)) / 100.0  # cents to dollars
            else:
                result["kalshi"]["error"] = balance_data["error"]

            pos_result = await kalshi_client.get_positions(kalshi_token)
            if "error" in pos_result and pos_result["error"] == "token_expired":
                result["kalshi"]["error"] = "token_expired"
            positions = pos_result.get("positions", [])
            normalised = []
            total_val = 0.0

            def _to_float(val, default=0.0):
                try:
                    return float(val or 0)
                except (ValueError, TypeError):
                    return default

            for p in positions:
                # Kalshi has used two schemas over time:
                #   - Current: `position` (signed int, +ve = yes / -ve = no),
                #     `market_exposure` (cents, current notional value)
                #   - Legacy: `yes_count`, `no_count`
                # Support both, preferring the current one.
                position_signed = p.get("position")
                if position_signed is not None:
                    try:
                        position_signed = int(position_signed)
                    except (ValueError, TypeError):
                        position_signed = 0
                    shares = abs(position_signed)
                    side = "yes" if position_signed > 0 else "no" if position_signed < 0 else "yes"
                else:
                    yes_count = _to_float(p.get("yes_count"))
                    no_count = _to_float(p.get("no_count"))
                    shares = abs(yes_count or no_count)
                    side = "yes" if yes_count > 0 else "no"

                # Current position value in USD (cents -> dollars)
                exposure_cents = _to_float(p.get("market_exposure"))
                value_usd = exposure_cents / 100.0
                # Derived current price per share (only if we have shares)
                current_price = (value_usd / shares) if shares else 0.0

                pnl_val = _to_float(p.get("realized_pnl")) / 100.0

                # Average cost per share: Kalshi exposes `total_traded` (cents
                # spent) in some versions. Derive avg from that if present.
                total_traded = _to_float(p.get("total_traded"))
                avg_price = (total_traded / 100.0 / shares) if shares and total_traded else 0.0

                # Skip zero-share positions (closed)
                if shares == 0:
                    continue

                pos = {
                    "market_id": f"kalshi:{p.get('ticker', '')}",
                    "market_title": p.get("market_title", p.get("ticker", "")),
                    "platform": "kalshi",
                    "side": side,
                    "shares": shares,
                    "avg_price": round(avg_price, 4),
                    "current_price": round(current_price, 4),
                    "pnl": pnl_val,
                    "value": round(value_usd, 2),
                }
                total_val += pos["value"]
                normalised.append(pos)
            result["kalshi"]["positions"] = normalised
            result["kalshi"]["total_value"] = round(total_val + result["kalshi"]["balance"], 2)
        except Exception as e:
            log.error("Kalshi portfolio aggregation error: %s", e)
            result["kalshi"]["error"] = str(e)

    # Polymarket positions
    if polymarket_address:
        result["polymarket"]["connected"] = True
        try:
            positions = await poly_client.get_positions(polymarket_address)
            normalised = []
            total_val = 0.0

            def _safe_float(val, default=0.0):
                try:
                    return float(val or 0)
                except (ValueError, TypeError):
                    return default

            for p in positions:
                pos = {
                    "market_id": f"poly:{p.get('slug', p.get('conditionId', ''))}",
                    "market_title": p.get("title", p.get("question", "")),
                    "platform": "polymarket",
                    "side": p.get("outcome", "yes").lower(),
                    "shares": _safe_float(p.get("size")),
                    "avg_price": _safe_float(p.get("avgPrice")),
                    "current_price": _safe_float(p.get("currentPrice")),
                    "pnl": _safe_float(p.get("pnl")),
                    "value": _safe_float(p.get("currentValue")),
                }
                total_val += pos["value"]
                normalised.append(pos)
            result["polymarket"]["positions"] = normalised
            result["polymarket"]["total_value"] = total_val
        except Exception as e:
            log.error("Polymarket portfolio aggregation error: %s", e)
            result["polymarket"]["error"] = str(e)

    result["combined_total_usd"] = (
        result["kalshi"]["total_value"] + result["polymarket"]["total_value"]
    )

    return result


async def get_combined_orders(
    poly_client: PolymarketClient,
    kalshi_client: KalshiClient,
    *,
    polymarket_address: Optional[str] = None,
    kalshi_token: Optional[str] = None,
) -> list[dict]:
    """Get open and recent orders from both platforms."""
    orders: list[dict] = []

    if kalshi_token:
        try:
            orders_result = await kalshi_client.get_orders(kalshi_token)
            for o in orders_result.get("orders", []):
                side = (o.get("side") or "").lower()
                # Price: use the price field matching the order's side.
                # Kalshi stores both yes_price and no_price in cents.
                try:
                    if side == "no":
                        price_cents = float(o.get("no_price", 0) or 0)
                    else:
                        price_cents = float(o.get("yes_price", 0) or 0)
                except (ValueError, TypeError):
                    price_cents = 0.0
                price_usd = price_cents / 100.0

                # Amount: number of contracts requested
                try:
                    count = int(o.get("count", 0) or 0)
                except (ValueError, TypeError):
                    count = 0

                # Notional USD: contracts * price per contract
                amount_usd = count * price_usd

                orders.append({
                    "platform": "kalshi",
                    "order_id": o.get("order_id", ""),
                    "market_id": f"kalshi:{o.get('ticker', '')}",
                    "market_title": o.get("ticker", ""),
                    "side": side,
                    "count": count,
                    "amount": round(amount_usd, 2),
                    "price": round(price_usd, 4),
                    "status": o.get("status", ""),
                    "placed_at": o.get("created_time", ""),
                })
        except Exception as e:
            log.error("Kalshi orders error: %s", e)

    if polymarket_address:
        try:
            poly_orders = await poly_client.get_orders(polymarket_address)
            for o in poly_orders:
                orders.append({
                    "platform": "polymarket",
                    "order_id": o.get("id", ""),
                    "market_id": f"poly:{o.get('market', '')}",
                    "market_title": o.get("market", ""),
                    "side": o.get("side", ""),
                    "amount": float(o.get("size", 0) or 0),
                    "price": float(o.get("price", 0) or 0),
                    "status": o.get("status", ""),
                    "placed_at": o.get("created_at", ""),
                })
        except Exception as e:
            log.error("Polymarket orders error: %s", e)

    return orders

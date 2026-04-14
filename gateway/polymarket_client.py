"""
Consolidated Polymarket client for the gateway.

All dashboards should use this module (or call gateway endpoints that use it)
instead of rolling their own Gamma/CLOB wrappers.

Public (unauthenticated):
  - CLOB API:  get_book, get_price, get_midpoint, get_spread, get_market
  - Gamma API: get_markets, get_events, search_markets

Authenticated (via py-clob-client):
  - Trading:   place_order, cancel_order, cancel_all_orders
  - Portfolio:  get_open_orders, get_trades, get_balance

Prices are always in decimal 0.00–1.00.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Optional

import httpx

log = logging.getLogger("gateway.polymarket")

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

_TIMEOUT = 12
_RETRIES = 1

# ── Shared HTTP helper ────────────────────────────────────────────────────────

async def _get(base: str, path: str, params: dict | None = None) -> Optional[dict | list]:
    """Async GET with retry + 429 back-off."""
    url = f"{base}{path}"
    for attempt in range(_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 429:
                    await asyncio.sleep(1.5)
                    continue
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError as exc:
            if attempt == _RETRIES:
                log.warning("GET %s failed: %s", path, exc)
                return None
            await asyncio.sleep(0.5)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLIC — CLOB API (no auth)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_book(token_id: str) -> Optional[dict]:
    """Full order book (bids + asks) for a token."""
    return await _get(CLOB_HOST, "/book", {"token_id": token_id})


async def get_price(token_id: str, side: str = "buy") -> Optional[dict]:
    """Best price for a token on a given side."""
    return await _get(CLOB_HOST, "/price", {"token_id": token_id, "side": side})


async def get_midpoint(token_id: str) -> Optional[float]:
    """Midpoint price (0–1 decimal). Returns None on failure."""
    data = await _get(CLOB_HOST, "/midpoint", {"token_id": token_id})
    if data and "mid" in data:
        try:
            return float(data["mid"])
        except (TypeError, ValueError):
            pass
    return None


async def get_spread(token_id: str) -> Optional[dict]:
    return await _get(CLOB_HOST, "/spread", {"token_id": token_id})


async def get_market(condition_id: str) -> Optional[dict]:
    """CLOB market info by condition_id."""
    return await _get(CLOB_HOST, f"/markets/{condition_id}")


# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLIC — GAMMA API (market discovery)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_markets(
    limit: int = 100,
    offset: int = 0,
    active: bool = True,
    closed: bool = False,
    order: str = "volume24hr",
    ascending: bool = False,
    tag_slug: str | None = None,
) -> list[dict]:
    """Fetch markets from Gamma API with filters."""
    params: dict = {
        "active": str(active).lower(),
        "closed": str(closed).lower(),
        "limit": limit,
        "offset": offset,
        "order": order,
        "ascending": str(ascending).lower(),
    }
    if tag_slug:
        params["tag_slug"] = tag_slug
    data = await _get(GAMMA_HOST, "/markets", params)
    return data if isinstance(data, list) else []


async def get_events(
    limit: int = 50,
    active: bool = True,
    closed: bool = False,
    tag_slug: str | None = None,
) -> list[dict]:
    """Fetch events from Gamma API."""
    params: dict = {
        "active": str(active).lower(),
        "closed": str(closed).lower(),
        "limit": limit,
    }
    if tag_slug:
        params["tag_slug"] = tag_slug
    data = await _get(GAMMA_HOST, "/events", params)
    return data if isinstance(data, list) else []


async def search_markets(query: str, limit: int = 20) -> list[dict]:
    """Keyword search on active Gamma markets."""
    data = await _get(GAMMA_HOST, "/markets", {
        "active": "true",
        "closed": "false",
        "limit": limit,
        "_q": query,
    })
    return data if isinstance(data, list) else []


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTHENTICATED — py-clob-client helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _build_clob_client(creds: dict):
    """Construct an authenticated ClobClient from the gateway's cred dict.

    creds keys: private_key, api_key, api_secret, api_passphrase
    """
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    private_key = creds.get("private_key", "")
    if not private_key:
        raise ValueError("Missing Polymarket private key")

    client = ClobClient(host=CLOB_HOST, key=private_key, chain_id=CHAIN_ID)

    api_key = creds.get("api_key", "")
    api_secret = creds.get("api_secret", "")
    api_passphrase = creds.get("api_passphrase", "")
    if api_key and api_secret and api_passphrase:
        client.set_api_creds(ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        ))
    else:
        client.set_api_creds(client.create_or_derive_api_creds())

    return client


# ── Trading ──────────────────────────────────────────────────────────────────

async def place_order(
    creds: dict,
    token_id: str,
    side: str,
    action: str,
    amount: float,
    price: float,
) -> dict:
    """Place a GTC limit order on Polymarket.

    Returns {"status", "order_id", "fill_price", "shares"} on success,
    or {"status": "error", "error": "..."} on failure.
    Prices/sizes are rounded to Polymarket tick precision.
    """
    if not token_id:
        return {"status": "error", "error": "token_id required for Polymarket trades"}

    d_amount = Decimal(str(amount))
    d_price = Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if d_price <= 0 or d_price >= 1:
        return {"status": "error", "error": "Rounded price out of range"}

    d_shares = (d_amount / d_price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if d_shares <= 0:
        return {
            "status": "error",
            "error": f"Amount ${amount:.2f} too small for price {float(d_price):.2f}",
        }

    rounded_price = float(d_price)
    rounded_shares = float(d_shares)

    try:
        from py_clob_client.clob_types import OrderArgs, OrderType

        client = _build_clob_client(creds)
        buy_or_sell = "BUY" if action == "buy" else "SELL"

        order_args = OrderArgs(
            price=rounded_price,
            size=rounded_shares,
            side=buy_or_sell,
            token_id=token_id,
        )
        resp = await asyncio.to_thread(client.create_and_post_order, order_args, OrderType.GTC)

        if resp and resp.get("success"):
            return {
                "status": "submitted",
                "order_id": resp.get("orderID", ""),
                "fill_price": rounded_price,
                "shares": rounded_shares,
            }
        error = resp.get("errorMsg", "Order rejected") if resp else "No response"
        return {"status": "error", "error": error}

    except ImportError:
        return {"status": "error", "error": "py-clob-client not installed on server"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


# ── Portfolio reads (authenticated) ──────────────────────────────────────────

async def get_open_orders(creds: dict) -> list:
    """User's open orders via py-clob-client."""
    try:
        client = _build_clob_client(creds)
        return await asyncio.to_thread(client.get_orders) or []
    except Exception as exc:
        log.warning("get_open_orders failed: %s", exc)
        return []


async def get_trades(creds: dict) -> list:
    """User's trade / fill history."""
    try:
        client = _build_clob_client(creds)
        return await asyncio.to_thread(client.get_trades) or []
    except Exception as exc:
        log.warning("get_trades failed: %s", exc)
        return []


async def get_balance(creds: dict) -> dict:
    """Allowance / balance info for the connected wallet."""
    try:
        client = _build_clob_client(creds)
        return await asyncio.to_thread(client.get_balance_allowance) or {}
    except Exception as exc:
        log.warning("get_balance failed: %s", exc)
        return {"error": str(exc)}


async def test_connection(creds: dict) -> dict:
    """Validate that stored credentials work."""
    try:
        bal = await get_balance(creds)
        if "error" not in bal:
            return {"ok": True, "data": bal}
        return {"ok": False, "error": bal.get("error")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

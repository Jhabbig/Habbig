"""
Alpaca broker client for the gateway.

BYO-key model: each user provides their own API key + secret.
Supports paper and live accounts.

All stock prices are in USD.  Quantities are shares (fractional OK).
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

log = logging.getLogger("gateway.alpaca")

LIVE_URL = "https://api.alpaca.markets"
PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

_TIMEOUT = 15


def _headers(creds: dict) -> dict:
    return {
        "APCA-API-KEY-ID": creds["api_key"],
        "APCA-API-SECRET-KEY": creds["api_secret"],
        "Accept": "application/json",
    }


def _base(creds: dict) -> str:
    return PAPER_URL if creds.get("paper", True) else LIVE_URL


# ═══════════════════════════════════════════════════════════════════════════════
#  Account
# ═══════════════════════════════════════════════════════════════════════════════

async def get_account(creds: dict) -> dict:
    """Return Alpaca account summary."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{_base(creds)}/v2/account",
                headers=_headers(creds),
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        return {"error": str(e)}


async def test_connection(creds: dict) -> dict:
    """Verify credentials are valid."""
    acct = await get_account(creds)
    if "error" in acct:
        return {"ok": False, "error": acct["error"]}
    return {
        "ok": True,
        "account_id": acct.get("id", ""),
        "status": acct.get("status", ""),
        "paper": creds.get("paper", True),
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  Positions
# ═══════════════════════════════════════════════════════════════════════════════

async def get_positions(creds: dict) -> list[dict]:
    """Return all open positions."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{_base(creds)}/v2/positions",
                headers=_headers(creds),
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        log.warning("Alpaca positions error: %s", e)
        return []
    except Exception as e:
        log.warning("Alpaca positions error: %s", e)
        return []


# ═══════════════════════════════════════════════════════════════════════════════
#  Quotes
# ═══════════════════════════════════════════════════════════════════════════════

async def get_quote(creds: dict, symbol: str) -> dict:
    """Get latest quote for a symbol."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{DATA_URL}/v2/stocks/{symbol.upper()}/quotes/latest",
                headers=_headers(creds),
            )
            resp.raise_for_status()
            data = resp.json()
            q = data.get("quote", {})
            return {
                "symbol": symbol.upper(),
                "bid": float(q.get("bp", 0)),
                "ask": float(q.get("ap", 0)),
                "bid_size": int(q.get("bs", 0)),
                "ask_size": int(q.get("as", 0)),
                "timestamp": q.get("t", ""),
            }
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        return {"error": str(e)}


async def get_snapshot(creds: dict, symbol: str) -> dict:
    """Get latest snapshot (quote + trade + bar) for a symbol."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{DATA_URL}/v2/stocks/{symbol.upper()}/snapshot",
                headers=_headers(creds),
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
#  Orders
# ═══════════════════════════════════════════════════════════════════════════════

async def place_order(
    creds: dict,
    symbol: str,
    qty: float,
    side: str,
    order_type: str = "market",
    limit_price: Optional[float] = None,
    time_in_force: str = "day",
) -> dict:
    """Place a stock order. Returns order dict or error."""
    payload: dict = {
        "symbol": symbol.upper(),
        "qty": str(qty),
        "side": side,
        "type": order_type,
        "time_in_force": time_in_force,
    }
    if order_type == "limit" and limit_price is not None:
        payload["limit_price"] = str(round(limit_price, 2))

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{_base(creds)}/v2/orders",
                headers=_headers(creds),
                json=payload,
            )
            resp.raise_for_status()
            raw = resp.json()
            return {
                "status": "submitted",
                "order_id": raw.get("id", ""),
                "fill_price": float(raw.get("filled_avg_price") or 0),
                "shares": float(raw.get("filled_qty") or 0),
            }
    except httpx.HTTPStatusError as e:
        err_text = e.response.text[:300]
        log.warning("Alpaca order error: %s — %s", e.response.status_code, err_text)
        return {"status": "error", "error": f"Alpaca: {err_text}"}
    except Exception as e:
        log.warning("Alpaca order error: %s", e)
        return {"status": "error", "error": str(e)}


async def get_orders(creds: dict, status: str = "open", limit: int = 50) -> list[dict]:
    """List orders by status."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{_base(creds)}/v2/orders",
                headers=_headers(creds),
                params={"status": status, "limit": limit},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        log.warning("Alpaca orders list error: %s", e)
        return []


async def cancel_order(creds: dict, order_id: str) -> dict:
    """Cancel an open order."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.delete(
                f"{_base(creds)}/v2/orders/{order_id}",
                headers=_headers(creds),
            )
            resp.raise_for_status()
            return {"ok": True}
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
#  Balance
# ═══════════════════════════════════════════════════════════════════════════════

async def get_balance(creds: dict) -> dict:
    """Return cash / portfolio value."""
    acct = await get_account(creds)
    if "error" in acct:
        return acct
    return {
        "cash": float(acct.get("cash", 0)),
        "portfolio_value": float(acct.get("portfolio_value", 0)),
        "buying_power": float(acct.get("buying_power", 0)),
    }

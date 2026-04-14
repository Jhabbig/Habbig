"""
Consolidated Kalshi client for the gateway.

Auth methods (tried in order):
  1. RSA-PSS signing  — creds has "api_key" + "private_key_pem"
  2. Bearer token      — creds has "api_key" (no private_key_pem)
  3. Email/password    — creds has "email" + "password"

All prices are NORMALIZED to decimal 0.00–1.00 on output.
Conversion to/from Kalshi cents (1-99) happens at the boundary only.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Optional

import httpx

log = logging.getLogger("gateway.kalshi")

KALSHI_HOST = "https://api.elections.kalshi.com"
KALSHI_API  = "/trade-api/v2"

_TIMEOUT = 15
_PUBLIC_HEADERS = {"Accept": "application/json", "User-Agent": "Narve/1.0"}


# ═══════════════════════════════════════════════════════════════════════════════
#  Price helpers
# ═══════════════════════════════════════════════════════════════════════════════

def normalize_price(p) -> float:
    """Kalshi prices arrive as cents (1-99) or dollars (0.01-0.99). Normalize to 0-1."""
    if p is None:
        return 0.0
    try:
        v = float(p)
    except (TypeError, ValueError):
        return 0.0
    if v > 1.0:
        v = v / 100.0
    return round(v, 4)


def _to_cents(price_decimal: float) -> int:
    """Convert 0.00-1.00 decimal price to Kalshi cents 1-99."""
    return max(1, min(99, int(round(price_decimal * 100))))


# ═══════════════════════════════════════════════════════════════════════════════
#  RSA-PSS signing (for api_key + private_key_pem auth)
# ═══════════════════════════════════════════════════════════════════════════════

def _load_rsa_key(pem: str):
    from cryptography.hazmat.primitives import serialization
    pem_bytes = pem.encode() if isinstance(pem, str) else pem
    return serialization.load_pem_private_key(pem_bytes, password=None)


def _rsa_sign(private_key, timestamp_ms: str, method: str, path: str) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    msg = (timestamp_ms + method.upper() + path).encode()
    signature = private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


# ═══════════════════════════════════════════════════════════════════════════════
#  Authentication
# ═══════════════════════════════════════════════════════════════════════════════

def _make_rsa_headers(api_key: str, private_key, method: str, path: str) -> dict:
    """RSA-PSS signed headers (preferred auth method)."""
    ts = str(int(time.time() * 1000))
    sig = _rsa_sign(private_key, ts, method, path)
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "User-Agent": "Narve/1.0",
    }


class _AuthContext:
    """Resolves credentials to headers for each request."""

    def __init__(self, creds: dict):
        self.api_key = creds.get("api_key", "").strip()
        self.email = creds.get("email", "").strip()
        self.password = creds.get("password", "").strip()
        pem = creds.get("private_key_pem", "").strip()
        self._rsa_key = _load_rsa_key(pem) if pem else None
        self._bearer_token: str | None = None

    @property
    def uses_rsa(self) -> bool:
        return self._rsa_key is not None and bool(self.api_key)

    def headers_for(self, method: str, path: str) -> dict:
        """Return auth headers for a given request."""
        if self.uses_rsa:
            return _make_rsa_headers(self.api_key, self._rsa_key, method, path)
        # Bearer token (either stored api_key or session token from login)
        token = self._bearer_token or self.api_key
        if not token:
            raise ValueError("No auth credentials available — call login() first or provide api_key")
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Narve/1.0",
        }

    async def ensure_authenticated(self, client: httpx.AsyncClient) -> None:
        """If using email/password (no RSA, no api_key), perform login."""
        if self.uses_rsa or self.api_key:
            return  # already have auth
        if self._bearer_token:
            return  # already logged in
        if not (self.email and self.password):
            raise ValueError("Kalshi credentials incomplete: need api_key or email+password")
        resp = await client.post(
            f"{KALSHI_HOST}{KALSHI_API}/login",
            json={"email": self.email, "password": self.password},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if resp.status_code != 200:
            raise ValueError("Kalshi login failed")
        self._bearer_token = resp.json().get("token", "")


# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLIC — market data (no auth)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_events(
    limit: int = 200,
    status: str = "open",
    category: str | None = None,
    with_nested_markets: bool = False,
    cursor: str | None = None,
) -> dict:
    """Fetch events. Returns {"events": [...], "cursor": "..."}."""
    params: dict = {"limit": limit, "status": status}
    if category:
        params["category"] = category
    if with_nested_markets:
        params["with_nested_markets"] = "true"
    if cursor:
        params["cursor"] = cursor
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{KALSHI_HOST}{KALSHI_API}/events",
                params=params,
                headers=_PUBLIC_HEADERS,
            )
            if resp.status_code == 429:
                await asyncio.sleep(2)
                return {"events": [], "cursor": None}
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        log.warning("Kalshi get_events failed: %s", exc)
        return {"events": [], "cursor": None}


async def get_markets(
    event_ticker: str | None = None,
    limit: int = 200,
    status: str = "open",
    cursor: str | None = None,
) -> dict:
    """Fetch markets, optionally filtered by event_ticker."""
    params: dict = {"limit": limit, "status": status}
    if event_ticker:
        params["event_ticker"] = event_ticker
    if cursor:
        params["cursor"] = cursor
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{KALSHI_HOST}{KALSHI_API}/markets",
                params=params,
                headers=_PUBLIC_HEADERS,
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        log.warning("Kalshi get_markets failed: %s", exc)
        return {"markets": [], "cursor": None}


async def get_market(ticker: str) -> Optional[dict]:
    """Single market by ticker."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{KALSHI_HOST}{KALSHI_API}/markets/{ticker}",
                headers=_PUBLIC_HEADERS,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json().get("market")
    except httpx.HTTPError as exc:
        log.warning("Kalshi get_market(%s) failed: %s", ticker, exc)
        return None


async def get_orderbook(ticker: str) -> dict:
    """Order book for a ticker."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{KALSHI_HOST}{KALSHI_API}/markets/{ticker}/orderbook",
                headers=_PUBLIC_HEADERS,
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        log.warning("Kalshi get_orderbook(%s) failed: %s", ticker, exc)
        return {}


async def get_market_trades(ticker: str, limit: int = 100) -> list[dict]:
    """Public trade history for a market."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{KALSHI_HOST}{KALSHI_API}/markets/{ticker}/trades",
                params={"limit": limit},
                headers=_PUBLIC_HEADERS,
            )
            resp.raise_for_status()
            return resp.json().get("trades", [])
    except httpx.HTTPError as exc:
        log.warning("Kalshi get_market_trades(%s) failed: %s", ticker, exc)
        return []


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTHENTICATED — trading
# ═══════════════════════════════════════════════════════════════════════════════

async def place_order(
    creds: dict,
    ticker: str,
    side: str,
    action: str,
    amount: float,
    price: float,
) -> dict:
    """Place a limit order on Kalshi.

    price: decimal 0.00-1.00 (converted to cents internally).
    amount: dollar budget (contracts = amount / price).

    Returns {"status", "order_id", "fill_price", "shares"} on success.
    """
    price_cents = _to_cents(price)
    effective_price = price_cents / 100.0

    if amount < effective_price:
        return {
            "status": "error",
            "error": (
                f"Amount ${amount:.2f} is less than the contract price "
                f"${effective_price:.2f}. Increase amount or pick a lower-priced market."
            ),
        }
    contracts = int(amount / effective_price)
    if contracts < 1:
        return {"status": "error", "error": "Order rounds to 0 contracts."}

    auth = _AuthContext(creds)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            await auth.ensure_authenticated(client)
            path = f"{KALSHI_API}/portfolio/orders"
            order_body = {
                "ticker": ticker,
                "action": action,
                "side": side,
                "type": "limit",
                "count": contracts,
                ("yes_price" if side == "yes" else "no_price"): price_cents,
            }
            resp = await client.post(
                f"{KALSHI_HOST}{path}",
                json=order_body,
                headers=auth.headers_for("POST", path),
            )
            if resp.status_code in (200, 201):
                data = resp.json().get("order", resp.json())
                return {
                    "status": data.get("status", "submitted"),
                    "order_id": data.get("order_id", ""),
                    "fill_price": effective_price,
                    "shares": contracts,
                }
            error_data = {}
            if resp.headers.get("content-type", "").startswith("application/json"):
                error_data = resp.json()
            return {
                "status": "error",
                "error": error_data.get("message", error_data.get("error", f"HTTP {resp.status_code}")),
            }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTHENTICATED — portfolio reads
# ═══════════════════════════════════════════════════════════════════════════════

async def _auth_get(creds: dict, endpoint: str, params: dict | None = None) -> dict:
    """Authenticated GET to a Kalshi API endpoint."""
    auth = _AuthContext(creds)
    path = KALSHI_API + endpoint
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            await auth.ensure_authenticated(client)
            resp = await client.get(
                f"{KALSHI_HOST}{path}",
                params=params,
                headers=auth.headers_for("GET", path),
            )
            if resp.status_code >= 400:
                try:
                    err = resp.json()
                except Exception:
                    err = {"raw": resp.text[:500]}
                return {"error": err, "http_status": resp.status_code}
            return resp.json() if resp.text else {}
    except Exception as exc:
        log.warning("Kalshi GET %s failed: %s", endpoint, exc)
        return {"error": str(exc)}


async def get_balance(creds: dict) -> dict:
    """Account balance. Returns {"balance": <cents>, ...}."""
    return await _auth_get(creds, "/portfolio/balance")


async def get_positions(creds: dict) -> dict:
    """Open and settled positions.

    Returns {"market_positions": [...], "event_positions": [...]}.
    Prices in the response are raw Kalshi (cents) — use normalize_price()
    when displaying or storing.
    """
    return await _auth_get(creds, "/portfolio/positions")


async def get_fills(creds: dict, limit: int = 100) -> list[dict]:
    """Recent fills (executed trades)."""
    data = await _auth_get(creds, "/portfolio/fills", params={"limit": limit})
    return data.get("fills", []) if isinstance(data, dict) and "error" not in data else []


async def get_orders(creds: dict, status: str = "resting") -> list[dict]:
    """Open or historical orders."""
    data = await _auth_get(creds, "/portfolio/orders", params={"status": status})
    return data.get("orders", []) if isinstance(data, dict) and "error" not in data else []


async def test_connection(creds: dict) -> dict:
    """Verify credentials work."""
    result = await get_balance(creds)
    if isinstance(result, dict) and "error" not in result:
        return {"ok": True, "data": result}
    err = result.get("error") if isinstance(result, dict) else "unknown"
    return {"ok": False, "error": err}


# ═══════════════════════════════════════════════════════════════════════════════
#  Portfolio summary (high-level helper)
# ═══════════════════════════════════════════════════════════════════════════════

async def fetch_portfolio_summary(creds: dict) -> dict:
    """One-call portfolio overview: balance + positions + fills + aggregated stats.

    All dollar amounts are normalized from cents.
    """
    balance_data = await get_balance(creds)
    if isinstance(balance_data, dict) and "error" in balance_data:
        return {"ok": False, "error": balance_data.get("error")}

    positions_data = await get_positions(creds)
    fills_data = await get_fills(creds, limit=50)

    market_pos = positions_data.get("market_positions", []) if isinstance(positions_data, dict) else []
    fills = fills_data if isinstance(fills_data, list) else []

    open_positions = [p for p in market_pos if (p.get("position") or 0) != 0]
    total_realized = sum(p.get("realized_pnl", 0) for p in market_pos)
    total_exposure = sum(p.get("market_exposure", 0) for p in market_pos)
    total_fees = sum(p.get("fees_paid", 0) for p in market_pos)

    bal_cents = balance_data.get("balance", 0) if isinstance(balance_data, dict) else 0
    bal_dollars = round(bal_cents / 100.0, 2) if isinstance(bal_cents, (int, float)) else 0.0

    return {
        "ok": True,
        "balance": {
            "balance_cents": bal_cents,
            "balance_dollars": bal_dollars,
        },
        "summary": {
            "open_positions": len(open_positions),
            "realized_pnl_dollars": round(total_realized / 100.0, 2),
            "exposure_dollars": round(total_exposure / 100.0, 2),
            "fees_paid_dollars": round(total_fees / 100.0, 2),
        },
        "positions": [
            {
                "ticker": p.get("ticker"),
                "market_title": p.get("market_title") or p.get("ticker"),
                "position": p.get("position", 0),
                "side": "yes" if (p.get("position") or 0) > 0 else "no",
                "realized_pnl_dollars": round((p.get("realized_pnl") or 0) / 100.0, 2),
                "exposure_dollars": round((p.get("market_exposure") or 0) / 100.0, 2),
                "avg_cost_dollars": round((p.get("average_cost") or 0) / 100.0, 4),
                "fees_paid_dollars": round((p.get("fees_paid") or 0) / 100.0, 2),
            }
            for p in open_positions[:50]
        ],
        "fills": [
            {
                "ticker": f.get("ticker"),
                "side": f.get("side"),
                "action": f.get("action"),
                "count": f.get("count"),
                "price_dollars": normalize_price(f.get("yes_price") or f.get("no_price") or 0),
                "created_time": f.get("created_time"),
            }
            for f in fills[:30]
        ],
    }

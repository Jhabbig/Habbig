"""Polymarket API wrapper — public market data + CLOB order submission."""

from __future__ import annotations

import logging
from typing import Optional

import httpx

log = logging.getLogger("gateway.polymarket")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


class PolymarketClient:
    """Async wrapper around Polymarket's public (Gamma) and CLOB APIs."""

    def __init__(
        self,
        gamma_base: str = GAMMA_API,
        clob_base: str = CLOB_API,
        timeout: float = 15.0,
    ):
        self.gamma_base = gamma_base.rstrip("/")
        self.clob_base = clob_base.rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None
        self._timeout = timeout

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=5.0)
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── Public market data (no auth) ─────────────────────────────────────────

    async def get_markets(
        self,
        *,
        active: bool = True,
        closed: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Fetch active markets from the Gamma API, paginated."""
        client = await self._ensure_client()
        params = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
            "offset": offset,
        }
        try:
            resp = await client.get(f"{self.gamma_base}/markets", params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            log.error("Polymarket markets HTTP %d: %s", e.response.status_code, e)
            return []
        except httpx.RequestError as e:
            log.error("Polymarket markets request error: %s", e)
            return []

    async def get_all_markets(self, *, max_pages: int = 10) -> list[dict]:
        """Paginate through all active markets (up to max_pages * 100)."""
        all_markets: list[dict] = []
        for page in range(max_pages):
            batch = await self.get_markets(offset=page * 100, limit=100)
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < 100:
                break
        return all_markets

    async def get_market(self, slug: str) -> Optional[dict]:
        """Fetch a single market by slug."""
        client = await self._ensure_client()
        try:
            resp = await client.get(f"{self.gamma_base}/markets/{slug}")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError:
            return None
        except httpx.RequestError as e:
            log.error("Polymarket market detail error: %s", e)
            return None

    async def search_markets(self, query: str) -> list[dict]:
        """Search markets by query string."""
        client = await self._ensure_client()
        try:
            resp = await client.get(
                f"{self.gamma_base}/markets", params={"search": query}
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            log.error("Polymarket search error: %s", e)
            return []

    # ── Positions (public, by wallet address) ────────────────────────────────

    async def get_positions(self, wallet_address: str) -> list[dict]:
        """Fetch positions for a wallet address from the data API."""
        client = await self._ensure_client()
        try:
            resp = await client.get(
                f"{self.gamma_base}/positions",
                params={"user": wallet_address},
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            log.error("Polymarket positions error: %s", e)
            return []

    # ── CLOB order submission (pre-signed by client) ─────────────────────────

    async def submit_order(self, signed_order: dict) -> dict:
        """Submit a pre-signed order to the CLOB API.

        The order must be signed client-side with the user's wallet.
        The backend never sees private keys.
        """
        client = await self._ensure_client()
        try:
            resp = await client.post(
                f"{self.clob_base}/order", json=signed_order
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            body = e.response.text
            log.error("Polymarket CLOB order error HTTP %d: %s", e.response.status_code, body)
            return {"error": body, "status_code": e.response.status_code}
        except httpx.RequestError as e:
            log.error("Polymarket CLOB request error: %s", e)
            return {"error": str(e)}

    async def get_orders(self, wallet_address: str) -> list[dict]:
        """Get open orders for a wallet from the CLOB API."""
        client = await self._ensure_client()
        try:
            resp = await client.get(
                f"{self.clob_base}/orders",
                params={"user": wallet_address},
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            log.error("Polymarket orders error: %s", e)
            return []

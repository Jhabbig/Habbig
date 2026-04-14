"""Kalshi API client with RSA-PSS signature authentication.

Adapted from suislanchez/polymarket-kalshi-weather-bot (MIT).
Uses aiohttp to match the existing bot's HTTP library.
"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiClient:
    """Async Kalshi API client using RSA-PSS signature auth."""

    def __init__(self, api_key_id: str, private_key_path: str):
        self._api_key_id = api_key_id
        self._private_key_path = private_key_path
        self._private_key = None

    def _load_private_key(self):
        """Load RSA private key from file (lazy, cached)."""
        if self._private_key is not None:
            return self._private_key

        from cryptography.hazmat.primitives import serialization

        pem_data = Path(self._private_key_path).expanduser().read_bytes()
        self._private_key = serialization.load_pem_private_key(pem_data, password=None)
        return self._private_key

    def _sign_request(self, method: str, path: str) -> Dict[str, str]:
        """Generate auth headers for a Kalshi API request.

        Signature = RSA-PSS-sign(timestamp_ms + METHOD + path)
        where path = /trade-api/v2/... (no query params).
        """
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method.upper()}{path}"

        private_key = self._load_private_key()
        signature = private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "Content-Type": "application/json",
        }

    async def get(
        self,
        session: aiohttp.ClientSession,
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """Authenticated GET request to Kalshi API.

        Args:
            session: Shared aiohttp session from the bot's scan loop.
            path: API path after /trade-api/v2 (e.g., "/markets").
            params: Query parameters (not included in signature).
        """
        full_path = f"/trade-api/v2{path}"
        url = f"{BASE_URL}{path}"
        headers = self._sign_request("GET", full_path)

        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_markets(
        self,
        session: aiohttp.ClientSession,
        params: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """Fetch markets with optional filters."""
        return await self.get(session, "/markets", params=params)

    async def get_balance(self, session: aiohttp.ClientSession) -> dict:
        """Get portfolio balance (useful for auth test)."""
        return await self.get(session, "/portfolio/balance")

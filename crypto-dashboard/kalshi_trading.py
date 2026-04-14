#!/usr/bin/env python3
"""
Kalshi Authenticated Trading Integration

Kalshi uses RSA-PSS signed requests:
  - API key (UUID provided by Kalshi)
  - RSA private key (PEM, generated alongside the key in the Kalshi UI)

Each request signs the string `<timestamp_ms><method><path>` with RSA-PSS,
base64 encodes the result, and sends it in headers:
  KALSHI-ACCESS-KEY
  KALSHI-ACCESS-TIMESTAMP
  KALSHI-ACCESS-SIGNATURE

Credentials are encrypted at rest via the same Fernet helper used by clob_trading.
"""

import base64
import json
import logging
import time
from typing import Optional

import requests

from clob_trading import encrypt_credentials, decrypt_credentials  # reuse Fernet

log = logging.getLogger("crypto.kalshi")

KALSHI_HOST = "https://api.elections.kalshi.com"
KALSHI_API_BASE = "/trade-api/v2"


# ═══════════════════════════════════════════════════════════════════════
# CREDENTIAL HELPERS — reuse Fernet from clob_trading
# ═══════════════════════════════════════════════════════════════════════

def encrypt_kalshi_credentials(api_key: str, private_key_pem: str) -> str:
    """Encrypt a Kalshi credential pair to a Fernet token string."""
    return encrypt_credentials({
        "api_key": api_key.strip(),
        "private_key_pem": private_key_pem.strip(),
    })


def decrypt_kalshi_credentials(token: str) -> dict:
    """Decrypt a Fernet token back to a Kalshi credentials dict."""
    return decrypt_credentials(token)


# ═══════════════════════════════════════════════════════════════════════
# RSA SIGNING
# ═══════════════════════════════════════════════════════════════════════

def _load_rsa_key(pem: str):
    """Load an RSA private key from a PEM string."""
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:
        raise RuntimeError(
            "The 'cryptography' package is required for Kalshi auth"
        ) from exc

    pem_bytes = pem.encode() if isinstance(pem, str) else pem
    return serialization.load_pem_private_key(pem_bytes, password=None)


def _sign_request(private_key, timestamp_ms: str, method: str, path: str) -> str:
    """Sign a Kalshi request using RSA-PSS with SHA256 (as Kalshi requires)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    # Kalshi signs <timestamp><METHOD><path> — no body, no query string
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


# ═══════════════════════════════════════════════════════════════════════
# AUTHENTICATED CLIENT
# ═══════════════════════════════════════════════════════════════════════

class KalshiClient:
    """Authenticated Kalshi REST client with RSA-PSS request signing."""

    def __init__(self, api_key: str, private_key_pem: str):
        self.api_key = api_key.strip()
        try:
            self.private_key = _load_rsa_key(private_key_pem)
        except Exception as e:
            raise RuntimeError(f"Invalid Kalshi private key: {e}") from e

    def _headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        sig = _sign_request(self.private_key, ts, method, path)
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "User-Agent": "CryptoEdge/1.0",
        }

    def _request(self, method: str, endpoint: str,
                 params: dict = None, body: dict = None) -> Optional[dict]:
        path = KALSHI_API_BASE + endpoint
        url = KALSHI_HOST + path
        headers = self._headers(method, path)
        try:
            resp = requests.request(
                method, url,
                headers=headers,
                params=params,
                data=json.dumps(body) if body is not None else None,
                timeout=15,
            )
            if resp.status_code >= 400:
                # Surface Kalshi's error body to the caller
                try:
                    err = resp.json()
                except Exception:
                    err = {"raw": resp.text[:500]}
                return {"error": err, "status": resp.status_code}
            return resp.json() if resp.text else {}
        except requests.RequestException as e:
            log.warning("Kalshi %s %s failed: %s", method, endpoint, e)
            return {"error": str(e)}

    # ─── Account ────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        """Fetch portfolio balance."""
        return self._request("GET", "/portfolio/balance") or {}

    def get_portfolio(self) -> dict:
        """Fetch positions across markets."""
        return self._request("GET", "/portfolio/positions") or {}

    def get_fills(self, limit: int = 50) -> dict:
        """Fetch recent fills."""
        return self._request("GET", "/portfolio/fills", params={"limit": limit}) or {}

    def get_orders(self, status: str = "resting") -> dict:
        """Fetch user's orders by status (resting/canceled/executed)."""
        return self._request("GET", "/portfolio/orders", params={"status": status}) or {}

    # ─── Trading ────────────────────────────────────────────────────

    def place_order(self, ticker: str, side: str, action: str,
                    count: int, order_type: str = "market",
                    yes_price: int = None, no_price: int = None,
                    client_order_id: str = None) -> dict:
        """Place a Kalshi order.

        Args:
            ticker: Market ticker (e.g. "PRES-2024-DJT")
            side: "yes" or "no"
            action: "buy" or "sell"
            count: Number of contracts
            order_type: "market" or "limit"
            yes_price: Limit price in cents (1-99) for yes-side orders
            no_price:  Limit price in cents (1-99) for no-side orders
            client_order_id: Optional client-side ID for idempotency
        """
        body = {
            "ticker": ticker,
            "side": side.lower(),
            "action": action.lower(),
            "count": int(count),
            "type": order_type.lower(),
        }
        if order_type.lower() == "limit":
            if side.lower() == "yes" and yes_price is not None:
                body["yes_price"] = int(yes_price)
            elif side.lower() == "no" and no_price is not None:
                body["no_price"] = int(no_price)
            else:
                return {"error": "limit orders require yes_price or no_price (cents)"}
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self._request("POST", "/portfolio/orders", body=body) or {}

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a resting order."""
        return self._request("DELETE", f"/portfolio/orders/{order_id}") or {}

    # ─── Diagnostics ────────────────────────────────────────────────

    def test_connection(self) -> dict:
        """Quick auth test — fetch the balance."""
        result = self.get_balance()
        if isinstance(result, dict) and "error" not in result:
            return {"ok": True, "data": result}
        return {"ok": False, "error": result.get("error") if isinstance(result, dict) else "unknown"}

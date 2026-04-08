#!/usr/bin/env python3
"""
Polymarket CLOB Trading Integration

Read-only access uses direct REST calls (no auth).
Trading uses py-clob-client SDK for EIP-712 order signing.
Credentials are encrypted at rest with Fernet (AES-128-CBC + HMAC).
"""

import base64
import hashlib
import json
import time
import logging
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("crypto.clob")

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

_SECRET_KEY_PATH = Path(__file__).parent / ".secret_key"


# ═══════════════════════════════════════════════════════════════════════
# CREDENTIAL ENCRYPTION
# ═══════════════════════════════════════════════════════════════════════

def _get_fernet_key() -> bytes:
    """Derive a Fernet key from the server's .secret_key file."""
    if not _SECRET_KEY_PATH.exists():
        raise RuntimeError("Server secret key not found")
    raw = _SECRET_KEY_PATH.read_bytes().strip()
    # Fernet needs 32 bytes URL-safe base64-encoded
    dk = hashlib.sha256(raw).digest()
    return base64.urlsafe_b64encode(dk)


def encrypt_credentials(data: dict) -> str:
    """Encrypt a credentials dict to a Fernet token string."""
    from cryptography.fernet import Fernet
    f = Fernet(_get_fernet_key())
    return f.encrypt(json.dumps(data).encode()).decode()


def decrypt_credentials(token: str) -> dict:
    """Decrypt a Fernet token string back to a credentials dict."""
    from cryptography.fernet import Fernet
    f = Fernet(_get_fernet_key())
    return json.loads(f.decrypt(token.encode()).decode())


# ═══════════════════════════════════════════════════════════════════════
# READ-ONLY CLOB API (no auth needed)
# ═══════════════════════════════════════════════════════════════════════

def _clob_get(path: str, params: dict = None, retries: int = 2) -> Optional[dict]:
    """GET request to the CLOB API with retry."""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(f"{CLOB_HOST}{path}", params=params, timeout=10)
            if resp.status_code == 429:
                time.sleep(1)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            if attempt == retries:
                log.warning("CLOB GET %s failed: %s", path, e)
                return None
            time.sleep(0.5)
    return None


def get_order_book(token_id: str) -> Optional[dict]:
    """Get the full order book for a token (bids + asks)."""
    return _clob_get("/book", {"token_id": token_id})


def get_price(token_id: str, side: str = "buy") -> Optional[dict]:
    """Get best price for a token on a given side."""
    return _clob_get("/price", {"token_id": token_id, "side": side})


def get_midpoint(token_id: str) -> Optional[dict]:
    """Get midpoint price for a token."""
    return _clob_get("/midpoint", {"token_id": token_id})


def get_spread(token_id: str) -> Optional[dict]:
    """Get bid-ask spread for a token."""
    return _clob_get("/spread", {"token_id": token_id})


def get_clob_market(condition_id: str) -> Optional[dict]:
    """Get market info from CLOB by condition ID."""
    return _clob_get(f"/markets/{condition_id}")


# ═══════════════════════════════════════════════════════════════════════
# GAMMA API (market discovery — already used by suspicious_trades.py)
# ═══════════════════════════════════════════════════════════════════════

def get_markets(limit: int = 100, offset: int = 0,
                active: bool = True, closed: bool = False,
                order: str = "volume24hr",
                ascending: bool = False) -> list:
    """Fetch markets from the Gamma API with filters."""
    try:
        resp = requests.get(f"{GAMMA_HOST}/markets", params={
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
            "offset": offset,
            "order": order,
            "ascending": str(ascending).lower(),
        }, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        log.warning("Gamma markets fetch failed: %s", e)
        return []


def get_events(limit: int = 50, active: bool = True, closed: bool = False) -> list:
    """Fetch events from the Gamma API."""
    try:
        resp = requests.get(f"{GAMMA_HOST}/events", params={
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
        }, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        log.warning("Gamma events fetch failed: %s", e)
        return []


def get_event_by_slug(slug: str) -> Optional[dict]:
    """Fetch a single event by slug."""
    try:
        resp = requests.get(f"{GAMMA_HOST}/events", params={"slug": slug}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data[0] if data else None
    except requests.RequestException as e:
        log.warning("Gamma event fetch failed: %s", e)
        return None


def search_markets(query: str, limit: int = 20) -> list:
    """Search markets by keyword."""
    try:
        resp = requests.get(f"{GAMMA_HOST}/markets", params={
            "active": "true",
            "closed": "false",
            "limit": limit,
            "_q": query,
        }, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        log.warning("Market search failed: %s", e)
        return []


# ═══════════════════════════════════════════════════════════════════════
# AUTHENTICATED TRADING (requires py-clob-client)
# ═══════════════════════════════════════════════════════════════════════

class ClobTrader:
    """Authenticated CLOB trading via py-clob-client SDK."""

    def __init__(self, api_key: str, api_secret: str, api_passphrase: str,
                 private_key: str):
        try:
            from py_clob_client.client import ClobClient
        except ImportError:
            raise RuntimeError(
                "py-clob-client not installed. "
                "Run: pip install py-clob-client"
            )
        self.client = ClobClient(
            host=CLOB_HOST,
            key=api_key,
            chain_id=CHAIN_ID,
            funder=private_key,
            signature_type=2,  # POLY_GNOSIS_SAFE
        )
        # Set API credentials directly
        from py_clob_client.clob_types import ApiCreds
        self.client.set_api_creds(ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        ))
        log.info("ClobTrader initialized")

    def get_balance(self) -> dict:
        """Get allowances / balance info."""
        try:
            return self.client.get_balance_allowance()
        except Exception as e:
            log.warning("Balance fetch failed: %s", e)
            return {"error": str(e)}

    def place_market_buy(self, token_id: str, amount: float) -> dict:
        """Place a market buy order (Fill or Kill)."""
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        try:
            order = self.client.create_market_order(
                MarketOrderArgs(token_id=token_id, amount=amount)
            )
            resp = self.client.post_order(order, order_type=OrderType.FOK)
            log.info("Market buy placed: %s USDC on %s", amount, token_id[:12])
            return resp
        except Exception as e:
            log.warning("Market buy failed: %s", e)
            return {"error": str(e)}

    def place_limit_order(self, token_id: str, price: float,
                          size: float, side: str) -> dict:
        """Place a limit order (Good Till Cancelled)."""
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL
        order_side = BUY if side.lower() == "buy" else SELL
        try:
            order = self.client.create_order(
                OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=order_side,
                )
            )
            resp = self.client.post_order(order, order_type=OrderType.GTC)
            log.info("Limit %s placed: %.2f @ %.4f on %s",
                     side, size, price, token_id[:12])
            return resp
        except Exception as e:
            log.warning("Limit order failed: %s", e)
            return {"error": str(e)}

    def get_open_orders(self) -> list:
        """Get user's open orders."""
        try:
            return self.client.get_orders()
        except Exception as e:
            log.warning("Open orders fetch failed: %s", e)
            return []

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order."""
        try:
            return self.client.cancel(order_id)
        except Exception as e:
            log.warning("Cancel order failed: %s", e)
            return {"error": str(e)}

    def cancel_all_orders(self) -> dict:
        """Cancel all open orders."""
        try:
            return self.client.cancel_all()
        except Exception as e:
            log.warning("Cancel all failed: %s", e)
            return {"error": str(e)}

    def get_trades(self) -> list:
        """Get user's trade history."""
        try:
            return self.client.get_trades()
        except Exception as e:
            log.warning("Trades fetch failed: %s", e)
            return []

    def test_connection(self) -> dict:
        """Test that credentials are valid."""
        try:
            result = self.client.get_balance_allowance()
            return {"ok": True, "data": result}
        except Exception as e:
            return {"ok": False, "error": str(e)}

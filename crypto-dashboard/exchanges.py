#!/usr/bin/env python3
"""
Spot exchange adapters for auto-execution.

Two adapters behind a common interface:
  - Coinbase Advanced Trade (modern API, JWT/ES256-signed requests)
  - Kraken (HMAC-SHA512-signed requests)

Both run with **READ-ONLY + TRADE** keys only — never withdraw-enabled. The
test_connection() probe verifies credentials work; users are responsible for
locking down API key scopes on the exchange side.

Credentials are encrypted at rest via the existing Fernet helper in
clob_trading. We never log decrypted keys; the adapter cache below holds
short-lived per-request clients.

This module is pure transport + auth. Order construction (size USD →
quantity, limit price selection, retries) lives in execution.py so the
safety rails wrap every order placement.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional, Protocol

import requests

from clob_trading import encrypt_credentials, decrypt_credentials  # reuse Fernet
import database as db

log = logging.getLogger("crypto.exchanges")


# ─── Common interface ───────────────────────────────────────────────────────

@dataclass
class Balance:
    asset: str          # ticker, e.g. "BTC" or "USD"
    available: float
    total: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Fill:
    """A normalised view of one historical fill, returned by
    `get_filled_orders()`. Asset codes are already mapped to our
    canonical BTC/ETH/SOL/DOGE/XRP keys."""
    external_id: str          # exchange-side unique trade id
    ticker: str               # base asset (BTC, ETH, ...)
    side: str                 # buy | sell
    qty: float                # base-asset quantity
    price: float              # USD per unit at fill
    fee_usd: float            # commission in USD (signed positive)
    filled_at: str            # ISO datetime
    raw: dict                 # original payload for debugging

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OrderResponse:
    ok: bool
    order_id: Optional[str]
    raw: dict
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class ExchangeAdapter(Protocol):
    name: str

    def supported_assets(self) -> list[str]: ...
    def test_connection(self) -> tuple[bool, str]: ...
    def get_balances(self) -> list[Balance]: ...
    def get_price(self, ticker: str) -> Optional[float]: ...
    def place_limit_buy(
        self, ticker: str, usd_amount: float, limit_price: float, client_order_id: str,
    ) -> OrderResponse: ...
    def place_market_buy(
        self, ticker: str, usd_amount: float, client_order_id: str,
    ) -> OrderResponse: ...
    def place_limit_sell(
        self, ticker: str, base_qty: float, limit_price: float, client_order_id: str,
    ) -> OrderResponse: ...
    def place_market_sell(
        self, ticker: str, base_qty: float, client_order_id: str,
    ) -> OrderResponse: ...
    def cancel_order(self, order_id: str) -> OrderResponse: ...
    def get_order_status(self, order_id: str) -> dict: ...
    def get_filled_orders(self, since_iso: str | None = None) -> list[Fill]: ...


# ─── Credential storage ─────────────────────────────────────────────────────

# Schema for the encrypted blob, per exchange:
#   coinbase: {"api_key": "<uuid>", "private_key_pem": "<EC PEM>"}
#   kraken:   {"api_key": "<key>", "secret": "<base64 secret>"}

def save_exchange_credentials(user_id: str, exchange: str, payload: dict) -> None:
    """Encrypt and persist a credentials blob for one user × exchange."""
    if exchange not in ("coinbase", "kraken"):
        raise ValueError(f"unsupported exchange: {exchange}")
    token = encrypt_credentials(payload)
    db.upsert_exchange_credentials(user_id, exchange, token)


def load_exchange_credentials(user_id: str, exchange: str) -> Optional[dict]:
    token = db.get_exchange_credentials(user_id, exchange)
    if not token:
        return None
    try:
        return decrypt_credentials(token)
    except Exception as e:
        log.warning("decrypt failed for %s/%s: %s", user_id, exchange, e)
        return None


def delete_exchange_credentials(user_id: str, exchange: str) -> None:
    db.delete_exchange_credentials(user_id, exchange)


# ─── Coinbase Advanced Trade ────────────────────────────────────────────────

COINBASE_BASE = "https://api.coinbase.com"
COINBASE_BROKERAGE = "/api/v3/brokerage"

# Tickers we support on Coinbase. Coinbase uses "{BASE}-USD" product ids.
COINBASE_PRODUCTS = {
    "BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD",
    "DOGE": "DOGE-USD", "XRP": "XRP-USD",
}


def _coinbase_jwt(key_id: str, private_key_pem: str, method: str, path: str) -> str:
    """Build a short-lived ES256 JWT for Coinbase Advanced Trade.

    Header: {alg: ES256, kid: <api key uuid>, nonce: <random>, typ: JWT}
    Claims: {sub: <api key uuid>, iss: "cdp", nbf: now, exp: now+120,
             uri: "METHOD api.coinbase.com<path>"}
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    try:
        private_key = serialization.load_pem_private_key(
            private_key_pem.encode(), password=None
        )
    except Exception as e:
        raise RuntimeError(f"Invalid Coinbase EC private key: {e}") from e
    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise RuntimeError("Coinbase key must be an EC private key (ES256)")

    import secrets
    nonce = secrets.token_hex(16)
    header = {"alg": "ES256", "kid": key_id, "nonce": nonce, "typ": "JWT"}
    now = int(time.time())
    claims = {
        "sub": key_id, "iss": "cdp",
        "nbf": now, "exp": now + 120,
        "uri": f"{method.upper()} api.coinbase.com{path}",
    }

    def _b64(d: dict) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(d, separators=(",", ":")).encode()
        ).rstrip(b"=").decode()

    signing_input = f"{_b64(header)}.{_b64(claims)}".encode()
    der_sig = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    sig = base64.urlsafe_b64encode(raw_sig).rstrip(b"=").decode()
    return f"{signing_input.decode()}.{sig}"


class CoinbaseAdapter:
    name = "coinbase"

    def __init__(self, api_key: str, private_key_pem: str):
        self.api_key = api_key.strip()
        self.private_key_pem = private_key_pem  # cached as string; re-parsed per call
        # Validate up-front so the rest of the methods can assume good input.
        _coinbase_jwt(self.api_key, self.private_key_pem, "GET", "/api/v3/brokerage/accounts")

    def supported_assets(self) -> list[str]:
        return list(COINBASE_PRODUCTS.keys())

    def _request(self, method: str, endpoint: str, body: dict | None = None,
                 params: dict | None = None) -> dict:
        path = COINBASE_BROKERAGE + endpoint
        if params:
            path = path + "?" + urllib.parse.urlencode(params)
        token = _coinbase_jwt(self.api_key, self.private_key_pem, method, path)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "CryptoEdge/1.0",
        }
        url = COINBASE_BASE + path
        try:
            r = requests.request(
                method, url, headers=headers,
                data=json.dumps(body) if body is not None else None,
                timeout=15,
            )
            if r.status_code >= 400:
                try:
                    err = r.json()
                except ValueError:
                    err = {"raw": r.text[:500]}
                return {"_error": err, "_status": r.status_code}
            return r.json() if r.text else {}
        except requests.RequestException as e:
            return {"_error": str(e)}

    def test_connection(self) -> tuple[bool, str]:
        r = self._request("GET", "/accounts", params={"limit": 1})
        if "_error" in r:
            return False, f"Coinbase auth failed: {json.dumps(r.get('_error'))[:200]}"
        return True, f"Coinbase OK ({len(r.get('accounts', []))} accounts visible)"

    def get_balances(self) -> list[Balance]:
        out: list[Balance] = []
        cursor = None
        for _ in range(5):  # 5 pages = up to 1250 accounts
            params = {"limit": 250}
            if cursor:
                params["cursor"] = cursor
            r = self._request("GET", "/accounts", params=params)
            if "_error" in r:
                break
            for a in r.get("accounts", []):
                try:
                    avail = float(a["available_balance"]["value"])
                    held = float(a.get("hold", {}).get("value", "0"))
                    ccy = a["currency"]
                except (KeyError, ValueError, TypeError):
                    continue
                out.append(Balance(asset=ccy, available=avail, total=avail + held))
            cursor = r.get("cursor")
            if not cursor:
                break
        return out

    def get_price(self, ticker: str) -> Optional[float]:
        product = COINBASE_PRODUCTS.get(ticker.upper())
        if not product:
            return None
        # Public endpoint — no auth needed, but use the brokerage path for consistency.
        try:
            r = requests.get(f"{COINBASE_BASE}/api/v3/brokerage/market/products/{product}", timeout=10)
            r.raise_for_status()
            data = r.json()
            return float(data.get("price", 0)) or None
        except (requests.RequestException, ValueError, TypeError):
            return None

    def place_limit_buy(self, ticker: str, usd_amount: float, limit_price: float,
                        client_order_id: str) -> OrderResponse:
        product = COINBASE_PRODUCTS.get(ticker.upper())
        if not product:
            return OrderResponse(False, None, {}, "unsupported product")
        if limit_price <= 0 or usd_amount <= 0:
            return OrderResponse(False, None, {}, "invalid price/amount")
        # Coinbase expects base_size (in base asset units). qty = usd / limit.
        base_size = usd_amount / limit_price
        # Coinbase Advanced Trade product-spec precision varies; round to 8 dp
        # which fits all supported assets here.
        body = {
            "client_order_id": client_order_id,
            "product_id": product,
            "side": "BUY",
            "order_configuration": {
                "limit_limit_gtc": {
                    "base_size": f"{base_size:.8f}",
                    "limit_price": f"{limit_price:.2f}",
                    "post_only": False,
                },
            },
        }
        r = self._request("POST", "/orders", body=body)
        if "_error" in r:
            return OrderResponse(False, None, r, f"Coinbase rejected: {r.get('_error')}")
        if not r.get("success", False):
            err = r.get("error_response", {}).get("message", "unknown")
            return OrderResponse(False, None, r, err)
        return OrderResponse(True, r.get("order_id") or r.get("success_response", {}).get("order_id"), r)

    def place_market_buy(self, ticker: str, usd_amount: float,
                         client_order_id: str) -> OrderResponse:
        product = COINBASE_PRODUCTS.get(ticker.upper())
        if not product:
            return OrderResponse(False, None, {}, "unsupported product")
        body = {
            "client_order_id": client_order_id,
            "product_id": product,
            "side": "BUY",
            "order_configuration": {
                "market_market_ioc": {"quote_size": f"{usd_amount:.2f}"},
            },
        }
        r = self._request("POST", "/orders", body=body)
        if "_error" in r:
            return OrderResponse(False, None, r, f"Coinbase rejected: {r.get('_error')}")
        if not r.get("success", False):
            err = r.get("error_response", {}).get("message", "unknown")
            return OrderResponse(False, None, r, err)
        return OrderResponse(True, r.get("order_id") or r.get("success_response", {}).get("order_id"), r)

    def place_limit_sell(self, ticker: str, base_qty: float, limit_price: float,
                         client_order_id: str) -> OrderResponse:
        product = COINBASE_PRODUCTS.get(ticker.upper())
        if not product:
            return OrderResponse(False, None, {}, "unsupported product")
        if limit_price <= 0 or base_qty <= 0:
            return OrderResponse(False, None, {}, "invalid qty/price")
        body = {
            "client_order_id": client_order_id,
            "product_id": product,
            "side": "SELL",
            "order_configuration": {
                "limit_limit_gtc": {
                    "base_size": f"{base_qty:.8f}",
                    "limit_price": f"{limit_price:.2f}",
                    "post_only": False,
                },
            },
        }
        r = self._request("POST", "/orders", body=body)
        if "_error" in r:
            return OrderResponse(False, None, r, f"Coinbase rejected: {r.get('_error')}")
        if not r.get("success", False):
            err = r.get("error_response", {}).get("message", "unknown")
            return OrderResponse(False, None, r, err)
        return OrderResponse(True, r.get("order_id") or r.get("success_response", {}).get("order_id"), r)

    def place_market_sell(self, ticker: str, base_qty: float,
                          client_order_id: str) -> OrderResponse:
        product = COINBASE_PRODUCTS.get(ticker.upper())
        if not product:
            return OrderResponse(False, None, {}, "unsupported product")
        body = {
            "client_order_id": client_order_id,
            "product_id": product,
            "side": "SELL",
            "order_configuration": {
                "market_market_ioc": {"base_size": f"{base_qty:.8f}"},
            },
        }
        r = self._request("POST", "/orders", body=body)
        if "_error" in r:
            return OrderResponse(False, None, r, f"Coinbase rejected: {r.get('_error')}")
        if not r.get("success", False):
            err = r.get("error_response", {}).get("message", "unknown")
            return OrderResponse(False, None, r, err)
        return OrderResponse(True, r.get("order_id") or r.get("success_response", {}).get("order_id"), r)

    def cancel_order(self, order_id: str) -> OrderResponse:
        r = self._request("POST", "/orders/batch_cancel", body={"order_ids": [order_id]})
        if "_error" in r:
            return OrderResponse(False, order_id, r, str(r.get("_error")))
        return OrderResponse(True, order_id, r)

    def get_order_status(self, order_id: str) -> dict:
        return self._request("GET", f"/orders/historical/{order_id}")

    def get_filled_orders(self, since_iso: str | None = None) -> list[Fill]:
        """Paginated historical fills. Coinbase paginates with a `cursor`
        token. `since_iso` filters server-side via `start_sequence_timestamp`.
        Hard cap at 50 pages (~12,500 fills) so a runaway loop can't burn
        through their rate limit."""
        out: list[Fill] = []
        cursor: str | None = None
        for _ in range(50):
            params: dict = {"limit": 250}
            if cursor:
                params["cursor"] = cursor
            if since_iso:
                params["start_sequence_timestamp"] = since_iso
            r = self._request("GET", "/orders/historical/fills", params=params)
            if "_error" in r:
                break
            fills_raw = r.get("fills", []) or []
            for f in fills_raw:
                try:
                    product = f.get("product_id", "")
                    base = product.split("-", 1)[0] if "-" in product else product
                    base = base.upper()
                    # We only support USD-quoted spot pairs (BTC-USD etc.).
                    # Naive base-only matching would let BTC-USDC / BTC-USDT
                    # through and persist their quote-currency prices as USD
                    # — wrong cost basis. Match the *whole* product id.
                    if COINBASE_PRODUCTS.get(base) != product:
                        continue
                    qty = float(f.get("size") or 0)
                    price = float(f.get("price") or 0)
                    if qty <= 0 or price <= 0:
                        continue
                    side = (f.get("side") or "").upper()
                    if side not in ("BUY", "SELL"):
                        continue
                    external_id = str(f.get("trade_id") or f.get("entry_id") or "").strip()
                    # Reject empty external_id outright. The UNIQUE
                    # (exchange, external_id) constraint would otherwise
                    # collapse every later no-id fill into one global slot
                    # and silently drop them under "already imported".
                    if not external_id:
                        continue
                    out.append(Fill(
                        external_id=external_id,
                        ticker=base, side=side.lower(),
                        qty=qty, price=price,
                        fee_usd=float(f.get("commission") or 0),
                        filled_at=str(f.get("trade_time") or f.get("sequence_timestamp") or ""),
                        raw=f,
                    ))
                except (TypeError, ValueError, KeyError):
                    continue
            cursor = r.get("cursor")
            if not cursor:
                break
        return out


# ─── Kraken ─────────────────────────────────────────────────────────────────

KRAKEN_BASE = "https://api.kraken.com"

# Kraken pair codes. Kraken historically used "XBT" for BTC; the v0 REST API
# accepts both BTC/USD aliases as of 2024.
KRAKEN_PAIRS = {
    "BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD",
    "DOGE": "XDGUSD", "XRP": "XRPUSD",
}


def _kraken_sign(path: str, data: dict, secret_b64: str) -> str:
    """Compute Kraken's API-Sign header.
    sign = base64(HMAC-SHA512(path + SHA256(nonce + post_data), base64_decode(secret)))
    """
    try:
        secret = base64.b64decode(secret_b64)
    except Exception as e:
        raise RuntimeError(f"Invalid Kraken secret (base64): {e}")
    postdata = urllib.parse.urlencode(data)
    nonce = str(data["nonce"]).encode()
    sha256 = hashlib.sha256(nonce + postdata.encode()).digest()
    sig = hmac.new(secret, path.encode() + sha256, hashlib.sha512).digest()
    return base64.b64encode(sig).decode()


class KrakenAdapter:
    name = "kraken"

    def __init__(self, api_key: str, secret_b64: str):
        self.api_key = api_key.strip()
        self.secret = secret_b64.strip()
        # Validate base64 + length once. Kraken secrets are 88 chars base64
        # → 64 bytes raw.
        try:
            raw = base64.b64decode(self.secret)
            if len(raw) < 32:
                raise ValueError("too short")
        except Exception as e:
            raise RuntimeError(f"Invalid Kraken secret: {e}")

    def supported_assets(self) -> list[str]:
        return list(KRAKEN_PAIRS.keys())

    def _private(self, endpoint: str, data: dict | None = None) -> dict:
        path = f"/0/private/{endpoint}"
        body = dict(data or {})
        body["nonce"] = str(int(time.time() * 1000))
        try:
            sig = _kraken_sign(path, body, self.secret)
            headers = {
                "API-Key": self.api_key,
                "API-Sign": sig,
                "User-Agent": "CryptoEdge/1.0",
                "Content-Type": "application/x-www-form-urlencoded",
            }
            r = requests.post(KRAKEN_BASE + path, headers=headers, data=body, timeout=15)
            r.raise_for_status()
            payload = r.json()
        except requests.RequestException as e:
            return {"_error": str(e)}
        except ValueError:
            return {"_error": "non-JSON response"}
        # Kraken always returns {"error": [...], "result": {...}}.
        if payload.get("error"):
            return {"_error": payload["error"]}
        return payload.get("result", {})

    def _public(self, endpoint: str, params: dict | None = None) -> dict:
        try:
            r = requests.get(f"{KRAKEN_BASE}/0/public/{endpoint}", params=params, timeout=10)
            r.raise_for_status()
            payload = r.json()
        except (requests.RequestException, ValueError) as e:
            return {"_error": str(e)}
        if payload.get("error"):
            return {"_error": payload["error"]}
        return payload.get("result", {})

    def test_connection(self) -> tuple[bool, str]:
        r = self._private("Balance")
        if "_error" in r:
            return False, f"Kraken auth failed: {r['_error']}"
        return True, f"Kraken OK ({len(r)} assets in balance)"

    def get_balances(self) -> list[Balance]:
        r = self._private("Balance")
        if "_error" in r:
            return []
        out = []
        # Kraken returns codes like "XXBT" for BTC, "ZUSD" for USD; normalise.
        rename = {"XXBT": "BTC", "XBT": "BTC", "ZUSD": "USD", "XETH": "ETH",
                  "XXRP": "XRP", "XDG": "DOGE"}
        for code, qty_str in r.items():
            try:
                qty = float(qty_str)
            except (ValueError, TypeError):
                continue
            ticker = rename.get(code, code)
            out.append(Balance(asset=ticker, available=qty, total=qty))
        return out

    def get_price(self, ticker: str) -> Optional[float]:
        pair = KRAKEN_PAIRS.get(ticker.upper())
        if not pair:
            return None
        r = self._public("Ticker", params={"pair": pair})
        if "_error" in r:
            return None
        # Result is {pair_key: {a: [ask, ..], b: [bid, ..], c: [last, ..]}}
        for _, body in r.items():
            try:
                return float(body["c"][0])
            except (KeyError, ValueError, TypeError, IndexError):
                continue
        return None

    def place_limit_buy(self, ticker: str, usd_amount: float, limit_price: float,
                        client_order_id: str) -> OrderResponse:
        pair = KRAKEN_PAIRS.get(ticker.upper())
        if not pair:
            return OrderResponse(False, None, {}, "unsupported pair")
        if usd_amount <= 0 or limit_price <= 0:
            return OrderResponse(False, None, {}, "invalid amount/price")
        # Kraken volume is in base units.
        volume = usd_amount / limit_price
        body = {
            "pair": pair,
            "type": "buy",
            "ordertype": "limit",
            "price": f"{limit_price:.5f}",
            "volume": f"{volume:.8f}",
            "userref": _clip_userref(client_order_id),
        }
        r = self._private("AddOrder", body)
        if "_error" in r:
            return OrderResponse(False, None, r, str(r["_error"]))
        txid = (r.get("txid") or [None])[0]
        return OrderResponse(True, txid, r)

    def place_market_buy(self, ticker: str, usd_amount: float,
                         client_order_id: str) -> OrderResponse:
        pair = KRAKEN_PAIRS.get(ticker.upper())
        if not pair:
            return OrderResponse(False, None, {}, "unsupported pair")
        # Kraken doesn't have a quote-based market order; need to know the
        # last price to size in base units. Use the public Ticker.
        last = self.get_price(ticker)
        if not last:
            return OrderResponse(False, None, {}, "no price")
        volume = usd_amount / last
        body = {
            "pair": pair, "type": "buy", "ordertype": "market",
            "volume": f"{volume:.8f}",
            "userref": _clip_userref(client_order_id),
        }
        r = self._private("AddOrder", body)
        if "_error" in r:
            return OrderResponse(False, None, r, str(r["_error"]))
        txid = (r.get("txid") or [None])[0]
        return OrderResponse(True, txid, r)

    def place_limit_sell(self, ticker: str, base_qty: float, limit_price: float,
                         client_order_id: str) -> OrderResponse:
        pair = KRAKEN_PAIRS.get(ticker.upper())
        if not pair:
            return OrderResponse(False, None, {}, "unsupported pair")
        if base_qty <= 0 or limit_price <= 0:
            return OrderResponse(False, None, {}, "invalid qty/price")
        body = {
            "pair": pair, "type": "sell", "ordertype": "limit",
            "price": f"{limit_price:.5f}", "volume": f"{base_qty:.8f}",
            "userref": _clip_userref(client_order_id),
        }
        r = self._private("AddOrder", body)
        if "_error" in r:
            return OrderResponse(False, None, r, str(r["_error"]))
        txid = (r.get("txid") or [None])[0]
        return OrderResponse(True, txid, r)

    def place_market_sell(self, ticker: str, base_qty: float,
                          client_order_id: str) -> OrderResponse:
        pair = KRAKEN_PAIRS.get(ticker.upper())
        if not pair:
            return OrderResponse(False, None, {}, "unsupported pair")
        body = {
            "pair": pair, "type": "sell", "ordertype": "market",
            "volume": f"{base_qty:.8f}",
            "userref": _clip_userref(client_order_id),
        }
        r = self._private("AddOrder", body)
        if "_error" in r:
            return OrderResponse(False, None, r, str(r["_error"]))
        txid = (r.get("txid") or [None])[0]
        return OrderResponse(True, txid, r)

    def cancel_order(self, order_id: str) -> OrderResponse:
        r = self._private("CancelOrder", {"txid": order_id})
        if "_error" in r:
            return OrderResponse(False, order_id, r, str(r["_error"]))
        return OrderResponse(True, order_id, r)

    def get_order_status(self, order_id: str) -> dict:
        return self._private("QueryOrders", {"txid": order_id, "trades": True})

    def get_filled_orders(self, since_iso: str | None = None) -> list[Fill]:
        """Paginated historical trades. Kraken uses `ofs` (offset). Caps at
        50 pages × 50 trades = 2,500 — fine for first-time imports too;
        the call is fast."""
        # Pair → ticker normaliser. Kraken returns pairs as XXBTZUSD,
        # XETHZUSD, etc. We strip the Z-prefixed quote currency.
        pair_to_ticker = {
            "XBTUSD": "BTC", "XXBTZUSD": "BTC", "XBTZUSD": "BTC",
            "ETHUSD": "ETH", "XETHZUSD": "ETH",
            "SOLUSD": "SOL",
            "XDGUSD": "DOGE", "XXDGZUSD": "DOGE",
            "XRPUSD": "XRP", "XXRPZUSD": "XRP",
        }
        since_ts = None
        if since_iso:
            try:
                since_ts = int(datetime.fromisoformat(since_iso).timestamp())
            except (ValueError, TypeError):
                since_ts = None

        out: list[Fill] = []
        offset = 0
        for _ in range(50):
            body: dict = {"ofs": offset, "trades": True}
            if since_ts is not None:
                body["start"] = since_ts
            r = self._private("TradesHistory", body)
            if "_error" in r:
                break
            trades = r.get("trades", {}) or {}
            if not trades:
                break
            for tid, t in trades.items():
                try:
                    pair = (t.get("pair") or "").upper()
                    ticker = pair_to_ticker.get(pair)
                    if not ticker:
                        continue
                    qty = float(t.get("vol") or 0)
                    price = float(t.get("price") or 0)
                    if qty <= 0 or price <= 0:
                        continue
                    side = (t.get("type") or "").lower()
                    if side not in ("buy", "sell"):
                        continue
                    ts_unix = float(t.get("time") or 0)
                    if ts_unix <= 0:
                        continue
                    ext = str(tid).strip()
                    if not ext:
                        continue   # see Coinbase: empty id would dedup-collapse
                    filled_at = datetime.fromtimestamp(
                        ts_unix, tz=timezone.utc,
                    ).isoformat()
                    out.append(Fill(
                        external_id=ext,
                        ticker=ticker, side=side,
                        qty=qty, price=price,
                        fee_usd=float(t.get("fee") or 0),
                        filled_at=filled_at,
                        raw=dict(t),
                    ))
                except (TypeError, ValueError, KeyError):
                    continue
            # Kraken returns `count` = total available; break when offset
            # exceeds it. Also break if we got fewer than the page size.
            offset += len(trades)
            count = int(r.get("count") or 0)
            if offset >= count or len(trades) == 0:
                break
        return out


def _clip_userref(client_order_id: str) -> int:
    """Kraken `userref` must fit in a 32-bit signed int. Hash the client id."""
    h = hashlib.sha256(client_order_id.encode()).digest()
    return int.from_bytes(h[:4], "big") & 0x7FFFFFFF


# ─── Factory ────────────────────────────────────────────────────────────────

def get_adapter(user_id: str, exchange: str) -> Optional[ExchangeAdapter]:
    """Build an adapter from stored credentials. Returns None if no creds."""
    creds = load_exchange_credentials(user_id, exchange)
    if not creds:
        return None
    try:
        if exchange == "coinbase":
            return CoinbaseAdapter(creds["api_key"], creds["private_key_pem"])
        elif exchange == "kraken":
            return KrakenAdapter(creds["api_key"], creds["secret"])
    except Exception as e:
        log.warning("adapter init failed for %s: %s", exchange, e)
        return None
    return None


def configured_exchanges(user_id: str) -> list[str]:
    """Which exchanges has this user configured? Returns a list of exchange names."""
    out = []
    for ex in ("coinbase", "kraken"):
        if db.get_exchange_credentials(user_id, ex):
            out.append(ex)
    return out

"""Kalshi RSA-PSS request signer + signed HTTP client.

Kalshi's authenticated API requires every request to be signed with the
account's RSA private key. The signature spec:

    sig_input  = f"{timestamp_ms}{method}{path}"
    signature  = base64( RSA_PSS(
                     hash       = SHA256,
                     mgf        = MGF1(SHA256),
                     salt_length = 32,
                     message    = sig_input.encode(),
                     key        = private_key,
                 ) )

    headers = {
        "KALSHI-ACCESS-KEY":       <key_id>,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
        "Content-Type":            "application/json",
    }

The signing path is *path only* (no query string, no body). The body is
sent in JSON but does not contribute to the signature — that's a
deliberate Kalshi design choice for cacheability of body-less GETs.

This module is pure: `sign_request` returns a (headers, body) tuple
without touching the network. `KalshiSignedClient` wraps `requests` for
production use and accepts a clock function so tests can pin
deterministic timestamps.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

logger = logging.getLogger(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"


def load_rsa_private_key(pem_bytes: bytes,
                         password: Optional[bytes] = None) -> rsa.RSAPrivateKey:
    """Parse a PEM-encoded RSA private key.

    Raises ``ValueError`` if the bytes don't look like a valid PEM RSA
    private key. The error message is generic on purpose — we don't
    want to leak structural detail to a caller passing user input.
    """
    if not pem_bytes:
        raise ValueError("empty key bytes")
    try:
        key = serialization.load_pem_private_key(pem_bytes, password=password)
    except Exception as e:
        logger.debug("RSA key load failed: %s", type(e).__name__)
        raise ValueError("could not parse RSA private key") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("key is not an RSA private key")
    if key.key_size < 2048:
        raise ValueError("RSA key size below 2048 bits — refusing to use")
    return key


def sign_message(private_key: rsa.RSAPrivateKey, message: bytes) -> str:
    """Return base64-encoded RSA-PSS(SHA-256, salt=32) signature."""
    sig = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=32,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("ascii")


def verify_signature(public_key: rsa.RSAPublicKey, message: bytes,
                     signature_b64: str) -> bool:
    """Verify a signature — used by tests and the audit replayer."""
    try:
        public_key.verify(
            base64.b64decode(signature_b64),
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=32,
            ),
            hashes.SHA256(),
        )
        return True
    except InvalidSignature:
        return False


@dataclass
class SignedRequest:
    """Output of `sign_request`. Caller passes these straight to requests."""
    method: str
    url: str
    headers: dict
    body: Optional[bytes]


def sign_request(private_key: rsa.RSAPrivateKey, key_id: str,
                 method: str, path: str, body: Optional[dict] = None,
                 *, timestamp_ms: Optional[int] = None,
                 base_url: str = KALSHI_BASE) -> SignedRequest:
    """Build the signed-request bundle for one Kalshi API call.

    `path` must be the path *only* (e.g. "/portfolio/balance"). The
    base_url is added back when forming the URL but excluded from the
    signing string. Query strings, if any, must already be appended to
    `path` — Kalshi's spec includes the path verbatim in the signature.
    """
    if not key_id:
        raise ValueError("key_id is required")
    if not path.startswith("/"):
        path = "/" + path
    method = method.upper()
    ts = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
    sig_input = f"{ts}{method}{path}".encode("utf-8")
    signature = sign_message(private_key, sig_input)

    headers = {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "Accept": "application/json",
    }
    body_bytes: Optional[bytes] = None
    if body is not None:
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    full_url = base_url.rstrip("/") + path
    return SignedRequest(method=method, url=full_url, headers=headers,
                         body=body_bytes)


class KalshiSignedClient:
    """Thin authenticated client. Network calls only — no order or
    risk logic, those live in `trade_engine`.
    """

    def __init__(self, key_id: str, private_key: rsa.RSAPrivateKey,
                 *, base_url: str = KALSHI_BASE,
                 timeout_seconds: float = 10.0,
                 clock: Callable[[], float] = time.time):
        self.key_id = key_id
        self.private_key = private_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        self._clock = clock

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def _send(self, method: str, path: str, body: Optional[dict] = None
              ) -> tuple[int, Optional[dict]]:
        signed = sign_request(self.private_key, self.key_id, method, path,
                              body, timestamp_ms=self._now_ms(),
                              base_url=self.base_url)
        try:
            resp = requests.request(signed.method, signed.url,
                                    headers=signed.headers,
                                    data=signed.body,
                                    timeout=self.timeout)
        except requests.RequestException as e:
            logger.warning("Kalshi network error %s %s: %s", method, path, e)
            return -1, {"error": "network", "detail": str(e)}
        try:
            data = resp.json() if resp.content else None
        except ValueError:
            data = {"raw": resp.text}
        return resp.status_code, data

    # Public wrappers that the engine uses. Any non-200 surfaces the
    # status + body to the caller — never silently retry.
    def get_balance(self) -> tuple[int, Optional[dict]]:
        return self._send("GET", "/portfolio/balance")

    def get_positions(self) -> tuple[int, Optional[dict]]:
        return self._send("GET", "/portfolio/positions")

    def list_orders(self, status: Optional[str] = None,
                    limit: int = 100) -> tuple[int, Optional[dict]]:
        path = f"/portfolio/orders?limit={limit}"
        if status:
            path += f"&status={status}"
        return self._send("GET", path)

    def place_order(self, ticker: str, side: str, action: str, count: int,
                    type_: str = "limit", yes_price_cents: Optional[int] = None,
                    no_price_cents: Optional[int] = None,
                    client_order_id: Optional[str] = None
                    ) -> tuple[int, Optional[dict]]:
        body: dict = {
            "ticker": ticker, "side": side, "action": action,
            "count": int(count), "type": type_,
        }
        if yes_price_cents is not None:
            body["yes_price"] = int(yes_price_cents)
        if no_price_cents is not None:
            body["no_price"] = int(no_price_cents)
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self._send("POST", "/portfolio/orders", body=body)

    def cancel_order(self, order_id: str) -> tuple[int, Optional[dict]]:
        return self._send("DELETE", f"/portfolio/orders/{order_id}")

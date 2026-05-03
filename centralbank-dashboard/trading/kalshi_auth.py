"""Kalshi API authentication — RSA-PSS request signing.

Per Kalshi's auth spec (https://trading-api.readme.io/reference/api-key-auth):

  1. Compose the message:    f"{timestamp_ms}{METHOD}{path}"
     - timestamp_ms is current Unix time in milliseconds (string)
     - METHOD is the uppercase HTTP verb
     - path is the request path *with* the API prefix but *without* query string

  2. Sign the message bytes with the user's RSA private key using:
     - PSS padding (MGF1 over SHA-256, salt length = digest length)
     - SHA-256 hash

  3. Base64-encode the signature and send three headers on the request:
     - KALSHI-ACCESS-KEY:        the API key id
     - KALSHI-ACCESS-TIMESTAMP:  the same timestamp_ms
     - KALSHI-ACCESS-SIGNATURE:  base64(signature)

We do *not* store the user's plaintext private key in memory beyond the
duration of a single signing call — :mod:`key_store` decrypts the key just
in time, hands it to ``sign_request``, and we drop the reference immediately
after. That keeps the attack surface for a memory dump narrow.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


# --- Endpoints --------------------------------------------------------------

KALSHI_PROD_HOST = "https://api.elections.kalshi.com"
KALSHI_DEMO_HOST = "https://demo-api.kalshi.co"
KALSHI_API_PREFIX = "/trade-api/v2"


def host_for_mode(mode: str) -> str:
    """Return the Kalshi host for ``mode``: ``"paper"`` (demo) or ``"prod"`` (live)."""
    if mode == "paper":
        return KALSHI_DEMO_HOST
    if mode == "prod":
        return KALSHI_PROD_HOST
    raise ValueError(f"unknown trading mode: {mode!r}")


# --- Signing ---------------------------------------------------------------

def load_private_key(pem: bytes | str, password: bytes | None = None) -> rsa.RSAPrivateKey:
    """Parse a PEM-encoded RSA private key. Raises ``ValueError`` on bad PEM."""
    if isinstance(pem, str):
        pem = pem.encode("utf-8")
    key = serialization.load_pem_private_key(pem, password=password)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("Kalshi auth requires an RSA private key (got something else)")
    return key


def sign_message(private_key: rsa.RSAPrivateKey, message: bytes) -> str:
    """Sign ``message`` with RSA-PSS / SHA-256 / MGF1-SHA-256, return base64."""
    sig = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("ascii")


@dataclass
class SignedRequest:
    method: str         # "GET" / "POST" / "DELETE"
    full_url: str       # absolute URL including host
    headers: dict       # KALSHI-ACCESS-{KEY,TIMESTAMP,SIGNATURE} + Content-Type
    body: bytes | None  # JSON body, or None for non-POST


def build_signed_request(
    method: str,
    path: str,
    api_key_id: str,
    private_key: rsa.RSAPrivateKey,
    *,
    mode: str = "paper",
    body: bytes | None = None,
) -> SignedRequest:
    """Assemble a signed Kalshi request ready to fire via urllib/httpx.

    ``path`` should start with ``/trade-api/v2/...`` — the prefix is part of
    Kalshi's signed message. ``mode`` picks the prod or paper endpoint.
    """
    method = method.upper()
    timestamp_ms = str(int(time.time() * 1000))
    message = (timestamp_ms + method + path).encode("utf-8")
    signature_b64 = sign_message(private_key, message)
    headers = {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": signature_b64,
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    full_url = host_for_mode(mode) + path
    return SignedRequest(method=method, full_url=full_url, headers=headers, body=body)


# --- Self-test --------------------------------------------------------------

if __name__ == "__main__":
    # Generate an ephemeral key and sign a fake request — proves the crypto
    # paths run end-to-end without needing real Kalshi credentials.
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
    test_key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    req = build_signed_request(
        "GET", "/trade-api/v2/portfolio/balance",
        api_key_id="test-key-id", private_key=test_key, mode="paper",
    )
    print("URL:    ", req.full_url)
    print("Method: ", req.method)
    for k, v in req.headers.items():
        if k == "KALSHI-ACCESS-SIGNATURE":
            v = v[:24] + "…"
        print(f"  {k}: {v}")

"""Tests for Kalshi RSA-PSS request signing."""

import base64
import time

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_signing import (
    KalshiSignedClient,
    load_rsa_private_key,
    sign_message,
    sign_request,
    verify_signature,
)
from tests._trade_fixtures import make_test_rsa_key


def test_load_rejects_empty_bytes():
    with pytest.raises(ValueError):
        load_rsa_private_key(b"")


def test_load_rejects_garbage():
    with pytest.raises(ValueError):
        load_rsa_private_key(b"-----BEGIN RSA PRIVATE KEY-----\nnope\n-----END RSA PRIVATE KEY-----\n")


def test_load_accepts_valid_pem():
    pem, _ = make_test_rsa_key()
    key = load_rsa_private_key(pem)
    assert isinstance(key, rsa.RSAPrivateKey)


def test_load_rejects_undersized_keys():
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    from cryptography.hazmat.primitives import serialization
    pem = weak.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with pytest.raises(ValueError, match="below 2048"):
        load_rsa_private_key(pem)


def test_signature_round_trip_verifies():
    _pem, pk = make_test_rsa_key()
    msg = b"1234567890123GET/portfolio/balance"
    sig = sign_message(pk, msg)
    assert isinstance(sig, str)
    assert verify_signature(pk.public_key(), msg, sig) is True


def test_signature_rejects_tampered_message():
    _pem, pk = make_test_rsa_key()
    sig = sign_message(pk, b"original")
    assert verify_signature(pk.public_key(), b"tampered", sig) is False


def test_sign_request_builds_headers():
    _pem, pk = make_test_rsa_key()
    signed = sign_request(pk, "key-abc", "GET", "/portfolio/balance",
                          timestamp_ms=1700000000000)
    assert signed.method == "GET"
    assert signed.url.endswith("/portfolio/balance")
    assert signed.headers["KALSHI-ACCESS-KEY"] == "key-abc"
    assert signed.headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    assert "KALSHI-ACCESS-SIGNATURE" in signed.headers
    # No body for GET
    assert signed.body is None
    assert "Content-Type" not in signed.headers


def test_sign_request_includes_body_for_post():
    _pem, pk = make_test_rsa_key()
    signed = sign_request(pk, "key-abc", "POST", "/portfolio/orders",
                          body={"ticker": "FOO", "qty": 1},
                          timestamp_ms=1700000000000)
    assert signed.body is not None
    assert signed.headers["Content-Type"] == "application/json"
    # Body should be deterministic JSON (separators=(',',':'))
    assert b'"ticker":"FOO"' in signed.body


def test_sign_request_signature_matches_spec_format():
    """The signed bytes are exactly `{ts}{METHOD}{path}` — no body, no query."""
    _pem, pk = make_test_rsa_key()
    ts = 1700000000000
    method = "GET"
    path = "/portfolio/balance"
    signed = sign_request(pk, "key-abc", method, path, timestamp_ms=ts)
    sig_b64 = signed.headers["KALSHI-ACCESS-SIGNATURE"]
    expected_msg = f"{ts}{method}{path}".encode()
    assert verify_signature(pk.public_key(), expected_msg, sig_b64)


def test_sign_request_normalizes_leading_slash():
    """RSA-PSS uses a random salt so two signatures of the same message
    are byte-different. Instead we verify both signatures validate
    against the same expected message — i.e. the leading slash was
    normalized identically."""
    _pem, pk = make_test_rsa_key()
    a = sign_request(pk, "k", "GET", "portfolio/balance", timestamp_ms=1)
    b = sign_request(pk, "k", "GET", "/portfolio/balance", timestamp_ms=1)
    expected = b"1GET/portfolio/balance"
    assert verify_signature(pk.public_key(), expected, a.headers["KALSHI-ACCESS-SIGNATURE"])
    assert verify_signature(pk.public_key(), expected, b.headers["KALSHI-ACCESS-SIGNATURE"])
    # And the URLs are the same too
    assert a.url == b.url


def test_client_uses_clock_for_timestamp():
    _pem, pk = make_test_rsa_key()
    pinned = [1700000000.0]
    client = KalshiSignedClient("key-x", pk, clock=lambda: pinned[0])
    assert client._now_ms() == 1700000000000
    pinned[0] = 1700000001.5
    assert client._now_ms() == 1700000001500


def test_client_distinct_timestamps_produce_distinct_signatures():
    _pem, pk = make_test_rsa_key()
    s1 = sign_request(pk, "k", "GET", "/p", timestamp_ms=1).headers["KALSHI-ACCESS-SIGNATURE"]
    s2 = sign_request(pk, "k", "GET", "/p", timestamp_ms=2).headers["KALSHI-ACCESS-SIGNATURE"]
    # PSS includes a random salt so signatures differ even for the same
    # input; this test mainly confirms timestamps are part of the
    # signed bytes by verifying the corresponding messages independently.
    assert s1 != s2
    assert verify_signature(pk.public_key(), b"1GET/p", s1)
    assert verify_signature(pk.public_key(), b"2GET/p", s2)

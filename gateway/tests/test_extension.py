"""Tests for the browser extension backend — JWT auth + market overlay API.

Covers:
- Extension JWT sign/verify round-trip
- JWT expiry enforcement
- JWT type='extension' enforcement
- /extension/auth page requires session, returns HTML with postMessage
- /api/extension/market/{slug} returns correct structure
- URL input extracts slug correctly
- Rate limit 60/min per JWT subject
- Tier gating: Trader can access, unauthenticated cannot
- 401 on invalid/expired JWT
- 404 on unknown market
- Insider signals included for Pro tier
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import time
import unittest

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("EMAIL_DRY_RUN", "true")
os.environ.setdefault("GATEWAY_COOKIE_SECRET", "test-ext-secret")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402

_test_conn = sqlite3.connect(":memory:", check_same_thread=False)
_test_conn.row_factory = sqlite3.Row
_test_conn.execute("PRAGMA foreign_keys = ON")


@contextlib.contextmanager
def _fake_conn():
    try:
        yield _test_conn
        _test_conn.commit()
    except Exception:
        _test_conn.rollback()
        raise


db.conn = _fake_conn
db.init_db()
import migrations  # noqa: E402
migrations.upgrade_to_head()

import server  # noqa: E402
import server_features  # noqa: F401,E402
from server_features import _ext_jwt_sign, _ext_jwt_decode  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app)


class _RebindMixin:
    @classmethod
    def setUpClass(cls):
        cls._prev = db.conn
        db.conn = _fake_conn

    @classmethod
    def tearDownClass(cls):
        db.conn = cls._prev

    def setUp(self):
        db.conn = _fake_conn
        client.cookies.clear()
        # Clear rate limits between tests
        with db.conn() as c:
            c.execute("DELETE FROM rate_limits")


# ── JWT sign/verify ─────────────────────────────────────────────────────


class TestExtensionJWT(_RebindMixin, unittest.TestCase):
    def test_sign_and_decode_roundtrip(self):
        now = int(time.time())
        payload = {
            "sub": 42,
            "email": "test@example.com",
            "display_name": "Test",
            "tier": "trader",
            "type": "extension",
            "iat": now,
            "exp": now + 3600,
        }
        token = _ext_jwt_sign(payload)
        decoded = _ext_jwt_decode(token)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["sub"], 42)
        self.assertEqual(decoded["tier"], "trader")
        self.assertEqual(decoded["type"], "extension")

    def test_expired_jwt_rejected(self):
        now = int(time.time())
        token = _ext_jwt_sign({
            "sub": 1, "type": "extension", "iat": now - 7200, "exp": now - 3600,
        })
        self.assertIsNone(_ext_jwt_decode(token))

    def test_wrong_type_rejected(self):
        now = int(time.time())
        token = _ext_jwt_sign({
            "sub": 1, "type": "session", "iat": now, "exp": now + 3600,
        })
        self.assertIsNone(_ext_jwt_decode(token))

    def test_tampered_jwt_rejected(self):
        now = int(time.time())
        token = _ext_jwt_sign({
            "sub": 1, "type": "extension", "iat": now, "exp": now + 3600,
        })
        parts = token.split(".")
        # Flip a character in the payload
        tampered_payload = parts[1][:-1] + ("a" if parts[1][-1] != "a" else "b")
        tampered = f"{parts[0]}.{tampered_payload}.{parts[2]}"
        self.assertIsNone(_ext_jwt_decode(tampered))

    def test_jwt_has_three_parts(self):
        self.assertIsNone(_ext_jwt_decode("onlyonepart"))
        self.assertIsNone(_ext_jwt_decode("two.parts"))
        self.assertIsNone(_ext_jwt_decode(""))


# ── /extension/auth page ────────────────────────────────────────────────


class TestExtensionAuthPage(_RebindMixin, unittest.TestCase):
    def test_requires_session(self):
        r = client.get("/extension/auth", follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        self.assertIn("/token", r.headers.get("location", ""))

    def test_returns_html_with_postmessage(self):
        uid = db.create_user("extauth@example.com", "TestPass123!", username="extauth")
        token = db.create_session(uid)
        try:
            db.mark_session_two_fa_verified(token)
        except Exception:
            pass
        r = client.get("/extension/auth", cookies={server.COOKIE_NAME: token})
        self.assertEqual(r.status_code, 200)
        self.assertIn("NARVE_EXT_AUTH", r.text)
        self.assertIn("postMessage", r.text)
        self.assertIn("Extension connected", r.text)

    def test_jwt_in_page_is_valid(self):
        uid = db.create_user("extjwt@example.com", "TestPass123!", username="extjwt")
        token = db.create_session(uid)
        try:
            db.mark_session_two_fa_verified(token)
        except Exception:
            pass
        r = client.get("/extension/auth", cookies={server.COOKIE_NAME: token})
        # Extract the JWT from the page's postMessage JS
        import re
        match = re.search(r'jwt:\s*"([^"]+)"', r.text)
        self.assertIsNotNone(match, "JWT not found in auth page response")
        jwt_value = match.group(1)
        decoded = _ext_jwt_decode(jwt_value)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["sub"], uid)
        self.assertEqual(decoded["type"], "extension")


# ── /api/extension/market/{slug} ────────────────────────────────────────


def _make_trader_jwt(user_id=99):
    now = int(time.time())
    return _ext_jwt_sign({
        "sub": user_id, "tier": "trader", "type": "extension",
        "iat": now, "exp": now + 3600,
    })


def _make_pro_jwt(user_id=99):
    now = int(time.time())
    return _ext_jwt_sign({
        "sub": user_id, "tier": "pro", "type": "extension",
        "iat": now, "exp": now + 3600,
    })


def _make_free_jwt(user_id=99):
    now = int(time.time())
    return _ext_jwt_sign({
        "sub": user_id, "tier": "free", "type": "extension",
        "iat": now, "exp": now + 3600,
    })


class TestExtensionMarketAPI(_RebindMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Seed a prediction + source so the market has data
        db.upsert_source_credibility("analyst1", global_credibility=0.8, accuracy_unlocked=1)
        db.create_prediction(
            "analyst1", "Fed will hold rates", category="politics",
            market_id="fed-rates-hold", direction="YES", predicted_probability=0.7,
        )
        db.insert_market_snapshot(
            "fed-rates-hold", 0.62,
            market_question="Will the Fed hold rates?",
            category="politics",
        )

    def test_returns_correct_structure(self):
        jwt = _make_trader_jwt()
        r = client.get(
            "/api/extension/market/fed-rates-hold",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["market_slug"], "fed-rates-hold")
        self.assertIn("betyc_yes_probability", body)
        self.assertIn("market_yes_price", body)
        self.assertIn("betyc_edge", body)
        self.assertIn("betyc_confidence", body)
        self.assertIn("source_count", body)
        self.assertIn("top_sources", body)
        self.assertIn("risk_flag", body)
        self.assertIn("insider_signals", body)
        self.assertIsInstance(body["top_sources"], list)

    def test_url_input_extracts_slug(self):
        jwt = _make_trader_jwt()
        r = client.get(
            "/api/extension/market/https%3A%2F%2Fpolymarket.com%2Fevent%2Ffed-rates-hold",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        # The endpoint should extract "fed-rates-hold" from the URL
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["market_slug"], "fed-rates-hold")

    def test_401_without_auth(self):
        r = client.get("/api/extension/market/fed-rates-hold")
        self.assertEqual(r.status_code, 401)

    def test_401_with_expired_jwt(self):
        now = int(time.time())
        expired = _ext_jwt_sign({
            "sub": 1, "tier": "trader", "type": "extension",
            "iat": now - 7200, "exp": now - 3600,
        })
        r = client.get(
            "/api/extension/market/fed-rates-hold",
            headers={"Authorization": f"Bearer {expired}"},
        )
        self.assertEqual(r.status_code, 401)

    def test_403_for_free_tier(self):
        jwt = _make_free_jwt()
        r = client.get(
            "/api/extension/market/fed-rates-hold",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        self.assertEqual(r.status_code, 403)

    def test_trader_tier_can_access(self):
        jwt = _make_trader_jwt()
        r = client.get(
            "/api/extension/market/fed-rates-hold",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        self.assertEqual(r.status_code, 200)

    def test_404_for_unknown_market(self):
        jwt = _make_trader_jwt()
        r = client.get(
            "/api/extension/market/totally-nonexistent-market-slug",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        # Either 404 or 200 with null fields (both acceptable for unknown markets)
        if r.status_code == 200:
            body = r.json()
            # All data fields should be None/empty for an unknown market
            self.assertIsNone(body.get("betyc_yes_probability"))
        else:
            self.assertEqual(r.status_code, 404)

    def test_pro_jwt_includes_insider_signals_field(self):
        jwt = _make_pro_jwt()
        r = client.get(
            "/api/extension/market/fed-rates-hold",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("insider_signals", body)
        self.assertIsInstance(body["insider_signals"], list)

    def test_rate_limit_60_per_minute(self):
        jwt = _make_trader_jwt(user_id=777)
        for i in range(60):
            r = client.get(
                "/api/extension/market/fed-rates-hold",
                headers={"Authorization": f"Bearer {jwt}"},
            )
            self.assertIn(r.status_code, (200, 429), f"Request {i+1} failed with {r.status_code}")
        # 61st request should be rate limited
        r = client.get(
            "/api/extension/market/fed-rates-hold",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        self.assertEqual(r.status_code, 429)

    def test_cached_response_same_within_ttl(self):
        jwt = _make_trader_jwt(user_id=888)
        r1 = client.get(
            "/api/extension/market/fed-rates-hold",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        r2 = client.get(
            "/api/extension/market/fed-rates-hold",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        # Both should return identical data (cache hit)
        self.assertEqual(r1.json()["market_slug"], r2.json()["market_slug"])
        self.assertEqual(r1.json()["betyc_yes_probability"], r2.json()["betyc_yes_probability"])


if __name__ == "__main__":
    unittest.main()

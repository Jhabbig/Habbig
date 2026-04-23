"""Tests for the /api/public/v1 Bearer-auth surface.

Covers:
  - verify_api_key: accepts valid, rejects missing / invalid / revoked
  - per-key hourly rate limiting (429 with Retry-After once over cap)
  - require_scope('write'): GET endpoints work with read-only,
    POST /predictions blocked without write
  - usage endpoint returns the post-increment count
"""

from __future__ import annotations

import os
import unittest

from tests import _testdb  # noqa: F401  — shared in-memory DB bootstrap

# Force non-production so SubproductMiddleware doesn't demand CF headers
# in the TestClient path.
os.environ["PRODUCTION"] = "0"

import db
import api_v1  # canonical key-mint helper
from fastapi.testclient import TestClient


def _mk_user(email: str) -> int:
    return db.create_user(email, "pw-" * 4, username=email.split("@")[0])


def _mint_key(user_id: int, *, scopes: str = "read", rate_limit: int = 5):
    """Mint a fresh key via the canonical path, then patch scopes + rate
    limit to whatever the test needs. The real endpoint does this same
    post-insert UPDATE."""
    raw, key_id = api_v1.create_api_key(user_id=user_id, name="t", tier="standard")
    with db.conn() as c:
        c.execute(
            "UPDATE api_keys SET scopes = ?, rate_limit_hour = ? WHERE id = ?",
            (scopes, rate_limit, key_id),
        )
    return raw, key_id


def _client():
    # Import lazily so _testdb's PRAGMAs have already patched db.conn.
    import server
    return TestClient(server.app)


_HOST = {"host": "narve.ai"}


class TestAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.uid = _mk_user(f"auth_{id(cls)}@t.com")
        cls.raw, cls.kid = _mint_key(cls.uid, rate_limit=10)
        cls.c = _client()

    def test_missing_bearer_returns_401(self):
        r = self.c.get("/api/public/v1/sources", headers=_HOST)
        self.assertEqual(r.status_code, 401)

    def test_non_narve_prefix_bearer_returns_401(self):
        r = self.c.get(
            "/api/public/v1/sources",
            headers={**_HOST, "authorization": "Bearer sk_openai_something"},
        )
        self.assertEqual(r.status_code, 401)

    def test_garbage_key_returns_401(self):
        r = self.c.get(
            "/api/public/v1/sources",
            headers={**_HOST, "authorization": "Bearer narve_not_a_real_key"},
        )
        self.assertEqual(r.status_code, 401)

    def test_valid_key_passes(self):
        r = self.c.get(
            "/api/public/v1/usage",
            headers={**_HOST, "authorization": f"Bearer {self.raw}"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("key_id", r.json())

    def test_revoked_key_returns_401(self):
        # Mint a second key so we don't break sibling tests.
        raw2, kid2 = _mint_key(self.uid)
        self.assertTrue(db.revoke_api_key(kid2, self.uid))
        r = self.c.get(
            "/api/public/v1/sources",
            headers={**_HOST, "authorization": f"Bearer {raw2}"},
        )
        self.assertEqual(r.status_code, 401)


class TestRateLimit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.uid = _mk_user(f"rl_{id(cls)}@t.com")
        cls.c = _client()

    def test_429_after_bucket_fills(self):
        # Tight budget so we hit the cap fast.
        raw, _ = _mint_key(self.uid, rate_limit=3)
        hdr = {**_HOST, "authorization": f"Bearer {raw}"}
        # First 3 OK.
        for _ in range(3):
            self.assertEqual(self.c.get("/api/public/v1/sources", headers=hdr).status_code, 200)
        # 4th → 429 with Retry-After.
        r = self.c.get("/api/public/v1/sources", headers=hdr)
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)
        self.assertIn("X-RateLimit-Limit", r.headers)


class TestScopes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.uid = _mk_user(f"scope_{id(cls)}@t.com")
        cls.read_raw, _ = _mint_key(cls.uid, scopes="read")
        cls.write_raw, _ = _mint_key(cls.uid, scopes="read,write")
        cls.c = _client()

    def test_read_key_cannot_post_predictions(self):
        r = self.c.post(
            "/api/public/v1/predictions",
            headers={**_HOST, "authorization": f"Bearer {self.read_raw}",
                     "content-type": "application/json"},
            json={"market_slug": "x", "predicted_outcome": "YES",
                  "predicted_probability": 0.6},
        )
        self.assertEqual(r.status_code, 403)

    def test_read_key_can_get(self):
        r = self.c.get(
            "/api/public/v1/feed",
            headers={**_HOST, "authorization": f"Bearer {self.read_raw}"},
        )
        self.assertEqual(r.status_code, 200)
        # Shape check only — don't assert counts.
        self.assertIn("feed", r.json())

    def test_write_key_reaches_validation(self):
        # Reaches route-level validation — at minimum gets past 403/401.
        r = self.c.post(
            "/api/public/v1/predictions",
            headers={**_HOST, "authorization": f"Bearer {self.write_raw}",
                     "content-type": "application/json"},
            json={"market_slug": "x", "predicted_outcome": "BOGUS",
                  "predicted_probability": 0.6},
        )
        # 400 (bad outcome) proves scope check passed; anything else means
        # scope enforcement leaked a 403/401 we shouldn't see here.
        self.assertIn(r.status_code, (400, 409, 500))


class TestReadEndpointsShape(unittest.TestCase):
    """Just confirm every GET returns a well-formed JSON object.
    No count assertions — data may be empty on a fresh test DB."""

    @classmethod
    def setUpClass(cls):
        cls.uid = _mk_user(f"shape_{id(cls)}@t.com")
        cls.raw, _ = _mint_key(cls.uid, rate_limit=100)
        cls.c = _client()

    def _hdr(self):
        return {**_HOST, "authorization": f"Bearer {self.raw}"}

    def test_sources_shape(self):
        r = self.c.get("/api/public/v1/sources", headers=self._hdr())
        self.assertEqual(r.status_code, 200)
        self.assertIn("sources", r.json())
        self.assertIn("total", r.json())

    def test_markets_shape(self):
        r = self.c.get("/api/public/v1/markets?q=", headers=self._hdr())
        self.assertEqual(r.status_code, 200)
        self.assertIn("markets", r.json())

    def test_feed_shape(self):
        r = self.c.get("/api/public/v1/feed", headers=self._hdr())
        self.assertEqual(r.status_code, 200)
        self.assertIn("feed", r.json())

    def test_best_bets_shape(self):
        r = self.c.get("/api/public/v1/best-bets", headers=self._hdr())
        self.assertEqual(r.status_code, 200)
        self.assertIn("best_bets", r.json())

    def test_calendar_shape(self):
        r = self.c.get("/api/public/v1/calendar", headers=self._hdr())
        self.assertEqual(r.status_code, 200)
        self.assertIn("calendar", r.json())

    def test_usage_reports_current_bucket(self):
        r = self.c.get("/api/public/v1/usage", headers=self._hdr())
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertGreaterEqual(int(body["requests_this_hour"]), 1)
        self.assertEqual(int(body["bucket_resets_at"]) - int(body["hour_bucket_start"]), 3600)


if __name__ == "__main__":
    unittest.main()

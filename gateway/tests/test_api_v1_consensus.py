"""Tests for the /api/v1/markets/{slug}/consensus endpoint.

This endpoint wraps ``db.calculate_betyc_probability`` and caches the payload
at ``credibility_consensus:{slug}`` with a 60-second TTL. The invalidation
facade already drops the key when new predictions land or a market resolves;
this test focuses on the HTTP contract:

* 401 without an API key.
* 404 when no predictions exist for the slug.
* 200 with the expected JSON shape when predictions exist.
* A second call within the TTL hits the cache (factory runs only once).
* ``ttl_invalidate.on_new_prediction`` drops the cached entry.

Uses the shared in-memory ``_testdb`` bootstrap so schema + migrations
are already applied.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("GLOBAL_RATE_LIMIT_PER_MIN", "10000")

from tests import _testdb  # noqa: F401 — shared DB + migrations

import db  # noqa: E402
import server  # noqa: F401,E402 — registers the v1 router
from fastapi.testclient import TestClient  # noqa: E402

from cache import ttl_cache, ttl_invalidate  # noqa: E402


client = TestClient(server.app)


def _issue_key() -> str:
    """Create a user + bearer key and return the raw key string."""
    import api_v1
    uid = db.create_user(
        "consensus-tester@test.local", "TestPass123!", username="consensustester",
    )
    raw_key, _ = api_v1.create_api_key(uid, name="consensus test", tier="standard")
    return raw_key


class TestConsensusAuth(unittest.TestCase):
    def test_401_without_key(self):
        r = client.get("/api/v1/markets/some-slug/consensus")
        self.assertIn(r.status_code, (401, 403))


class TestConsensusShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = _issue_key()
        cls.headers = {"Authorization": f"Bearer {cls.key}"}
        # Reset any stray cached state from other tests.
        ttl_cache.clear()

    def setUp(self):
        ttl_cache.clear()

    def _fake_predictions(self, slug: str) -> list[dict]:
        """A deterministic stand-in for db.get_predictions_for_market."""
        return [
            {
                "source_handle": "fedwatcher",
                "direction": "YES",
                "predicted_probability": 0.72,
                "global_credibility": 0.81,
                "category_credibility": 0.78,
                "accuracy_unlocked": True,
            },
            {
                "source_handle": "marketskeptic",
                "direction": "NO",
                "predicted_probability": 0.35,
                "global_credibility": 0.64,
                "category_credibility": None,
                "accuracy_unlocked": False,
            },
        ]

    def _fake_pred_rows(self, slug: str):
        """Wrap dicts as sqlite3.Row-like objects — api_v1 uses ``row["field"]``
        and ``"field" in row.keys()``, so we expose both."""
        class _Row(dict):
            def keys(self):
                return list(super().keys())
        return [_Row(d) for d in self._fake_predictions(slug)]

    def test_404_when_no_predictions(self):
        with patch.object(db, "get_predictions_for_market", return_value=[]):
            r = client.get(
                "/api/v1/markets/nonexistent-market/consensus",
                headers=self.headers,
            )
        self.assertEqual(r.status_code, 404)

    def test_200_with_predictions(self):
        slug = "will-fed-hold-jan-2027"
        with patch.object(db, "get_predictions_for_market",
                          return_value=self._fake_pred_rows(slug)):
            r = client.get(
                f"/api/v1/markets/{slug}/consensus",
                headers=self.headers,
            )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in (
            "slug",
            "betyc_yes_probability",
            "betyc_no_probability",
            "betyc_source_count",
            "betyc_confidence",
            "avg_source_credibility",
            "cached_for_seconds",
        ):
            self.assertIn(key, body, f"{key} missing from response")
        self.assertEqual(body["slug"], slug)
        self.assertEqual(body["betyc_source_count"], 2)
        # Avg credibility: (0.81 + 0.64) / 2 = 0.725
        self.assertAlmostEqual(body["avg_source_credibility"], 0.725, places=3)

    def test_cache_hit_on_second_call(self):
        slug = "cache-hit-market"
        calls = {"n": 0}

        def counting_predictions(_slug):
            calls["n"] += 1
            return self._fake_pred_rows(_slug)

        with patch.object(db, "get_predictions_for_market", side_effect=counting_predictions):
            r1 = client.get(f"/api/v1/markets/{slug}/consensus", headers=self.headers)
            r2 = client.get(f"/api/v1/markets/{slug}/consensus", headers=self.headers)

        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        # Same JSON payload.
        self.assertEqual(r1.json(), r2.json())
        # Factory ran exactly once — second call was served from the cache.
        self.assertEqual(calls["n"], 1)

    def test_invalidation_on_new_prediction_drops_cache(self):
        slug = "invalidation-test-market"
        with patch.object(db, "get_predictions_for_market",
                          return_value=self._fake_pred_rows(slug)):
            client.get(f"/api/v1/markets/{slug}/consensus", headers=self.headers)

        # Key should exist right after the first call.
        self.assertIsNotNone(ttl_cache.get(f"credibility_consensus:{slug}"))

        # Simulate a new prediction landing for this market.
        ttl_invalidate.on_new_prediction("fedwatcher", slug)
        self.assertIsNone(ttl_cache.get(f"credibility_consensus:{slug}"))

    def test_invalidation_on_market_resolved_drops_cache(self):
        slug = "market-resolve-test"
        with patch.object(db, "get_predictions_for_market",
                          return_value=self._fake_pred_rows(slug)):
            client.get(f"/api/v1/markets/{slug}/consensus", headers=self.headers)

        self.assertIsNotNone(ttl_cache.get(f"credibility_consensus:{slug}"))
        ttl_invalidate.on_market_resolved(slug)
        self.assertIsNone(ttl_cache.get(f"credibility_consensus:{slug}"))


if __name__ == "__main__":
    unittest.main()

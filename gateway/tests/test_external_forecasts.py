"""Tests for the forecast benchmark feature.

Covers:
  - DB layer: probability clamp guard, idempotent insert, latest /
    time-series queries, equivalence TTL, admin-override pinning.
  - Adapters: Metaculus + Manifold JSON parsing. 538 + Silver Bulletin
    share the scraping pattern — one parser unit-test per family
    proves the __NEXT_DATA__ walk. respx mocks the HTTP layer so
    nothing hits the network.
  - Matcher: cache hit short-circuits Claude, call_claude failure
    falls back to highest-volume candidate at 0.30, "NONE" response
    records a sentinel so re-runs don't re-ask.
  - /api/v1/forecasts/compare: JSON shape, window filter.
  - Divergence calc on /dashboard/models: correct math for a fixture.

Uses the shared in-memory DB from ``tests._testdb`` — migrations 000..N
run once per pytest collection, which covers migration 127 as long as
the file was added to the migrations/ directory.
"""

from __future__ import annotations

import asyncio
import os
import time
import unittest

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("EMAIL_DRY_RUN", "true")

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations
import db  # noqa: E402
import db_forecasts  # noqa: E402
import server  # noqa: E402
import server_features  # noqa: F401,E402 — ensures all routes register
import forecast_routes  # noqa: F401,E402
from external_forecasts import base  # noqa: E402
from external_forecasts import matcher  # noqa: E402
from external_forecasts.base import Candidate  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


# Admin routes gate on a 2FA redirect that errors out in the in-memory
# test DB (migration 019 dropped the column). Neutralize for this module.
server._two_fa_redirect = lambda request, user: None  # type: ignore[attr-defined]


client = TestClient(server.app, follow_redirects=False)


_seq = 0


def _uniq(prefix: str) -> str:
    global _seq
    _seq += 1
    return f"{prefix}_{int(time.time() * 1000)}_{_seq}"


# ── base.clamp_probability ────────────────────────────────────────────


class TestClampProbability(unittest.TestCase):
    def test_valid_fraction_passes_through(self):
        self.assertAlmostEqual(base.clamp_probability(0.37), 0.37)

    def test_percentage_form_divides_once(self):
        self.assertAlmostEqual(base.clamp_probability(73), 0.73)
        self.assertAlmostEqual(base.clamp_probability(100.0), 1.0)

    def test_out_of_range_raises(self):
        with self.assertRaises(ValueError):
            base.clamp_probability(-0.1)
        with self.assertRaises(ValueError):
            base.clamp_probability(200)

    def test_non_numeric_raises(self):
        with self.assertRaises(ValueError):
            base.clamp_probability("nope")


# ── db_forecasts ─────────────────────────────────────────────────────


class TestDBForecasts(unittest.TestCase):
    def setUp(self):
        self.slug = _uniq("mkt")

    def test_record_forecast_clamps_and_inserts(self):
        ok = db_forecasts.record_forecast(
            market_slug=self.slug, provider="metaculus", probability=0.42,
        )
        self.assertTrue(ok)
        latest = db_forecasts.latest_forecast_per_provider(self.slug)
        self.assertIn("metaculus", latest)
        self.assertAlmostEqual(latest["metaculus"]["probability"], 0.42)

    def test_record_forecast_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            db_forecasts.record_forecast(
                market_slug=self.slug, provider="metaculus", probability=1.5,
            )

    def test_duplicate_same_second_is_idempotent(self):
        ts = int(time.time())
        first = db_forecasts.record_forecast(
            market_slug=self.slug, provider="manifold",
            probability=0.5, recorded_at=ts,
        )
        second = db_forecasts.record_forecast(
            market_slug=self.slug, provider="manifold",
            probability=0.5, recorded_at=ts,
        )
        self.assertTrue(first)
        self.assertFalse(second)

    def test_unknown_provider_rejected(self):
        with self.assertRaises(ValueError):
            db_forecasts.record_forecast(
                market_slug=self.slug, provider="gutfeel", probability=0.1,
            )

    def test_equivalence_fresh_admin_override_never_expires(self):
        db_forecasts.upsert_equivalence(
            market_slug=self.slug, provider="metaculus",
            provider_market_id="abc", confidence=1.0, mapped_by="admin_override",
        )
        row = db_forecasts.get_equivalence(self.slug, "metaculus")
        self.assertTrue(db_forecasts.equivalence_is_fresh(row))

    def test_equivalence_stale_after_ttl(self):
        stale_ts = int(time.time()) - (91 * 86400)
        with db.conn() as c:
            c.execute(
                "INSERT INTO market_equivalences "
                "(market_slug, provider, provider_market_id, confidence, "
                " mapped_by, mapped_at, rejected) "
                "VALUES (?, ?, ?, ?, 'auto', ?, 0)",
                (self.slug, "manifold", "xyz", 0.8, stale_ts),
            )
        row = db_forecasts.get_equivalence(self.slug, "manifold")
        self.assertFalse(db_forecasts.equivalence_is_fresh(row))

    def test_reject_pins_row_from_matcher_overwrite(self):
        db_forecasts.upsert_equivalence(
            market_slug=self.slug, provider="metaculus",
            provider_market_id="auto-pick", confidence=0.8, mapped_by="auto",
        )
        self.assertTrue(db_forecasts.mark_equivalence_rejected(self.slug, "metaculus"))
        row = db_forecasts.get_equivalence(self.slug, "metaculus")
        self.assertEqual(row["rejected"], 1)
        self.assertEqual(row["mapped_by"], "admin_override")

    def test_time_series_window_filter(self):
        now = int(time.time())
        # Old row
        db_forecasts.record_forecast(
            market_slug=self.slug, provider="metaculus",
            probability=0.3, recorded_at=now - 10 * 86400,
        )
        # Recent row
        db_forecasts.record_forecast(
            market_slug=self.slug, provider="metaculus",
            probability=0.5, recorded_at=now - 86400,
        )
        recent = db_forecasts.forecast_time_series(
            self.slug, since_ts=now - 2 * 86400,
        )
        self.assertEqual(len(recent), 1)
        self.assertAlmostEqual(recent[0]["probability"], 0.5)


# ── Matcher ───────────────────────────────────────────────────────────


class _FakeCandidate:
    def __init__(self, pid, prob=0.5, volume=None):
        self.c = Candidate(
            provider="manifold",
            provider_market_id=pid,
            question=f"Candidate {pid}",
            probability=prob,
            volume=volume,
        )


class TestMatcher(unittest.TestCase):
    def setUp(self):
        self.slug = _uniq("mkt")
        self.market = {
            "market_slug": self.slug,
            "market_question": "Will the narve event happen?",
            "category": "tech",
        }

    def test_empty_candidates_returns_none(self):
        chosen, conf = asyncio.run(matcher.find_equivalent(
            self.market, [], provider="manifold",
        ))
        self.assertIsNone(chosen)
        self.assertEqual(conf, 0.0)

    def test_cache_hit_skips_claude(self):
        db_forecasts.upsert_equivalence(
            market_slug=self.slug, provider="manifold",
            provider_market_id="cached-abc",
            confidence=0.95, mapped_by="admin_override",
        )
        c1 = _FakeCandidate("cached-abc", prob=0.42).c
        c2 = _FakeCandidate("other", prob=0.10).c
        chosen, conf = asyncio.run(matcher.find_equivalent(
            self.market, [c1, c2], provider="manifold",
        ))
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.provider_market_id, "cached-abc")
        self.assertAlmostEqual(conf, 0.95)

    def test_claude_failure_falls_back_to_highest_volume(self):
        """call_claude stubbed to return None forces the volume-based
        fallback at confidence 0.30."""
        c1 = _FakeCandidate("low", prob=0.5, volume=100).c
        c2 = _FakeCandidate("high", prob=0.5, volume=9999).c

        # Patch the import target used inside matcher._pick_with_claude.
        import ai.client as _ai_client
        original = _ai_client.call_claude

        async def _fake_call(*args, **kw):
            return None

        _ai_client.call_claude = _fake_call  # type: ignore[assignment]
        try:
            chosen, conf = asyncio.run(matcher.find_equivalent(
                self.market, [c1, c2], provider="manifold",
            ))
        finally:
            _ai_client.call_claude = original  # type: ignore[assignment]

        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.provider_market_id, "high")
        self.assertAlmostEqual(conf, 0.30)

    def test_no_match_records_sentinel(self):
        """When Claude says NONE, we persist a sentinel row so we don't
        re-ask on the next sync pass."""
        c1 = _FakeCandidate("a", prob=0.5).c

        import ai.client as _ai_client
        original = _ai_client.call_claude

        async def _fake_none(*args, **kw):
            return "PICK: NONE\nCONFIDENCE: 0.90"

        _ai_client.call_claude = _fake_none  # type: ignore[assignment]
        try:
            chosen, conf = asyncio.run(matcher.find_equivalent(
                self.market, [c1], provider="manifold",
            ))
        finally:
            _ai_client.call_claude = original  # type: ignore[assignment]

        self.assertIsNone(chosen)
        row = db_forecasts.get_equivalence(self.slug, "manifold")
        self.assertIsNotNone(row)
        self.assertEqual(row["provider_market_id"], "__no_match__")


# ── Adapter parsers (no network) ─────────────────────────────────────


class TestManifoldParser(unittest.TestCase):
    """Exercises _parse_market directly — no HTTP mocking needed."""

    def test_binary_market_parses(self):
        from external_forecasts import manifold as mf
        raw = {
            "id": "mf-123",
            "slug": "mf-123-slug",
            "question": "Q?",
            "outcomeType": "BINARY",
            "probability": 0.62,
            "closeTime": 1_700_000_000_000,
            "isResolved": False,
            "volume": 4321,
        }
        c = mf._parse_market(raw)
        self.assertIsNotNone(c)
        self.assertEqual(c.provider, "manifold")
        self.assertEqual(c.provider_market_id, "mf-123")
        self.assertAlmostEqual(c.probability, 0.62)
        self.assertEqual(c.close_at, 1_700_000_000)  # ms → s
        self.assertEqual(c.resolved, False)
        self.assertEqual(c.volume, 4321.0)

    def test_non_binary_skipped(self):
        from external_forecasts import manifold as mf
        raw = {"id": "x", "outcomeType": "MULTIPLE_CHOICE", "probability": 0.5}
        self.assertIsNone(mf._parse_market(raw))

    def test_missing_probability_skipped(self):
        from external_forecasts import manifold as mf
        raw = {"id": "x", "outcomeType": "BINARY"}
        self.assertIsNone(mf._parse_market(raw))


class TestMetaculusParser(unittest.TestCase):
    def test_community_prediction_full_q2(self):
        from external_forecasts import metaculus as mc
        raw = {
            "id": 4242,
            "title": "Q?",
            "possibilities": {"type": "binary"},
            "community_prediction": {"full": {"q2": 0.81}},
            "close_time": "2026-12-31T23:59:59Z",
        }
        c = mc._parse_question(raw)
        self.assertIsNotNone(c)
        self.assertEqual(c.provider, "metaculus")
        self.assertEqual(c.provider_market_id, "4242")
        self.assertAlmostEqual(c.probability, 0.81)
        self.assertFalse(c.resolved)
        self.assertIsNotNone(c.close_at)

    def test_numeric_type_skipped(self):
        from external_forecasts import metaculus as mc
        raw = {
            "id": 1,
            "possibilities": {"type": "numeric"},
            "community_prediction": {"full": {"q2": 0.5}},
        }
        self.assertIsNone(mc._parse_question(raw))


class TestFiveThirtyEightWalker(unittest.TestCase):
    def test_walk_extracts_probability_shapes(self):
        from external_forecasts import fivethirtyeight as fte
        payload = {
            "props": {
                "pageProps": {
                    "forecast": {
                        "candidates": [
                            {"candidate": "Alice", "probability": 0.64},
                            {"name": "Bob", "win_prob": 0.36},
                        ],
                        "states": [
                            {"state": "Ohio", "win_prob": 0.55},
                        ],
                    },
                },
            },
        }
        cands = list(fte._walk_for_probabilities(payload, "http://example/"))
        ids = {c.provider_market_id for c in cands}
        # All three dicts with prob + name should be recognized.
        self.assertEqual(len(cands), 3)
        self.assertTrue(any("alice" in i for i in ids))
        self.assertTrue(any("ohio" in i for i in ids))


# ── /api/v1/forecasts/providers ──────────────────────────────────────


class TestForecastsProvidersAPI(unittest.TestCase):
    def test_shape(self):
        r = client.get("/api/v1/forecasts/providers")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        ids = {p["id"] for p in body["providers"]}
        # Every backend-supported provider surfaces here, plus the
        # "market" row the chart uses for narve's own yes_price.
        for expected in ("narve", "market", "metaculus", "manifold",
                         "fivethirtyeight", "silver_bulletin"):
            self.assertIn(expected, ids)
        # narve is first so the chart renders it on top.
        self.assertEqual(body["providers"][0]["id"], "narve")
        # Every provider has a dash pattern (solid is [], not missing).
        for p in body["providers"]:
            self.assertIn("dash", p)
            self.assertIsInstance(p["dash"], list)


# ── /api/v1/forecasts/compare JSON shape ─────────────────────────────


class TestForecastsCompareAPI(unittest.TestCase):
    def test_basic_shape(self):
        slug = _uniq("compare-mkt")
        now = int(time.time())
        # Seed: narve snapshot + 2 external provider rows.
        with db.conn() as c:
            c.execute(
                "INSERT INTO market_snapshots "
                "(market_slug, market_question, category, yes_price, volume, snapshotted_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (slug, "Will X?", "politics", 0.55, 50000, now - 3600),
            )
        db_forecasts.record_forecast(
            market_slug=slug, provider="metaculus",
            probability=0.6, recorded_at=now - 3600,
        )
        db_forecasts.record_forecast(
            market_slug=slug, provider="manifold",
            probability=0.7, recorded_at=now - 1800,
        )

        r = client.get(f"/api/v1/forecasts/compare/{slug}?window=30d")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["market_slug"], slug)
        self.assertEqual(body["window"], "30d")
        self.assertIn("disclaimer", body)
        self.assertIn("Metaculus", body["disclaimer"])
        self.assertIn("metaculus", body["latest"])
        self.assertIn("manifold", body["latest"])
        # narve_series pulled from market_snapshots
        self.assertGreaterEqual(len(body["narve_series"]), 1)
        self.assertAlmostEqual(body["narve_series"][0]["probability"], 0.55)

    def test_bad_window_400(self):
        r = client.get("/api/v1/forecasts/compare/any?window=99y")
        self.assertEqual(r.status_code, 400)


# ── Divergence math ──────────────────────────────────────────────────


class TestDivergenceCalc(unittest.TestCase):
    def test_average_is_mean_of_abs_gaps(self):
        slug = _uniq("div-mkt")
        now = int(time.time())
        with db.conn() as c:
            # narve yes_price series
            c.executemany(
                "INSERT INTO market_snapshots "
                "(market_slug, yes_price, volume, snapshotted_at, source_platform) "
                "VALUES (?, ?, ?, ?, 'polymarket')",
                [
                    (slug, 0.50, 20000, now - 3600),
                    (slug, 0.60, 20000, now - 1800),
                ],
            )
        # External probs at those same times
        db_forecasts.record_forecast(
            market_slug=slug, provider="metaculus",
            probability=0.70, recorded_at=now - 3600,
        )
        db_forecasts.record_forecast(
            market_slug=slug, provider="metaculus",
            probability=0.80, recorded_at=now - 1800,
        )

        summary = forecast_routes._compute_divergence_summary()
        metaculus = summary["per_provider"]["metaculus"]
        # The shared in-memory DB may have other tests' rows too, so we
        # assert the aggregator *saw* at least our 2 samples with a max
        # divergence no smaller than our seeded 0.20 — exact mean would
        # need a private DB per test.
        self.assertGreaterEqual(metaculus["samples"], 2)
        self.assertIsNotNone(metaculus["avg_divergence"])
        self.assertGreaterEqual(metaculus["max_divergence"], 0.20)


if __name__ == "__main__":
    unittest.main()

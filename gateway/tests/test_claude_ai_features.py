"""Tests for the Claude-backed intelligence features (migrations 022-025):

  - claude_usage_log: cost accounting helpers + pricing table
  - prediction_extractor: cache hits, JSON parsing, stub fallback
  - categoriser: cache hits + keyword fallback
  - source_summary: low-data graceful fallback, cache respect

The Anthropic SDK is never called — every module exposes a `_call_claude`
that tests monkey-patch with a fixture response or a failure stub.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import sys
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
import migrations
from intelligence import claude_usage
from intelligence import prediction_extractor as extractor
from intelligence import categoriser
from intelligence import source_summary


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── In-memory DB with every migration applied ───────────────────────────────


class _DbFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._test_conn = sqlite3.connect(":memory:")
        cls._test_conn.row_factory = sqlite3.Row
        cls._test_conn.execute("PRAGMA foreign_keys = ON")

        @contextlib.contextmanager
        def test_conn():
            try:
                yield cls._test_conn
                cls._test_conn.commit()
            except Exception:
                cls._test_conn.rollback()
                raise

        cls._orig_conn = db.conn
        db.conn = test_conn
        db.init_db()
        migrations.upgrade_to_head()

    @classmethod
    def tearDownClass(cls):
        db.conn = cls._orig_conn
        cls._test_conn.close()

    def setUp(self):
        with db.conn() as c:
            c.execute("DELETE FROM claude_usage_log")
            c.execute("DELETE FROM prediction_extractions")
            c.execute("DELETE FROM predictions_reextracted")
            c.execute("DELETE FROM market_categorisations")
            c.execute("DELETE FROM source_summaries")


# ── Migrations ──────────────────────────────────────────────────────────────


class TestMigrations(_DbFixture):
    def test_claude_usage_log_table_exists(self):
        with db.conn() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(claude_usage_log)")}
        for required in ("timestamp", "feature", "model", "input_tokens",
                         "output_tokens", "cost_usd", "cached_hit"):
            self.assertIn(required, cols)

    def test_prediction_extractions_table_exists(self):
        with db.conn() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(prediction_extractions)")}
        for required in ("post_hash", "schema_version", "is_prediction", "claim",
                         "direction", "explicit_probability", "implicit_confidence",
                         "time_frame", "category", "contains_sarcasm",
                         "is_conditional", "raw_payload"):
            self.assertIn(required, cols)

    def test_predictions_reextracted_table_exists(self):
        with db.conn() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(predictions_reextracted)")}
        for required in ("original_prediction_id", "matches_original", "diff_summary"):
            self.assertIn(required, cols)

    def test_market_categorisations_table_exists(self):
        with db.conn() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(market_categorisations)")}
        for required in ("market_id", "primary_category", "sub_category", "tags",
                         "political_leaning", "sensitivity",
                         "insider_trading_relevant", "environmental_relevant",
                         "requires_expert_knowledge"):
            self.assertIn(required, cols)

    def test_source_summaries_table_exists(self):
        with db.conn() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(source_summaries)")}
        for required in ("source_handle", "summary", "generated_at",
                         "cache_valid_until", "predictions_considered"):
            self.assertIn(required, cols)


# ── claude_usage module ─────────────────────────────────────────────────────


class TestClaudeUsage(_DbFixture):
    def test_cost_for_haiku_matches_published_rate(self):
        # $0.25/M input + $1.25/M output for Haiku 4.5.
        cost = claude_usage.cost_for("claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 1.50, places=4)

    def test_cost_for_unknown_model_returns_zero(self):
        self.assertEqual(
            claude_usage.cost_for("claude-mystery-99", 1_000_000, 1_000_000),
            0.0,
        )

    def test_log_response_records_tokens_and_cost(self):
        resp = SimpleNamespace(usage=SimpleNamespace(input_tokens=200, output_tokens=100))
        claude_usage.log_response(
            feature="extraction",
            model="claude-haiku-4-5-20251001",
            response=resp,
        )
        rollup = db.claude_usage_daily_rollup(days=1)
        self.assertEqual(len(rollup), 1)
        row = rollup[0]
        self.assertEqual(row["feature"], "extraction")
        self.assertEqual(row["input_tokens"], 200)
        self.assertEqual(row["output_tokens"], 100)
        self.assertGreater(row["cost_usd"], 0)

    def test_log_response_cache_hit_has_zero_tokens(self):
        claude_usage.log_response(
            feature="extraction",
            model="claude-haiku-4-5-20251001",
            response=None,
            cached_hit=True,
        )
        rollup = db.claude_usage_daily_rollup(days=1)
        self.assertEqual(len(rollup), 1)
        self.assertEqual(rollup[0]["input_tokens"], 0)
        self.assertEqual(rollup[0]["cache_hits"], 1)
        self.assertEqual(rollup[0]["cost_usd"], 0.0)

    def test_daily_rollup_groups_by_feature(self):
        for feature in ("extraction", "categorisation", "summarisation"):
            resp = SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=50))
            claude_usage.log_response(feature=feature, model="claude-haiku-4-5", response=resp)
        rollup = db.claude_usage_daily_rollup(days=1)
        features = {r["feature"] for r in rollup}
        self.assertEqual(features, {"extraction", "categorisation", "summarisation"})


# ── Prediction extractor ────────────────────────────────────────────────────


class TestExtractor(_DbFixture):
    def _patch_claude(self, text_response):
        async def _stub(content, handle):
            return (
                text_response,
                SimpleNamespace(usage=SimpleNamespace(input_tokens=50, output_tokens=30)),
            )
        extractor._call_claude = _stub

    def setUp(self):
        super().setUp()
        async def _none(*a, **kw):
            return None, None
        extractor._call_claude = _none

    def test_clear_prediction_extracted(self):
        self._patch_claude(json.dumps([{
            "claim": "Trump wins the 2028 Republican primary",
            "direction": "yes",
            "explicit_probability": 0.72,
            "implicit_confidence": "high",
            "time_frame": "by 2028",
            "category": "politics",
            "contains_sarcasm": False,
            "is_conditional": False,
        }]))
        result = _run(extractor.extract_predictions_from_post({
            "content": "Trump will win the 2028 primary, 72% likely",
            "author_handle": "pundit",
        }))
        self.assertTrue(result["is_prediction"])
        self.assertEqual(result["direction"], "yes")
        self.assertAlmostEqual(result["explicit_probability"], 0.72)
        self.assertEqual(result["category"], "politics")

    def test_sarcasm_still_extracts_flag(self):
        self._patch_claude(json.dumps([{
            "claim": "Ethereum will hit zero",
            "direction": "yes",
            "explicit_probability": None,
            "implicit_confidence": "low",
            "time_frame": None,
            "category": "crypto",
            "contains_sarcasm": True,
            "is_conditional": False,
        }]))
        result = _run(extractor.extract_predictions_from_post({
            "content": "Oh yeah, ETH is TOTALLY going to zero any day now",
            "author_handle": "snark",
        }))
        self.assertTrue(result["contains_sarcasm"])

    def test_empty_array_means_not_a_prediction(self):
        self._patch_claude("[]")
        result = _run(extractor.extract_predictions_from_post({
            "content": "What do you think about the market?",
            "author_handle": "anon",
        }))
        self.assertFalse(result["is_prediction"])
        self.assertIsNone(result["claim"])

    def test_invalid_json_falls_back_to_not_prediction(self):
        self._patch_claude("not json at all {{{{")
        result = _run(extractor.extract_predictions_from_post({
            "content": "Market go up",
            "author_handle": "anon",
        }))
        self.assertFalse(result["is_prediction"])

    def test_explicit_probability_out_of_range_is_nulled(self):
        self._patch_claude(json.dumps([{
            "claim": "BTC 200k",
            "direction": "yes",
            "explicit_probability": 2.5,  # invalid — must become None
            "implicit_confidence": "medium",
            "time_frame": "2026",
            "category": "crypto",
            "contains_sarcasm": False,
            "is_conditional": False,
        }]))
        result = _run(extractor.extract_predictions_from_post({
            "content": "BTC to 200k by 2026",
            "author_handle": "bull",
        }))
        self.assertTrue(result["is_prediction"])
        self.assertIsNone(result["explicit_probability"])

    def test_cache_hit_skips_claude_on_second_call(self):
        call_count = [0]
        async def _counting(content, handle):
            call_count[0] += 1
            return (
                json.dumps([{
                    "claim": "X happens", "direction": "yes",
                    "explicit_probability": None, "implicit_confidence": "high",
                    "time_frame": None, "category": "other",
                    "contains_sarcasm": False, "is_conditional": False,
                }]),
                SimpleNamespace(usage=SimpleNamespace(input_tokens=10, output_tokens=5)),
            )
        extractor._call_claude = _counting

        post = {"content": "X will happen", "author_handle": "seer"}
        _run(extractor.extract_predictions_from_post(post))
        _run(extractor.extract_predictions_from_post(post))
        _run(extractor.extract_predictions_from_post(post))
        self.assertEqual(call_count[0], 1)

    def test_cache_hit_logs_cached_row(self):
        self._patch_claude(json.dumps([{
            "claim": "Y", "direction": "no",
            "explicit_probability": None, "implicit_confidence": "medium",
            "time_frame": None, "category": "finance",
            "contains_sarcasm": False, "is_conditional": False,
        }]))
        post = {"content": "Y won't happen", "author_handle": "bear"}
        _run(extractor.extract_predictions_from_post(post))
        _run(extractor.extract_predictions_from_post(post))
        rollup = db.claude_usage_daily_rollup(days=1)
        extraction_rows = [r for r in rollup if r["feature"] == "extraction"]
        self.assertEqual(sum(r["calls"] for r in extraction_rows), 2)
        self.assertEqual(sum(r["cache_hits"] for r in extraction_rows), 1)

    def test_conditional_prediction_flagged(self):
        self._patch_claude(json.dumps([{
            "claim": "If Fed cuts, markets rally",
            "direction": "yes",
            "explicit_probability": None,
            "implicit_confidence": "medium",
            "time_frame": "by Q4",
            "category": "finance",
            "contains_sarcasm": False,
            "is_conditional": True,
        }]))
        result = _run(extractor.extract_predictions_from_post({
            "content": "If the Fed cuts rates, markets rally by Q4",
            "author_handle": "wonk",
        }))
        self.assertTrue(result["is_conditional"])

    def test_claude_unavailable_caches_stub(self):
        async def _none(content, handle):
            return None, None
        extractor._call_claude = _none
        result = _run(extractor.extract_predictions_from_post({
            "content": "X will Y", "author_handle": "anon",
        }))
        self.assertFalse(result["is_prediction"])
        # The stub is cached so a retry doesn't ping Claude.
        cached = db.get_prediction_extraction(extractor.content_hash("X will Y"))
        self.assertIsNotNone(cached)


# ── Categoriser ─────────────────────────────────────────────────────────────


class TestCategoriser(_DbFixture):
    def _patch_claude(self, text_response):
        async def _stub(market_title):
            return (
                text_response,
                SimpleNamespace(usage=SimpleNamespace(input_tokens=60, output_tokens=40)),
            )
        categoriser._call_claude = _stub

    def setUp(self):
        super().setUp()
        async def _none(*a, **kw):
            return None, None
        categoriser._call_claude = _none

    def test_valid_response_persists_and_returns(self):
        self._patch_claude(json.dumps({
            "primary_category": "finance",
            "sub_category": "fed_rates",
            "tags": ["fed", "rates", "macro", "inflation", "cpi"],
            "political_leaning": "n_a",
            "sensitivity": "normal",
            "relevance_signals": {
                "insider_trading_relevant": False,
                "environmental_impact_relevant": False,
                "requires_expert_knowledge": True,
            },
        }))
        market = SimpleNamespace(id="poly:fed-cut-2026", title="Will the Fed cut rates in 2026?")
        result = _run(categoriser.categorise_market(market))
        self.assertEqual(result["primary_category"], "finance")
        self.assertEqual(result["sub_category"], "fed_rates")
        self.assertTrue(result["requires_expert_knowledge"])
        self.assertIn("fed", result["tags"])

    def test_cache_prevents_duplicate_calls(self):
        call_count = [0]
        async def _counting(market_title):
            call_count[0] += 1
            return (
                json.dumps({
                    "primary_category": "sports", "sub_category": "nba",
                    "tags": ["nba"], "political_leaning": "n_a", "sensitivity": "normal",
                    "relevance_signals": {},
                }),
                SimpleNamespace(usage=SimpleNamespace(input_tokens=50, output_tokens=30)),
            )
        categoriser._call_claude = _counting

        market = SimpleNamespace(id="poly:lakers", title="Lakers win 2026 championship?")
        _run(categoriser.categorise_market(market))
        _run(categoriser.categorise_market(market))
        _run(categoriser.categorise_market(market))
        self.assertEqual(call_count[0], 1)

    def test_invalid_category_coerced_to_other(self):
        self._patch_claude(json.dumps({
            "primary_category": "aliens",  # not in VALID_PRIMARY
            "sub_category": None,
            "tags": [],
            "political_leaning": "wild",  # invalid
            "sensitivity": "normal",
            "relevance_signals": {},
        }))
        market = SimpleNamespace(id="poly:x", title="Something weird")
        result = _run(categoriser.categorise_market(market))
        self.assertEqual(result["primary_category"], "other")
        self.assertEqual(result["political_leaning"], "n_a")

    def test_claude_unavailable_uses_keyword_fallback(self):
        async def _none(*a, **kw):
            return None, None
        categoriser._call_claude = _none
        market = SimpleNamespace(id="poly:btc", title="Will Bitcoin hit 100k?")
        result = _run(categoriser.categorise_market(market))
        # Keyword matcher recognizes "bitcoin" → crypto
        self.assertEqual(result["primary_category"], "crypto")

    def test_lookup_cached_category_synchronous(self):
        self._patch_claude(json.dumps({
            "primary_category": "crypto", "sub_category": "btc",
            "tags": ["btc"], "political_leaning": "n_a", "sensitivity": "normal",
            "relevance_signals": {},
        }))
        market = SimpleNamespace(id="poly:btc-100k", title="BTC hits 100k by end of year?")
        _run(categoriser.categorise_market(market))
        # Synchronous lookup — no Claude call even if caller forgets.
        self.assertEqual(categoriser.lookup_cached_category("poly:btc-100k"), "crypto")
        self.assertIsNone(categoriser.lookup_cached_category("poly:uncached"))


# ── Source summary ──────────────────────────────────────────────────────────


class TestSourceSummary(_DbFixture):
    def _patch_claude(self, text_response):
        async def _stub(user_message):
            return (
                text_response,
                SimpleNamespace(usage=SimpleNamespace(input_tokens=400, output_tokens=120)),
            )
        source_summary._call_claude = _stub

    def setUp(self):
        super().setUp()
        async def _none(*a, **kw):
            return None, None
        source_summary._call_claude = _none

    def _seed_rated_source(self, handle, total=20, correct=14, global_cred=0.71):
        """Put a rated source in the DB with enough predictions to trigger
        a real summary call. Idempotent per handle."""
        now = int(time.time())
        with db.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO source_credibility "
                "(source_handle, global_credibility, accuracy_unlocked, "
                " total_predictions, correct_predictions, categories_active, "
                " last_computed_at) VALUES (?,?,?,?,?,?,?)",
                (handle, global_cred, 1, total, correct, 2, now),
            )
            for i in range(total):
                c.execute(
                    "INSERT INTO predictions "
                    "(source_handle, market_id, category, direction, content, "
                    " extracted_at, resolved, resolved_correct) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        handle, None, "finance", "YES",
                        f"Fed decision prediction {i}",
                        now - i * 3600,
                        1 if i < correct else 0,
                        1 if i < correct else 0,
                    ),
                )

    def test_source_with_no_data_returns_graceful_fallback(self):
        result = _run(source_summary.generate_source_summary("ghosthandle"))
        self.assertIn("not yet made enough", result["summary"])
        self.assertEqual(result["generated_by"], "fallback_no_data")

    def test_low_data_source_skips_claude(self):
        self._seed_rated_source("smallsource", total=3, correct=2)
        called = [0]
        async def _counting(*a, **kw):
            called[0] += 1
            return "should not be called", SimpleNamespace()
        source_summary._call_claude = _counting
        result = _run(source_summary.generate_source_summary("smallsource"))
        self.assertEqual(called[0], 0)
        self.assertEqual(result["generated_by"], "fallback_low_data")

    def test_full_source_generates_and_caches(self):
        self._seed_rated_source("fedwatcher", total=47, correct=33)
        self._patch_claude(
            "@fedwatcher is a strong source on Fed decisions with "
            "71% accuracy across 47 tracked predictions."
        )
        first = _run(source_summary.generate_source_summary("fedwatcher"))
        self.assertIn("@fedwatcher", first["summary"])

        # Second call should hit the cache.
        call_count = [0]
        async def _counting(user_message):
            call_count[0] += 1
            return "different text", SimpleNamespace()
        source_summary._call_claude = _counting
        second = _run(source_summary.generate_source_summary("fedwatcher"))
        self.assertEqual(call_count[0], 0)
        self.assertEqual(second["summary"], first["summary"])

    def test_claude_failure_uses_fallback(self):
        self._seed_rated_source("unreachable", total=25, correct=18)
        async def _fails(*a, **kw):
            return None, None
        source_summary._call_claude = _fails
        result = _run(source_summary.generate_source_summary("unreachable"))
        self.assertEqual(result["generated_by"], "fallback_claude_unavailable")


# ── Backfill diff + switchover ──────────────────────────────────────────────


class TestBackfill(_DbFixture):
    def test_switchover_updates_originals_and_clears_staging(self):
        # Original prediction
        now = int(time.time())
        with db.conn() as c:
            cur = c.execute(
                "INSERT INTO predictions "
                "(source_handle, market_id, category, direction, "
                " predicted_probability, content, extracted_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("handle1", None, "other", "YES", 0.5, "Original claim", now),
            )
            orig_id = cur.lastrowid

        db.insert_reextracted_prediction({
            "original_prediction_id": orig_id,
            "source_handle": "handle1",
            "market_id": None,
            "category": "finance",
            "direction": "YES",
            "predicted_probability": 0.72,
            "content": "Original claim",
            "claim": "Reworded claim",
            "extracted_at": now,
            "matches_original": False,
            "diff_summary": "category: other → finance; probability: 0.5 → 0.72",
        })

        diff = db.reextraction_diff_summary()
        self.assertEqual(diff["total"], 1)
        self.assertEqual(diff["diffs"], 1)

        switch = db.apply_reextraction_switchover()
        self.assertEqual(switch["updated"], 1)

        with db.conn() as c:
            row = c.execute("SELECT * FROM predictions WHERE id = ?", (orig_id,)).fetchone()
        self.assertEqual(row["category"], "finance")
        self.assertEqual(row["predicted_probability"], 0.72)

        self.assertEqual(db.reextraction_diff_summary()["total"], 0)


if __name__ == "__main__":
    unittest.main()

"""
Unit tests for the two-pass classifier — no network required.

Covers the pure helpers (triage parser, entity sanitiser, cost estimation,
ceiling check) and the orchestration logic when no API key is present.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Point at a throwaway DB so we don't touch annoyance.db on disk.
_TMP = tempfile.TemporaryDirectory()
import config as _config  # noqa: E402
_config.DB_PATH = Path(_TMP.name) / "unit.db"

import db  # noqa: E402
db._local.__dict__.clear()
db.init_db()

import classifier  # noqa: E402


class TestTriageParser(unittest.TestCase):
    """_parse_triage_response accepts clean output, rejects ambiguity."""

    def test_clean_keep_skip(self):
        out = classifier._parse_triage_response("keep\nskip\nkeep", expected=3)
        self.assertEqual(out, ["keep", "skip", "keep"])

    def test_numbered_lines_are_stripped(self):
        out = classifier._parse_triage_response("1. keep\n2. skip\n3. keep", expected=3)
        self.assertEqual(out, ["keep", "skip", "keep"])

    def test_case_insensitive_prefix(self):
        out = classifier._parse_triage_response("KEEP\nSkip\n   keep  ", expected=3)
        self.assertEqual(out, ["keep", "skip", "keep"])

    def test_wrong_length_returns_none(self):
        out = classifier._parse_triage_response("keep\nskip", expected=3)
        self.assertIsNone(out)

    def test_unknown_token_returns_none(self):
        out = classifier._parse_triage_response("keep\nmaybe\nskip", expected=3)
        self.assertIsNone(out)

    def test_empty_input_returns_none(self):
        self.assertIsNone(classifier._parse_triage_response("", expected=1))


class TestEntitySanitizer(unittest.TestCase):
    def test_drops_hallucinated_entities(self):
        # "Tesla" not in content
        entities = [
            {"name": "United Airlines", "type": "company", "salience": 0.9, "sentiment": "angry"},
            {"name": "Tesla", "type": "company", "salience": 0.9, "sentiment": "neutral"},
        ]
        content = "United Airlines lost my bag"
        out = classifier._sanitize_entities(entities, content)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "United Airlines")

    def test_clamps_invalid_type_to_other(self):
        entities = [{"name": "X", "type": "spaceship", "salience": 0.5}]
        out = classifier._sanitize_entities(entities, "X is annoying")
        self.assertEqual(out[0]["type"], "other")

    def test_clamps_salience_to_unit_range(self):
        entities = [
            {"name": "A", "type": "company", "salience": 5.0},
            {"name": "B", "type": "company", "salience": -1.0},
            {"name": "C", "type": "company", "salience": "not a number"},
        ]
        out = classifier._sanitize_entities(entities, "A B C are all bad")
        saliences = [e["salience"] for e in out]
        self.assertTrue(all(0.0 <= s <= 1.0 for s in saliences))

    def test_drops_empty_names(self):
        entities = [{"name": "", "type": "company"}, {"name": "   ", "type": "company"}]
        out = classifier._sanitize_entities(entities, "stuff")
        self.assertEqual(out, [])


class TestCostEstimation(unittest.TestCase):
    def test_haiku_cheaper_than_sonnet(self):
        h = classifier._estimated_cost_cents("claude-haiku-4-5+triagev1", 1000, 200)
        s = classifier._estimated_cost_cents("claude-sonnet-4-5+classifyv1", 1000, 200)
        self.assertLess(h, s)

    def test_zero_tokens_zero_cost(self):
        self.assertEqual(classifier._estimated_cost_cents("haiku", 0, 0), 0.0)

    def test_cost_scales_linearly(self):
        small = classifier._estimated_cost_cents("haiku", 100, 100)
        big = classifier._estimated_cost_cents("haiku", 10000, 10000)
        # 100× the tokens → 100× the cost, within float noise
        self.assertAlmostEqual(big / small, 100.0, delta=0.01)


class TestDailyCostCeiling(unittest.TestCase):
    def test_fresh_db_under_ceiling(self):
        with db.cursor() as cur:
            cur.execute("DELETE FROM claude_usage")
        self.assertFalse(classifier._daily_cost_exceeded())

    def test_logs_trip_ceiling(self):
        with db.cursor() as cur:
            cur.execute("DELETE FROM claude_usage")
        # Log one huge usage event, over the ceiling
        db.log_claude_usage(
            operation="test",
            model="haiku",
            input_tokens=10_000_000,
            output_tokens=10_000_000,
            estimated_cost_cents=_config.DAILY_COST_CEILING_CENTS + 100,
            post_count=1,
            batch_id="test",
        )
        self.assertTrue(classifier._daily_cost_exceeded())

        # Cleanup so other tests aren't poisoned
        with db.cursor() as cur:
            cur.execute("DELETE FROM claude_usage")


class TestClassifyPendingPostsNoApi(unittest.IsolatedAsyncioTestCase):
    """With no API key, orchestrator returns cleanly without raising."""

    async def test_no_api_key_short_circuits(self):
        with patch.object(classifier.config, "ANTHROPIC_API_KEY", ""):
            summary = await classifier.classify_pending_posts(limit=10)
            self.assertIsInstance(summary, dict)
            # Either nothing to do, or triage forwards all to Sonnet which also no-ops
            self.assertIn("triaged", summary)

    async def test_cost_ceiling_halts_batch(self):
        # Seed one unclassified post, then trip the ceiling
        post_id = "unit:ceiling:01"
        db.insert_post(
            id=post_id, source="unit", content="Apple sucks today",
            posted_at="2026-04-20T00:00:00+00:00",
        )
        with db.cursor() as cur:
            cur.execute("DELETE FROM claude_usage")
        db.log_claude_usage(
            operation="test", model="haiku",
            input_tokens=0, output_tokens=0,
            estimated_cost_cents=_config.DAILY_COST_CEILING_CENTS + 1,
            post_count=0, batch_id="t",
        )
        summary = await classifier.classify_pending_posts(limit=10)
        self.assertEqual(summary.get("error"), "cost_ceiling")

        # Cleanup
        with db.cursor() as cur:
            cur.execute("DELETE FROM claude_usage")
            cur.execute("DELETE FROM posts WHERE id = ?", (post_id,))


class TestChunked(unittest.TestCase):
    def test_chunks_of_three(self):
        out = list(classifier.chunked([1, 2, 3, 4, 5, 6, 7], 3))
        self.assertEqual(out, [[1, 2, 3], [4, 5, 6], [7]])

    def test_empty_yields_nothing(self):
        self.assertEqual(list(classifier.chunked([], 5)), [])


class TestClassifyResponseParser(unittest.TestCase):
    def test_plain_array(self):
        out = classifier._parse_classify_response(
            '[{"id": "a", "annoyance": 80}]'
        )
        self.assertEqual(out[0]["id"], "a")

    def test_fenced_array(self):
        out = classifier._parse_classify_response(
            '```json\n[{"id": "a", "annoyance": 80}]\n```'
        )
        self.assertEqual(out[0]["id"], "a")

    def test_items_wrapper(self):
        out = classifier._parse_classify_response(
            '{"items": [{"id": "a", "annoyance": 80}]}'
        )
        self.assertEqual(out[0]["id"], "a")

    def test_garbage_returns_none(self):
        self.assertIsNone(classifier._parse_classify_response("not json"))
        self.assertIsNone(classifier._parse_classify_response(""))


if __name__ == "__main__":
    unittest.main()

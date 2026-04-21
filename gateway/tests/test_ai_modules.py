"""Tests for gateway/ai/ — extractor, categoriser, summariser, environmental.

Every module exposes a tiny ``_call_claude`` that takes the user text
and returns (raw_text, response_object). We monkey-patch that so no
network traffic happens and the DB writes (cache + usage log) stay
verifiable.

All four modules hand ai.client.log_response a SimpleNamespace response
with a .usage attribute; our fakes do the same. This mirrors the real
Anthropic SDK's shape closely enough that log_response's cost_for math
runs for real.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _fake_response(text: str, *, in_toks: int = 40, out_toks: int = 20):
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        usage=SimpleNamespace(input_tokens=in_toks, output_tokens=out_toks),
    )


# ── Fixture: file-backed DB so the modules' sqlite3 connections see it ────


class _AiFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = Path(tempfile.mktemp(suffix=".db", prefix="narve-ai-test-"))
        os.environ["GATEWAY_DB_PATH"] = str(cls.db_path)

        import db, migrations
        # Point db.conn at the temp file for migrations only, then close.
        conn = sqlite3.connect(cls.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        orig = db.conn

        @contextlib.contextmanager
        def fake():
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        db.conn = fake
        try:
            db.init_db()
            migrations.upgrade_to_head()
        finally:
            db.conn = orig
        conn.close()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.db_path.unlink(missing_ok=True)
        except Exception:
            pass

    def setUp(self):
        # Reset ai_cache + claude_usage_log between tests so counts stay clean.
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM ai_cache")
        conn.execute("DELETE FROM claude_usage_log")
        conn.commit()
        conn.close()


# ── Extractor ───────────────────────────────────────────────────────────────


class TestExtractor(_AiFixture):
    def test_valid_prediction_extracted(self):
        from ai import extractor
        payload = [{
            "claim": "BTC hits 100k by 2026",
            "direction": "yes",
            "explicit_probability": 0.65,
            "implicit_confidence": "medium",
            "time_frame": "by 2026",
            "category": "crypto",
            "contains_sarcasm": False,
            "is_conditional": False,
        }]

        async def fake(text, handle=None):
            return json.dumps(payload), _fake_response(json.dumps(payload))

        # _call_claude only receives one positional arg in this module.
        extractor._call_claude = lambda text: fake(text)

        preds = _run(extractor.extract_predictions_from_post("BTC to 100k soon"))
        self.assertEqual(len(preds), 1)
        self.assertEqual(preds[0]["category"], "crypto")
        self.assertAlmostEqual(preds[0]["explicit_probability"], 0.65)

    def test_cache_prevents_second_call(self):
        from ai import extractor
        calls = {"n": 0}

        async def fake(text):
            calls["n"] += 1
            return "[]", _fake_response("[]")

        extractor._call_claude = fake
        for _ in range(3):
            _run(extractor.extract_predictions_from_post("identical text"))
        self.assertEqual(calls["n"], 1)

    def test_invalid_json_returns_empty_list(self):
        from ai import extractor

        async def fake(text):
            return "not-json {{{", _fake_response("not-json")

        extractor._call_claude = fake
        preds = _run(extractor.extract_predictions_from_post("garbage"))
        self.assertEqual(preds, [])

    def test_empty_post_short_circuits(self):
        from ai import extractor
        called = {"n": 0}

        async def fake(text):
            called["n"] += 1
            return "[]", _fake_response("[]")

        extractor._call_claude = fake
        self.assertEqual(_run(extractor.extract_predictions_from_post("")), [])
        self.assertEqual(_run(extractor.extract_predictions_from_post("   ")), [])
        self.assertEqual(called["n"], 0)


# ── Categoriser ─────────────────────────────────────────────────────────────


class TestCategoriser(_AiFixture):
    def test_parses_strict_json(self):
        from ai import categoriser

        async def fake(q, d=""):
            text = json.dumps({
                "primary_category": "finance",
                "sub_category": "fed_rates",
                "tags": ["fed", "rates"],
                "political_leaning": "n_a",
                "sensitivity": "normal",
                "relevance_signals": {
                    "insider_trading_relevant": False,
                    "environmental_impact_relevant": False,
                    "requires_expert_knowledge": True,
                },
            })
            return text, _fake_response(text)

        categoriser._call_claude = lambda q, d="": fake(q, d)
        result = _run(categoriser.categorise_market(
            "poly:fed-cut", "Will the Fed cut rates?", ""))
        self.assertEqual(result["primary_category"], "finance")
        self.assertTrue(result["requires_expert_knowledge"])

    def test_unknown_category_falls_back(self):
        from ai import categoriser

        async def fake(q, d=""):
            text = json.dumps({"primary_category": "aliens"})
            return text, _fake_response(text)

        categoriser._call_claude = lambda q, d="": fake(q, d)
        result = _run(categoriser.categorise_market(
            "poly:alien", "Will aliens land?", ""))
        self.assertEqual(result["primary_category"], "other")

    def test_claude_unavailable_uses_keyword_fallback(self):
        from ai import categoriser

        async def fake(q, d=""):
            return None, None

        categoriser._call_claude = lambda q, d="": fake(q, d)
        result = _run(categoriser.categorise_market(
            "poly:btc", "Will Bitcoin hit 100k?", ""))
        self.assertEqual(result["primary_category"], "crypto")


# ── Source summariser ──────────────────────────────────────────────────────


class TestSourceSummariser(_AiFixture):
    def test_unknown_source_returns_graceful_fallback(self):
        from ai import source_summariser

        async def fake(msg):
            raise AssertionError("Should not be called for unknown source")

        source_summariser._call_claude = fake
        result = _run(source_summariser.generate_source_summary("ghosthandle"))
        self.assertIn("not yet made enough", result["summary"])
        self.assertEqual(result["model"], "fallback_no_data")

    def test_low_data_source_skips_claude(self):
        from ai import source_summariser
        # Seed a source with < MIN_PREDICTIONS history.
        conn = sqlite3.connect(self.db_path)
        import time as _t
        now = int(_t.time())
        conn.execute(
            "INSERT INTO source_credibility "
            "(source_handle, global_credibility, accuracy_unlocked, "
            "total_predictions, correct_predictions, categories_active, "
            "last_computed_at) VALUES (?,?,?,?,?,?,?)",
            ("tinysource", 0.6, 1, 3, 2, 1, now),
        )
        conn.commit()
        conn.close()

        called = {"n": 0}

        async def fake(msg):
            called["n"] += 1
            return "never", _fake_response("never")

        source_summariser._call_claude = fake
        result = _run(source_summariser.generate_source_summary("tinysource"))
        self.assertEqual(called["n"], 0)
        self.assertEqual(result["model"], "fallback_low_data")

    def test_full_source_generates_and_caches(self):
        from ai import source_summariser
        conn = sqlite3.connect(self.db_path)
        import time as _t
        now = int(_t.time())
        conn.execute(
            "INSERT OR REPLACE INTO source_credibility "
            "(source_handle, global_credibility, accuracy_unlocked, "
            "total_predictions, correct_predictions, categories_active, "
            "last_computed_at) VALUES (?,?,?,?,?,?,?)",
            ("fedwatcher", 0.81, 1, 47, 33, 3, now),
        )
        for i in range(47):
            conn.execute(
                "INSERT INTO predictions "
                "(source_handle, market_id, category, direction, content, "
                "extracted_at, resolved, resolved_correct) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("fedwatcher", None, "finance", "YES",
                 f"Fed prediction {i}", now - i * 3600,
                 1 if i < 33 else 0, 1 if i < 33 else 0),
            )
        conn.commit()
        conn.close()

        async def fake(msg):
            text = "@fedwatcher is a credibility-0.81 source focused on US monetary policy."
            return text, _fake_response(text, in_toks=400, out_toks=120)

        source_summariser._call_claude = fake
        result = _run(source_summariser.generate_source_summary("fedwatcher"))
        self.assertIn("@fedwatcher", result["summary"])

        # Second call hits cache — monkey-patch with an assertion.
        async def should_not_call(msg):
            raise AssertionError("cache miss: should not hit Claude")

        source_summariser._call_claude = should_not_call
        result2 = _run(source_summariser.generate_source_summary("fedwatcher"))
        self.assertEqual(result2["summary"], result["summary"])


# ── Environmental ──────────────────────────────────────────────────────────


class TestEnvironmental(_AiFixture):
    def test_parses_strict_response(self):
        from ai import environmental

        async def fake(q, c=""):
            text = json.dumps({
                "is_relevant": True,
                "irrelevance_reason": None,
                "yes_outcome_label": "YES",
                "no_outcome_label": "NO",
                "yes_co2_impact_mt": -2.1,
                "no_co2_impact_mt": 0.8,
                "yes_impact_description": "Rejoin reduces emissions.",
                "no_impact_description": "Status quo.",
                "yes_impact_timeframe": "over 10 years",
                "no_impact_timeframe": "per year",
                "confidence": "medium",
                "confidence_reason": "Policy estimates vary.",
                "data_sources": ["IPCC AR6"],
            })
            return text, _fake_response(text, in_toks=500, out_toks=200)

        environmental._call_claude = lambda q, c="": fake(q, c)
        result = _run(environmental.generate_environmental_impact(
            "poly:paris", "Will the US rejoin the Paris Agreement?",
            category="politics", yes_price=0.5,
        ))
        self.assertTrue(result["is_relevant"])
        self.assertEqual(result["yes_co2_impact_mt"], -2.1)

    def test_price_drift_triggers_regeneration(self):
        from ai import environmental
        calls = {"n": 0}

        async def fake(q, c=""):
            calls["n"] += 1
            text = json.dumps({
                "is_relevant": True, "irrelevance_reason": None,
                "yes_outcome_label": "YES", "no_outcome_label": "NO",
                "yes_co2_impact_mt": 1.0, "no_co2_impact_mt": 1.0,
                "yes_impact_description": "x", "no_impact_description": "y",
                "yes_impact_timeframe": "per year", "no_impact_timeframe": "per year",
                "confidence": "low", "confidence_reason": "r",
                "data_sources": [],
            })
            return text, _fake_response(text)

        environmental._call_claude = lambda q, c="": fake(q, c)
        _run(environmental.generate_environmental_impact("poly:drift", "Q?", yes_price=0.4))
        # Same market, >10% drift — must regenerate.
        _run(environmental.generate_environmental_impact("poly:drift", "Q?", yes_price=0.6))
        self.assertEqual(calls["n"], 2)

    def test_claude_failure_returns_stub(self):
        from ai import environmental

        async def fake(q, c=""):
            return None, None

        environmental._call_claude = lambda q, c="": fake(q, c)
        result = _run(environmental.generate_environmental_impact(
            "poly:oops", "Q?", yes_price=0.5))
        self.assertFalse(result["is_relevant"])


if __name__ == "__main__":
    unittest.main()

"""Tests for the Environmental Impact feature (migration 008).

Covers:
- The migration creates the table and user columns
- DB helpers (get/upsert/list_top/get_user_env_preferences/set_user_env_preferences)
- intelligence/environmental.py: conversions, stub generation, JSON parsing,
  cache hits, force-refresh, price-drift regeneration
- intelligence/context.py: env detection in Intelligence assistant context
- API routes: pro gating, force-refresh rate limit, preference PATCH validation,
  the env merge into GET /api/markets/unified/{id}

The Anthropic SDK is never called — `intelligence.environmental._call_claude`
is monkey-patched in every test so the suite is fast and offline.
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
from intelligence import environmental as env
from intelligence import context as ctx_mod


def _run(coro):
    """Run an async coroutine in a fresh event loop. unittest tests are sync,
    and the analyser is async, so we wrap each call individually rather than
    fighting with the get_event_loop deprecation across Python versions."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_market(market_id="poly:test", title="Will US rejoin Paris Agreement?",
                 category="politics", yes_price=0.67):
    """Build a duck-typed UnifiedMarket-like object for the analyser."""
    return SimpleNamespace(id=market_id, title=title, category=category,
                           yes_price=yes_price)


# ── Test fixture: in-memory sqlite + migrations applied ─────────────────────

class _DbFixture(unittest.TestCase):
    """Base class that gives every test a fresh in-memory DB with migration
    008 applied. Each test method gets its own connection so cache state from
    one test cannot leak into another."""

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
        # Apply every migration including 008.
        migrations.upgrade_to_head()

        cls.user_id = db.create_user("env@test.com", "TestPass123!", username="envtest")

    @classmethod
    def tearDownClass(cls):
        db.conn = cls._orig_conn
        cls._test_conn.close()

    def setUp(self):
        # Wipe env cache between tests so cache hits don't bleed.
        with db.conn() as c:
            c.execute("DELETE FROM environmental_impacts")


# ── Migration ────────────────────────────────────────────────────────────────

class TestMigration008(_DbFixture):
    def test_environmental_impacts_table_exists(self):
        with db.conn() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(environmental_impacts)")}
        for required in (
            "market_id", "market_question", "generated_at", "generated_by",
            "cache_valid_until", "is_relevant", "yes_co2_impact_mt",
            "no_co2_impact_mt", "yes_impact_description", "no_impact_description",
            "yes_impact_timeframe", "no_impact_timeframe", "confidence",
            "confidence_reason", "data_sources", "category",
            "yes_market_price_at_gen",
        ):
            self.assertIn(required, cols)

    def test_users_env_columns_exist(self):
        with db.conn() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        self.assertIn("env_show", cols)
        self.assertIn("env_unit", cols)

    def test_schema_version_records_008(self):
        with db.conn() as c:
            revs = {r["revision"] for r in c.execute("SELECT revision FROM schema_version")}
        self.assertIn("008", revs)


# ── DB helpers ───────────────────────────────────────────────────────────────

class TestDbHelpers(_DbFixture):
    def test_user_pref_defaults(self):
        # Use a fresh user so we don't depend on test ordering — other tests
        # in this class mutate self.user_id's prefs.
        fresh_id = db.create_user(
            "envdefaults@test.com", "TestPass123!", username="envdefaults",
        )
        prefs = db.get_user_env_preferences(fresh_id)
        self.assertEqual(prefs, {"show": True, "unit": "co2_mt"})

    def test_set_user_pref_persists(self):
        ok = db.set_user_env_preferences(self.user_id, show=False, unit="trees")
        self.assertTrue(ok)
        self.assertEqual(
            db.get_user_env_preferences(self.user_id),
            {"show": False, "unit": "trees"},
        )

    def test_set_user_pref_rejects_invalid_unit(self):
        with self.assertRaises(ValueError):
            db.set_user_env_preferences(self.user_id, show=True, unit="bogus_unit")

    def test_upsert_round_trip(self):
        payload = {
            "market_question": "Will Tesla sell 3M EVs in 2026?",
            "market_category": "science",
            "generated_at": int(time.time()),
            "generated_by": "test",
            "cache_valid_until": int(time.time()) + 3600,
            "is_relevant": True,
            "yes_co2_impact_mt": -12.0,
            "no_co2_impact_mt": 0.0,
            "yes_impact_description": "EV displacement",
            "no_impact_description": "Status quo",
            "yes_impact_timeframe": "per year",
            "no_impact_timeframe": "per year",
            "confidence": "medium",
            "confidence_reason": "EV impact estimates vary",
            "data_sources": ["EPA"],
            "category": "emissions",
            "yes_market_price_at_gen": 0.40,
        }
        rid = db.upsert_environmental_impact("poly:tesla-2026", payload)
        self.assertGreater(rid, 0)
        row = db.get_environmental_impact("poly:tesla-2026")
        self.assertIsNotNone(row)
        self.assertEqual(row["market_question"], "Will Tesla sell 3M EVs in 2026?")
        self.assertEqual(row["yes_co2_impact_mt"], -12.0)
        self.assertEqual(json.loads(row["data_sources"]), ["EPA"])

    def test_get_returns_none_when_expired(self):
        payload = {
            "market_question": "Q?",
            "generated_at": int(time.time()) - 100,
            "generated_by": "test",
            "cache_valid_until": int(time.time()) - 10,  # already expired
            "is_relevant": True,
            "yes_co2_impact_mt": 1.0,
            "no_co2_impact_mt": 1.0,
        }
        db.upsert_environmental_impact("poly:expired", payload)
        self.assertIsNone(db.get_environmental_impact("poly:expired"))
        # but get_environmental_impact_any_age still finds it
        row = db.get_environmental_impact_any_age("poly:expired")
        self.assertIsNotNone(row)

    def test_top_impacts_orders_by_absolute_total(self):
        for i, (mid, yes, no) in enumerate([
            ("poly:small", 0.1, 0.1),     # total |0.2|
            ("poly:huge", -50.0, 25.0),   # total |75.0|
            ("poly:medium", -5.0, 3.0),   # total |8.0|
        ]):
            db.upsert_environmental_impact(mid, {
                "market_question": f"Q{i}",
                "generated_at": int(time.time()),
                "generated_by": "test",
                "cache_valid_until": int(time.time()) + 3600,
                "is_relevant": True,
                "yes_co2_impact_mt": yes,
                "no_co2_impact_mt": no,
            })
        top = db.list_top_environmental_impacts(limit=10)
        self.assertEqual(len(top), 3)
        self.assertEqual(top[0]["market_id"], "poly:huge")
        self.assertEqual(top[1]["market_id"], "poly:medium")
        self.assertEqual(top[2]["market_id"], "poly:small")

    def test_top_impacts_excludes_irrelevant(self):
        db.upsert_environmental_impact("poly:rel", {
            "market_question": "Q",
            "generated_at": int(time.time()),
            "generated_by": "test",
            "cache_valid_until": int(time.time()) + 3600,
            "is_relevant": True,
            "yes_co2_impact_mt": 1.0,
            "no_co2_impact_mt": 1.0,
        })
        db.upsert_environmental_impact("poly:irrel", {
            "market_question": "Q",
            "generated_at": int(time.time()),
            "generated_by": "test",
            "cache_valid_until": int(time.time()) + 3600,
            "is_relevant": False,
            "irrelevance_reason": "not climate",
        })
        top = db.list_top_environmental_impacts()
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["market_id"], "poly:rel")


# ── Unit conversions ─────────────────────────────────────────────────────────

class TestConversions(unittest.TestCase):
    def test_co2_mt_passthrough(self):
        r = env.convert_co2(2.1, "co2_mt")
        self.assertEqual(r["value"], 2.1)
        self.assertEqual(r["unit_label"], "MT CO2e")

    def test_trees(self):
        r = env.convert_co2(2.1, "trees")
        self.assertEqual(r["value"], round(2.1 * 45_871, 4))

    def test_cars(self):
        r = env.convert_co2(2.1, "cars")
        self.assertEqual(r["value"], round(2.1 * 217_391, 4))

    def test_homes(self):
        r = env.convert_co2(2.1, "homes")
        self.assertEqual(r["value"], round(2.1 * 86_957, 4))

    def test_flights(self):
        r = env.convert_co2(2.1, "flights")
        self.assertEqual(r["value"], round(2.1 * 500_000, 4))

    def test_none_passthrough(self):
        r = env.convert_co2(None, "trees")
        self.assertIsNone(r["value"])
        self.assertEqual(r["unit_label"], "trees planted")

    def test_unknown_unit_falls_back_to_co2_mt(self):
        r = env.convert_co2(1.0, "made_up_unit")
        self.assertEqual(r["unit_key"], "co2_mt")

    def test_negative_values_preserved(self):
        # Reductions must keep their sign — the UI uses sign for colour coding.
        r = env.convert_co2(-2.1, "trees")
        self.assertLess(r["value"], 0)


# ── Analyser orchestration (Claude mocked) ───────────────────────────────────

class TestAnalyser(_DbFixture):
    def _patch_claude(self, response_text):
        """Replace _call_claude with an async stub returning *response_text*."""
        async def _stub(*a, **kw):
            return response_text
        env._call_claude = _stub

    def setUp(self):
        super().setUp()
        # Reset to a no-op stub before each test so leftover patches don't
        # cross-contaminate. Tests that need real returns re-patch.
        async def _none(*a, **kw):
            return None
        env._call_claude = _none

    def test_irrelevant_category_short_circuits(self):
        # Sports markets never call Claude — get a stub immediately.
        called = [0]
        async def _stub(*a, **kw):
            called[0] += 1
            return None
        env._call_claude = _stub

        m = _make_market(market_id="poly:nba", title="Lakers win?", category="sports")
        result = _run(env.generate_environmental_impact(m))
        self.assertFalse(result["is_relevant"])
        self.assertEqual(called[0], 0)
        # And it gets cached so the next call also doesn't ring Claude.
        result2 = _run(env.generate_environmental_impact(m))
        self.assertFalse(result2["is_relevant"])
        self.assertEqual(called[0], 0)

    def test_relevant_market_with_valid_claude_response(self):
        valid_json = json.dumps({
            "is_relevant": True,
            "irrelevance_reason": None,
            "yes_co2_impact_mt": -2.1,
            "no_co2_impact_mt": 0.8,
            "yes_impact_description": "Rejoin reduces emissions.",
            "no_impact_description": "Status quo continues.",
            "yes_impact_timeframe": "over 10 years",
            "no_impact_timeframe": "per year",
            "confidence": "medium",
            "confidence_reason": "Policy estimates vary.",
            "data_sources": ["IPCC AR6", "EPA"],
            "category": "emissions",
        })
        self._patch_claude(valid_json)

        m = _make_market(market_id="poly:paris", title="Paris rejoin?", category="politics")
        result = _run(env.generate_environmental_impact(m))
        self.assertTrue(result["is_relevant"])
        self.assertEqual(result["yes_co2_impact_mt"], -2.1)
        self.assertEqual(result["no_co2_impact_mt"], 0.8)
        self.assertEqual(result["confidence"], "medium")
        self.assertEqual(result["category"], "emissions")
        self.assertIn("IPCC AR6", result["data_sources"])

    def test_invalid_json_falls_back_to_stub(self):
        self._patch_claude("not even close to json {{{")
        m = _make_market(market_id="poly:bad", title="Q?", category="politics")
        result = _run(env.generate_environmental_impact(m))
        self.assertFalse(result["is_relevant"])
        self.assertIn("invalid", (result["irrelevance_reason"] or "").lower())

    def test_code_fenced_json_stripped(self):
        valid = json.dumps({
            "is_relevant": True, "irrelevance_reason": None,
            "yes_co2_impact_mt": 1.0, "no_co2_impact_mt": -1.0,
            "yes_impact_description": "x", "no_impact_description": "y",
            "yes_impact_timeframe": "per year", "no_impact_timeframe": "per year",
            "confidence": "high", "confidence_reason": "r",
            "data_sources": [], "category": "energy",
        })
        self._patch_claude("```json\n" + valid + "\n```")
        m = _make_market(market_id="poly:fenced", title="Q?", category="politics")
        result = _run(env.generate_environmental_impact(m))
        self.assertTrue(result["is_relevant"])
        self.assertEqual(result["yes_co2_impact_mt"], 1.0)

    def test_cache_hit_skips_claude_on_second_call(self):
        call_count = [0]
        valid = json.dumps({
            "is_relevant": True, "irrelevance_reason": None,
            "yes_co2_impact_mt": 5.0, "no_co2_impact_mt": -5.0,
            "yes_impact_description": "x", "no_impact_description": "y",
            "yes_impact_timeframe": "per year", "no_impact_timeframe": "per year",
            "confidence": "low", "confidence_reason": "r",
            "data_sources": [], "category": "energy",
        })
        async def _counting_stub(*a, **kw):
            call_count[0] += 1
            return valid
        env._call_claude = _counting_stub

        m = _make_market(market_id="poly:cached", title="Q?", category="politics")
        _run(env.generate_environmental_impact(m))
        _run(env.generate_environmental_impact(m))
        _run(env.generate_environmental_impact(m))
        self.assertEqual(call_count[0], 1)

    def test_force_regenerate_bypasses_cache(self):
        call_count = [0]
        valid = json.dumps({
            "is_relevant": True, "irrelevance_reason": None,
            "yes_co2_impact_mt": 1.0, "no_co2_impact_mt": 1.0,
            "yes_impact_description": "x", "no_impact_description": "y",
            "yes_impact_timeframe": "per year", "no_impact_timeframe": "per year",
            "confidence": "low", "confidence_reason": "r",
            "data_sources": [], "category": "energy",
        })
        async def _counting_stub(*a, **kw):
            call_count[0] += 1
            return valid
        env._call_claude = _counting_stub

        m = _make_market(market_id="poly:force", title="Q?", category="politics")
        _run(env.generate_environmental_impact(m))
        _run(env.generate_environmental_impact(m, force=True))
        self.assertEqual(call_count[0], 2)

    def test_price_drift_triggers_regeneration(self):
        call_count = [0]
        valid = json.dumps({
            "is_relevant": True, "irrelevance_reason": None,
            "yes_co2_impact_mt": 1.0, "no_co2_impact_mt": 1.0,
            "yes_impact_description": "x", "no_impact_description": "y",
            "yes_impact_timeframe": "per year", "no_impact_timeframe": "per year",
            "confidence": "low", "confidence_reason": "r",
            "data_sources": [], "category": "energy",
        })
        async def _counting_stub(*a, **kw):
            call_count[0] += 1
            return valid
        env._call_claude = _counting_stub

        m = _make_market(market_id="poly:drift", title="Q?", category="politics", yes_price=0.40)
        _run(env.generate_environmental_impact(m))
        # Same market, but price has moved 25% — should regenerate without
        # the caller passing force=True.
        m2 = _make_market(market_id="poly:drift", title="Q?", category="politics", yes_price=0.65)
        _run(env.generate_environmental_impact(m2))
        self.assertEqual(call_count[0], 2)

    def test_no_drift_no_regeneration(self):
        call_count = [0]
        valid = json.dumps({
            "is_relevant": True, "irrelevance_reason": None,
            "yes_co2_impact_mt": 1.0, "no_co2_impact_mt": 1.0,
            "yes_impact_description": "x", "no_impact_description": "y",
            "yes_impact_timeframe": "per year", "no_impact_timeframe": "per year",
            "confidence": "low", "confidence_reason": "r",
            "data_sources": [], "category": "energy",
        })
        async def _counting_stub(*a, **kw):
            call_count[0] += 1
            return valid
        env._call_claude = _counting_stub

        m = _make_market(market_id="poly:nodrift", title="Q?", category="politics", yes_price=0.40)
        _run(env.generate_environmental_impact(m))
        # 5% movement is under the 10% threshold — should still hit cache.
        m2 = _make_market(market_id="poly:nodrift", title="Q?", category="politics", yes_price=0.45)
        _run(env.generate_environmental_impact(m2))
        self.assertEqual(call_count[0], 1)

    def test_claude_returns_none_falls_back_to_stub(self):
        # _call_claude returns None when API key missing or SDK error.
        async def _none(*a, **kw):
            return None
        env._call_claude = _none
        m = _make_market(market_id="poly:nokey", title="Q?", category="politics")
        result = _run(env.generate_environmental_impact(m))
        self.assertFalse(result["is_relevant"])

    def test_invalid_confidence_coerced_to_speculative(self):
        bad_conf = json.dumps({
            "is_relevant": True, "irrelevance_reason": None,
            "yes_co2_impact_mt": 1.0, "no_co2_impact_mt": 1.0,
            "yes_impact_description": "x", "no_impact_description": "y",
            "yes_impact_timeframe": "per year", "no_impact_timeframe": "per year",
            "confidence": "very_super_high",  # not in VALID_CONFIDENCE
            "confidence_reason": "r",
            "data_sources": [], "category": "energy",
        })
        self._patch_claude(bad_conf)
        m = _make_market(market_id="poly:badconf", title="Q?", category="politics")
        result = _run(env.generate_environmental_impact(m))
        self.assertEqual(result["confidence"], "speculative")


# ── apply_user_unit_preference ───────────────────────────────────────────────

class TestUnitPreferenceApplication(unittest.TestCase):
    def test_augments_payload_with_converted_fields(self):
        payload = {
            "yes_co2_impact_mt": -2.1,
            "no_co2_impact_mt": 0.8,
            "is_relevant": True,
        }
        out = env.apply_user_unit_preference(payload, "trees")
        self.assertIn("yes_co2_impact_converted", out)
        self.assertIn("no_co2_impact_converted", out)
        self.assertEqual(out["preferred_unit"], "trees")
        # Original MT values stay in place so clients can re-render client-side.
        self.assertEqual(out["yes_co2_impact_mt"], -2.1)
        self.assertEqual(out["no_co2_impact_mt"], 0.8)


# ── Intelligence context integration ────────────────────────────────────────

class TestApiResponseShaping(_DbFixture):
    """Tests for the integration logic that the new server.py routes call:
    _row_to_payload + apply_user_unit_preference. The HTTP routing layer is
    a thin wrapper around these so verifying the payload shape here gives
    confidence in the response without spinning up the FastAPI app."""

    def test_row_to_payload_round_trips(self):
        """upsert → row → _row_to_payload should preserve all fields, with
        data_sources surviving the JSON round trip."""
        now = int(time.time())
        db.upsert_environmental_impact("poly:roundtrip", {
            "market_question": "Q?",
            "market_category": "politics",
            "generated_at": now,
            "generated_by": "claude-sonnet-4-5-20250929",
            "cache_valid_until": now + 86400,
            "is_relevant": True,
            "yes_co2_impact_mt": -2.1,
            "no_co2_impact_mt": 0.8,
            "yes_impact_description": "good",
            "no_impact_description": "bad",
            "yes_impact_timeframe": "over 10 years",
            "no_impact_timeframe": "per year",
            "confidence": "medium",
            "confidence_reason": "policy uncertainty",
            "data_sources": ["IPCC AR6", "EPA"],
            "category": "emissions",
            "yes_market_price_at_gen": 0.67,
        })
        row = db.get_environmental_impact("poly:roundtrip")
        self.assertIsNotNone(row, "upsert+get round-trip should not return None")
        payload = env._row_to_payload(row)
        self.assertEqual(payload["market_id"], "poly:roundtrip")
        self.assertEqual(payload["yes_co2_impact_mt"], -2.1)
        self.assertEqual(payload["no_co2_impact_mt"], 0.8)
        self.assertTrue(payload["is_relevant"])
        self.assertEqual(payload["data_sources"], ["IPCC AR6", "EPA"])
        self.assertEqual(payload["confidence"], "medium")

    def test_row_to_payload_handles_invalid_sources_json(self):
        """If data_sources somehow contains invalid JSON in the DB (legacy
        rows, manual edits), _row_to_payload should not crash — it falls
        back to an empty list."""
        # Direct INSERT bypassing the upsert helper to plant bad data.
        now = int(time.time())
        with db.conn() as c:
            c.execute(
                "INSERT INTO environmental_impacts "
                "(market_id, market_question, generated_at, generated_by, "
                "cache_valid_until, is_relevant, data_sources) "
                "VALUES (?,?,?,?,?,?,?)",
                ("poly:badjson", "Q?", now, "test", now + 86400, 1, "{not valid json"),
            )
        row = db.get_environmental_impact("poly:badjson")
        self.assertIsNotNone(row, "direct INSERT then get should not return None")
        payload = env._row_to_payload(row)
        self.assertEqual(payload["data_sources"], [])

    def test_serialised_payload_keeps_original_mt_alongside_converted(self):
        """The detail-panel UI lets users switch units client-side, so the
        merged response must keep both raw MT values AND the converted
        values for the user's preferred unit."""
        payload = {
            "market_id": "poly:x",
            "is_relevant": True,
            "yes_co2_impact_mt": -2.1,
            "no_co2_impact_mt": 0.8,
        }
        out = env.apply_user_unit_preference(payload, "trees")
        # Originals preserved
        self.assertEqual(out["yes_co2_impact_mt"], -2.1)
        self.assertEqual(out["no_co2_impact_mt"], 0.8)
        # Converted fields added
        self.assertEqual(out["preferred_unit"], "trees")
        self.assertEqual(out["yes_co2_impact_converted"]["unit_key"], "trees")
        self.assertEqual(out["no_co2_impact_converted"]["unit_key"], "trees")
        # Sign preserved through conversion
        self.assertLess(out["yes_co2_impact_converted"]["value"], 0)
        self.assertGreater(out["no_co2_impact_converted"]["value"], 0)

    def test_serialised_payload_handles_null_impacts(self):
        """Stub rows with is_relevant=False have null impact values; the
        serialiser must not crash on them."""
        payload = {
            "market_id": "poly:stub",
            "is_relevant": False,
            "yes_co2_impact_mt": None,
            "no_co2_impact_mt": None,
        }
        out = env.apply_user_unit_preference(payload, "co2_mt")
        self.assertIsNone(out["yes_co2_impact_converted"]["value"])
        self.assertIsNone(out["no_co2_impact_converted"]["value"])


class TestContextIntegration(_DbFixture):
    def test_env_section_appears_for_climate_query(self):
        # Seed cache with one relevant impact
        db.upsert_environmental_impact("poly:climate", {
            "market_question": "Will US rejoin Paris?",
            "generated_at": int(time.time()),
            "generated_by": "test",
            "cache_valid_until": int(time.time()) + 3600,
            "is_relevant": True,
            "yes_co2_impact_mt": -2.1,
            "no_co2_impact_mt": 0.8,
            "yes_impact_timeframe": "over 10 years",
            "no_impact_timeframe": "per year",
            "confidence": "medium",
        })
        user = {"user_id": self.user_id}
        result = _run(ctx_mod.build_intelligence_context(
            user, "What's the climate impact of US policy markets?", []))
        self.assertIn("Environmental impact context", result["text"])
        self.assertIn("environmental", result["metadata"]["sections"])
        self.assertEqual(result["metadata"].get("env_impacts_count"), 1)

    def test_env_section_skipped_for_unrelated_query(self):
        db.upsert_environmental_impact("poly:climate2", {
            "market_question": "Q",
            "generated_at": int(time.time()),
            "generated_by": "test",
            "cache_valid_until": int(time.time()) + 3600,
            "is_relevant": True,
            "yes_co2_impact_mt": 1.0,
            "no_co2_impact_mt": 1.0,
        })
        user = {"user_id": self.user_id}
        result = _run(ctx_mod.build_intelligence_context(
            user, "What are the NBA finals odds?", []))
        self.assertNotIn("Environmental impact context", result["text"])
        self.assertNotIn("environmental", result["metadata"]["sections"])

    def test_env_section_not_added_when_cache_empty(self):
        # Climate-y query but no rows seeded.
        user = {"user_id": self.user_id}
        result = _run(ctx_mod.build_intelligence_context(
            user, "carbon emissions question", []))
        self.assertNotIn("Environmental impact context", result["text"])


if __name__ == "__main__":
    unittest.main()

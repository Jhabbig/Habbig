"""Scenarios — correlation + conditional probability + routes.

Covers:
  - pearson: known series, zero variance, short series
  - align_snapshot_series: overlap window, LOCF, no overlap
  - compute_market_correlations: filters < threshold, drops self, caches
  - estimate_shift: YES, NO, capped, invalid outcome
  - compute_scenario: empty correlations, anchor missing price
  - HTTP routes: Pro gate, saved scenarios round-trip, heatmap shape
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests import _testdb  # noqa: F401

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
import migrations
from scenarios import correlation, scenario


# ── Pure-math ───────────────────────────────────────────────────────────────


class TestPearson(unittest.TestCase):
    def test_perfect_positive(self):
        r = correlation.pearson([1, 2, 3, 4, 5], [2, 4, 6, 8, 10])
        self.assertAlmostEqual(r, 1.0, places=6)

    def test_perfect_negative(self):
        r = correlation.pearson([1, 2, 3, 4, 5], [10, 8, 6, 4, 2])
        self.assertAlmostEqual(r, -1.0, places=6)

    def test_zero_variance_returns_none(self):
        self.assertIsNone(correlation.pearson([1, 1, 1, 1], [2, 3, 4, 5]))

    def test_too_short_returns_none(self):
        self.assertIsNone(correlation.pearson([1, 2], [3, 4]))

    def test_mismatched_length_returns_none(self):
        self.assertIsNone(correlation.pearson([1, 2, 3], [4, 5]))

    def test_realistic_partial_correlation(self):
        a = [0.50, 0.52, 0.55, 0.57, 0.60, 0.58, 0.55, 0.52, 0.50]
        b = [0.40, 0.41, 0.43, 0.44, 0.46, 0.45, 0.43, 0.42, 0.40]
        r = correlation.pearson(a, b)
        self.assertIsNotNone(r)
        self.assertGreater(r, 0.95)


class TestAlignSeries(unittest.TestCase):
    def test_no_overlap(self):
        xs, ys = correlation.align_snapshot_series(
            [(100, 0.1), (200, 0.2)],
            [(500, 0.5), (600, 0.6)],
        )
        self.assertEqual(xs, [])
        self.assertEqual(ys, [])

    def test_locf_resamples_to_hourly_grid(self):
        # One-hour-apart series that exactly match the grid.
        a = [(3600 * i, 0.1 * i) for i in range(1, 6)]
        b = [(3600 * i, 0.05 * i) for i in range(1, 6)]
        xs, ys = correlation.align_snapshot_series(a, b)
        self.assertEqual(len(xs), len(ys))
        self.assertTrue(len(xs) >= 3)

    def test_single_overlap_point_is_stripped(self):
        # start==end after dedup → empty output.
        xs, ys = correlation.align_snapshot_series(
            [(100, 0.1), (200, 0.2)],
            [(100, 0.1)],
        )
        self.assertEqual(xs, [])


class TestEstimateShift(unittest.TestCase):
    def test_yes_direction_positive_r(self):
        # High corr, anchor near 0.5, hypothetical YES → positive shift.
        shift = scenario.estimate_shift(
            correlation=0.8,
            anchor_current_price=0.5,
            hypothetical_outcome="yes",
            other_volatility=0.05,
        )
        self.assertGreater(shift, 0)
        self.assertLessEqual(shift, scenario.MAX_SHIFT)

    def test_no_direction_flips_sign(self):
        shift_yes = scenario.estimate_shift(
            correlation=0.8, anchor_current_price=0.5,
            hypothetical_outcome="yes", other_volatility=0.05,
        )
        shift_no = scenario.estimate_shift(
            correlation=0.8, anchor_current_price=0.5,
            hypothetical_outcome="no", other_volatility=0.05,
        )
        self.assertGreater(shift_yes, 0)
        self.assertLess(shift_no, 0)

    def test_invalid_outcome_returns_zero(self):
        shift = scenario.estimate_shift(
            correlation=0.9, anchor_current_price=0.5,
            hypothetical_outcome="maybe", other_volatility=0.05,
        )
        self.assertEqual(shift, 0.0)

    def test_shift_capped_at_max(self):
        shift = scenario.estimate_shift(
            correlation=1.0, anchor_current_price=0.0,
            hypothetical_outcome="yes", other_volatility=0.5,
        )
        self.assertLessEqual(shift, scenario.MAX_SHIFT)


# ── DB-backed correlation ───────────────────────────────────────────────────


def _fresh_db() -> Path:
    p = Path(tempfile.mktemp(suffix=".db", prefix="narve-scenario-test-"))
    os.environ["GATEWAY_DB_PATH"] = str(p)
    conn = sqlite3.connect(p)
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
    return p


def _seed_snapshots(db_path: Path, slug: str, prices: list[float], start_ts: int) -> None:
    conn = sqlite3.connect(db_path)
    try:
        for i, p in enumerate(prices):
            conn.execute(
                "INSERT INTO market_snapshots "
                "(market_slug, market_question, category, yes_price, snapshotted_at) "
                "VALUES (?,?,?,?,?)",
                (slug, f"Will {slug}?", "finance", p, start_ts + i * 3600),
            )
        conn.commit()
    finally:
        conn.close()


class TestComputeCorrelations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = _fresh_db()
        now = int(time.time())
        start = now - 48 * 3600  # 48 hours ago
        # Anchor: rising then falling.
        anchor = [0.40 + 0.02 * i for i in range(12)] + [0.65 - 0.02 * i for i in range(12)]
        # Correlated market (positive): same shape, lower base.
        pos = [0.30 + 0.015 * i for i in range(12)] + [0.50 - 0.015 * i for i in range(12)]
        # Anti-correlated.
        neg = [0.60 - 0.02 * i for i in range(12)] + [0.35 + 0.02 * i for i in range(12)]
        # Uncorrelated (zigzag).
        noise = [0.50 + (0.05 if i % 2 == 0 else -0.05) for i in range(24)]
        _seed_snapshots(cls.db_path, "anchor", anchor, start)
        _seed_snapshots(cls.db_path, "pos-market", pos, start)
        _seed_snapshots(cls.db_path, "neg-market", neg, start)
        _seed_snapshots(cls.db_path, "noise-market", noise, start)

    def _run(self, coro):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_correlations_exclude_self(self):
        result = self._run(correlation.compute_market_correlations("anchor", use_cache=False))
        slugs = {r["slug"] for r in result}
        self.assertNotIn("anchor", slugs)

    def test_positive_correlation_returned_strong(self):
        result = self._run(correlation.compute_market_correlations("anchor", use_cache=False))
        by_slug = {r["slug"]: r for r in result}
        self.assertIn("pos-market", by_slug)
        self.assertGreater(by_slug["pos-market"]["correlation"], 0.7)

    def test_threshold_filters_weak(self):
        # Lowering the threshold should include at least as many rows as
        # raising it — and a mid-range threshold that excludes the
        # zigzag noise market proves the filter runs. We don't pin
        # min_abs=1.0 because seed data is deterministic-perfect on
        # the anti-correlated pair and would never be excluded.
        strict = self._run(correlation.compute_market_correlations(
            "anchor", min_abs=0.5, use_cache=False,
        ))
        permissive = self._run(correlation.compute_market_correlations(
            "anchor", min_abs=0.1, use_cache=False,
        ))
        strict_slugs = {r["slug"] for r in strict}
        permissive_slugs = {r["slug"] for r in permissive}
        # Permissive set contains the strict set.
        self.assertTrue(strict_slugs.issubset(permissive_slugs))
        # The noise market (low r) must not appear in the strict result.
        self.assertNotIn("noise-market", strict_slugs)

    def test_anti_correlated_negative_sign(self):
        result = self._run(correlation.compute_market_correlations("anchor", use_cache=False))
        by_slug = {r["slug"]: r for r in result}
        if "neg-market" in by_slug:
            self.assertLess(by_slug["neg-market"]["correlation"], 0)


class TestComputeScenario(unittest.TestCase):
    def _run(self, coro):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_invalid_hypothetical_returns_error(self):
        result = self._run(scenario.compute_scenario("anchor", "maybe"))
        self.assertIn("error", result)
        self.assertEqual(result["disclaimer"], scenario.DISCLAIMER)

    def test_unknown_anchor_returns_empty_shifts_note(self):
        result = self._run(scenario.compute_scenario(
            "no-such-anchor", "yes",
        ))
        self.assertEqual(result["shifts"], [])
        self.assertIn("disclaimer", result)


# ── HTTP routes ─────────────────────────────────────────────────────────────


class TestScenarioRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = _fresh_db()
        # Seed users (one pro, one free) + baseline market snapshots.
        now = int(time.time())
        conn = sqlite3.connect(cls.db_path)
        conn.execute(
            "INSERT INTO users (id, username, email, password_hash, password_salt, created_at) "
            "VALUES (?,?,?,'x','x',?)",
            (701, "pro.user", "pro@narve.ai", now),
        )
        conn.execute(
            "INSERT INTO users (id, username, email, password_hash, password_salt, created_at) "
            "VALUES (?,?,?,'x','x',?)",
            (702, "free.user", "free@narve.ai", now),
        )
        conn.commit()
        # One active market snapshot so the picker returns something.
        conn.execute(
            "INSERT INTO market_snapshots "
            "(market_slug, market_question, category, yes_price, snapshotted_at) "
            "VALUES ('fed-hold', 'Will the Fed hold rates?', 'finance', 0.67, ?)",
            (now - 3600,),
        )
        conn.commit()
        conn.close()

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import server as _server
        import scenarios_routes

        cls._server = _server

        cls._orig_current_user = _server.current_user
        cls._orig_plan_info = _server._user_plan_info

        def fake_current(request):
            return getattr(request.state, "_test_user", None)

        def fake_plan(user, subs, ts):
            return {"plan": (user or {}).get("_plan") or "none"}

        _server.current_user = fake_current
        _server._user_plan_info = fake_plan

        cls.app = FastAPI()

        @cls.app.middleware("http")
        async def _set(request, call_next):
            h = request.headers.get("x-test-user-json")
            if h:
                try:
                    request.state._test_user = json.loads(h)
                except Exception:
                    request.state._test_user = None
            else:
                request.state._test_user = None
            return await call_next(request)

        scenarios_routes.register(cls.app)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls._server.current_user = cls._orig_current_user
        cls._server._user_plan_info = cls._orig_plan_info
        try:
            cls.db_path.unlink(missing_ok=True)
        except Exception:
            pass

    def _pro(self):
        return {"x-test-user-json": json.dumps({
            "user_id": 701, "email": "pro@narve.ai",
            "is_admin": False, "_plan": "pro",
        })}

    def _free(self):
        return {"x-test-user-json": json.dumps({
            "user_id": 702, "email": "free@narve.ai",
            "is_admin": False, "_plan": "none",
        })}

    def test_markets_requires_auth(self):
        r = self.client.get("/api/scenario/markets")
        self.assertEqual(r.status_code, 401)

    def test_markets_requires_pro(self):
        r = self.client.get("/api/scenario/markets", headers=self._free())
        self.assertEqual(r.status_code, 402)

    def test_markets_returns_picker_rows_for_pro(self):
        r = self.client.get("/api/scenario/markets", headers=self._pro())
        self.assertEqual(r.status_code, 200)
        slugs = {m["slug"] for m in r.json()["markets"]}
        self.assertIn("fed-hold", slugs)

    def test_heatmap_includes_disclaimer(self):
        r = self.client.get("/api/scenario/heatmap", headers=self._pro())
        self.assertEqual(r.status_code, 200)
        self.assertIn("disclaimer", r.json())

    def test_compute_scenario_validates_outcome(self):
        r = self.client.post(
            "/api/scenario/compute",
            headers=self._pro(),
            data={"anchor_slug": "fed-hold", "hypothetical": "maybe"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("error", body)

    def test_save_then_list_round_trip(self):
        r = self.client.post(
            "/api/scenario/save",
            headers=self._pro(),
            data={"anchor_slug": "fed-hold", "hypothetical": "yes"},
        )
        self.assertEqual(r.status_code, 201)
        save_id = r.json()["saved_id"]
        r2 = self.client.get("/api/scenario/saved", headers=self._pro())
        self.assertEqual(r2.status_code, 200)
        saves = r2.json()["saved"]
        self.assertTrue(any(s["id"] == save_id for s in saves))

    def test_scenario_page_requires_pro(self):
        r = self.client.get("/tools/scenario", headers=self._free())
        self.assertEqual(r.status_code, 402)

    def test_scenario_page_renders_for_pro(self):
        r = self.client.get("/tools/scenario", headers=self._pro())
        self.assertEqual(r.status_code, 200)
        self.assertIn("Scenario calculator", r.text)
        self.assertIn(scenario.DISCLAIMER, r.text)

    def test_matrix_page_renders_for_pro(self):
        r = self.client.get("/tools/correlations", headers=self._pro())
        self.assertEqual(r.status_code, 200)
        self.assertIn("Correlation matrix", r.text)

    # ── Accessibility landmarks (post design-pass) ──

    def test_scenario_page_has_a11y_landmarks(self):
        r = self.client.get("/tools/scenario", headers=self._pro())
        self.assertEqual(r.status_code, 200)
        # Language attribute for screen readers.
        self.assertIn("<html lang='en'>", r.text)
        # Picker marked as a combobox pattern.
        self.assertIn("aria-autocomplete='list'", r.text)
        self.assertIn("aria-controls='picker-results'", r.text)
        self.assertIn("role='listbox'", r.text)
        # Outcome radios grouped via fieldset + radiogroup role.
        self.assertIn("<fieldset", r.text)
        self.assertIn("role='radiogroup'", r.text)
        # Results region is a live region.
        self.assertIn("aria-live='polite'", r.text)
        # Disclaimer keeps role=note.
        self.assertIn("role='note'", r.text)

    def test_matrix_page_has_a11y_landmarks(self):
        r = self.client.get("/tools/correlations", headers=self._pro())
        self.assertEqual(r.status_code, 200)
        self.assertIn("<html lang='en'>", r.text)
        # Heatmap busy state + role-annotated legend.
        self.assertIn("aria-busy='true'", r.text)
        self.assertIn("role='group'", r.text)
        self.assertIn("negative pattern", r.text)
        # Cells are announceable buttons (set client-side when the
        # matrix renders — we can at least verify the CSS class that
        # indicates the sign-aware pattern ships).
        self.assertIn(".cell.neg::before", r.text)


if __name__ == "__main__":
    unittest.main()

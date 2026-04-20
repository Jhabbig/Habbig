"""Tests for the Insider Trading Signal Detection feature.

Covers: signal storage, scoring, DB queries, API auth, leaderboard,
and job registration.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import _testdb  # noqa: F401
import db  # noqa: E402


def _insert_signal(
    source_name: str = "Sen. Test",
    signal_type: str = "congressional_trade",
    action: str = "bought",
    asset: str = "RTX (Raytheon)",
    amount: float = 250000,
    strength: str = "strong",
    filing_id: str = "",
    days_ago: int = 0,
) -> int:
    """Helper: insert a test insider signal."""
    now = int(time.time())
    filing_id = filing_id or f"test:{source_name}:{now}:{id(source_name)}"
    with db.conn() as c:
        cur = c.execute(
            """INSERT INTO insider_signals
                (signal_type, source_name, source_type, action, asset_or_entity,
                 amount_usd, disclosed_at, transaction_at, delay_days, fetched_at,
                 signal_strength, filing_id, committee, party, state, chamber)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                signal_type, source_name, "senator", action, asset,
                amount, now - days_ago * 86400, now - days_ago * 86400 - 86400 * 5,
                5, now, strength, filing_id, "Armed Services", "R", "TX", "senate",
            ),
        )
        return cur.lastrowid


def _insert_correlation(
    signal_id: int,
    market_id: str = "poly:test-market",
    implied_direction: str = "YES",
    confidence: str = "high",
    score: float = 0.85,
    resolved: bool = False,
    resolved_correct: bool = False,
) -> int:
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            """INSERT INTO insider_market_correlations
                (signal_id, market_id, market_question, correlation_type,
                 correlation_explanation, implied_direction, implied_confidence,
                 market_price_at_detection, insider_score, detected_at,
                 resolved, resolved_correct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                signal_id, market_id, "Will defence spending increase?",
                "direct", "Committee member buying defence contractor",
                implied_direction, confidence, 0.43, score, now,
                1 if resolved else 0,
                1 if resolved_correct else 0 if resolved else None,
            ),
        )
        return cur.lastrowid


class TestInsiderSignalStorage(unittest.TestCase):
    def test_insert_and_query(self):
        sid = _insert_signal(source_name="Sen. StorageTest", filing_id="storage:1")
        self.assertGreater(sid, 0)
        signals = db.get_insider_signals(days=1, limit=50)
        found = [s for s in signals if s["id"] == sid]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["source_name"], "Sen. StorageTest")

    def test_dedup_by_filing_id(self):
        from insider.base_fetcher import store_signals
        sig = {
            "signal_type": "congressional_trade",
            "source_name": "Sen. Dedup",
            "source_type": "senator",
            "action": "bought",
            "asset_or_entity": "RTX",
            "amount_usd": 100000,
            "disclosed_at": int(time.time()),
            "signal_strength": "moderate",
            "filing_id": "dedup:test:1",
        }
        count1 = store_signals([sig])
        count2 = store_signals([sig])  # duplicate
        self.assertEqual(count1, 1)
        self.assertEqual(count2, 0)

    def test_filter_by_type(self):
        _insert_signal(source_name="Sen. TypeFilter", signal_type="congressional_trade", filing_id="type:1")
        _insert_signal(source_name="CEO TypeFilter", signal_type="sec_form4", filing_id="type:2")
        congress = db.get_insider_signals(signal_type="congressional_trade", days=1)
        sec = db.get_insider_signals(signal_type="sec_form4", days=1)
        # At least the ones we just created
        congress_names = [s["source_name"] for s in congress]
        sec_names = [s["source_name"] for s in sec]
        self.assertIn("Sen. TypeFilter", congress_names)
        self.assertIn("CEO TypeFilter", sec_names)

    def test_filter_by_strength(self):
        _insert_signal(source_name="Sen. Strong", strength="strong", filing_id="str:1")
        _insert_signal(source_name="Sen. Weak", strength="weak", filing_id="str:2")
        strong = db.get_insider_signals(strength="strong", days=1)
        names = [s["source_name"] for s in strong]
        self.assertIn("Sen. Strong", names)
        self.assertNotIn("Sen. Weak", names)


class TestInsiderScoring(unittest.TestCase):
    def test_strong_signal_high_score(self):
        from insider.correlator import compute_insider_score
        score = compute_insider_score(
            signal_strength="strong",
            delay_days=3,
            amount_usd=1_000_000,
            correlation_confidence="high",
        )
        self.assertGreater(score, 0.8)

    def test_weak_signal_low_score(self):
        from insider.correlator import compute_insider_score
        score = compute_insider_score(
            signal_strength="weak",
            delay_days=45,
            amount_usd=5_000,
            correlation_confidence="low",
        )
        self.assertLess(score, 0.4)

    def test_score_always_in_range(self):
        from insider.correlator import compute_insider_score
        for s in ["strong", "moderate", "weak"]:
            for d in [0, 10, 30, 100]:
                for a in [0, 50000, 500000, 5000000]:
                    for c in ["high", "medium", "low"]:
                        score = compute_insider_score(s, d, a, c)
                        self.assertGreaterEqual(score, 0.0)
                        self.assertLessEqual(score, 1.0)


class TestInsiderCorrelations(unittest.TestCase):
    def test_correlations_for_signal(self):
        sid = _insert_signal(source_name="Sen. CorrTest", filing_id="corr:1")
        _insert_correlation(sid, market_id="poly:corr-market")
        corrs = db.get_insider_correlations_for_signal(sid)
        self.assertGreaterEqual(len(corrs), 1)
        self.assertEqual(corrs[0]["market_id"], "poly:corr-market")

    def test_signals_for_market(self):
        sid = _insert_signal(source_name="Sen. MktTest", filing_id="mkt:1")
        _insert_correlation(sid, market_id="poly:market-query-test")
        signals = db.get_insider_signals_for_market("poly:market-query-test", days=1)
        self.assertGreaterEqual(len(signals), 1)

    def test_empty_market_returns_empty(self):
        signals = db.get_insider_signals_for_market("poly:nonexistent", days=1)
        self.assertEqual(len(signals), 0)


class TestInsiderLeaderboard(unittest.TestCase):
    def test_leaderboard_requires_min_trades(self):
        # Create source with 5 resolved correlations
        for i in range(5):
            sid = _insert_signal(source_name="Sen. Leaderboard", filing_id=f"lb:{i}")
            _insert_correlation(sid, resolved=True, resolved_correct=True)

        leaderboard = db.get_insider_leaderboard(min_trades=3)
        names = [r["source_name"] for r in leaderboard]
        self.assertIn("Sen. Leaderboard", names)

    def test_leaderboard_excludes_below_min(self):
        sid = _insert_signal(source_name="Sen. OneTrade", filing_id="lb:one")
        _insert_correlation(sid, resolved=True, resolved_correct=True)

        leaderboard = db.get_insider_leaderboard(min_trades=3)
        names = [r["source_name"] for r in leaderboard]
        self.assertNotIn("Sen. OneTrade", names)


class TestInsiderSourceProfile(unittest.TestCase):
    def test_profile_returns_data(self):
        _insert_signal(source_name="Sen. Profile", filing_id="prof:1")
        profile = db.get_insider_source_profile("Sen. Profile")
        self.assertIsNotNone(profile)
        self.assertEqual(profile["source_name"], "Sen. Profile")
        self.assertGreaterEqual(profile["total_signals"], 1)

    def test_missing_source_returns_none(self):
        profile = db.get_insider_source_profile("Nobody")
        self.assertIsNone(profile)


class TestInsiderStats(unittest.TestCase):
    def test_stats_returns_dict(self):
        stats = db.get_insider_stats()
        self.assertIn("signals_today", stats)
        self.assertIn("strong_signals_30d", stats)
        self.assertIn("correlated_markets_30d", stats)


class TestInsiderPreferences(unittest.TestCase):
    def test_set_preferences(self):
        uid = db.create_user("insider_pref@test.com", "TestPass123!", username="insiderpref")
        db.set_insider_alert_preferences(uid, True, "moderate_and_above")
        with db.conn() as c:
            row = c.execute("SELECT insider_alerts_enabled, insider_alert_threshold FROM users WHERE id = ?", (uid,)).fetchone()
        self.assertEqual(row["insider_alerts_enabled"], 1)
        self.assertEqual(row["insider_alert_threshold"], "moderate_and_above")


class TestInsiderJobRegistration(unittest.TestCase):
    def test_all_jobs_registered(self):
        from jobs.registry import job_registry
        expected = [
            "fetch_congressional_trades",
            "fetch_sec_form4",
            "fetch_fec_campaign",
            "correlate_insider_signals",
            "resolve_insider_correlations",
        ]
        for name in expected:
            self.assertIn(name, job_registry, f"Job {name} not registered")

    def test_cron_entries_exist(self):
        from jobs.registry import cron_jobs
        insider_crons = [c for c in cron_jobs if c["name"].startswith("fetch_") or c["name"].startswith("correlate_insider") or c["name"].startswith("resolve_insider")]
        # Should have many cron entries across all insider jobs
        self.assertGreaterEqual(len(insider_crons), 5)


class TestInsiderAPIAuth(unittest.TestCase):
    """Verify insider endpoints require Pro tier."""

    def test_signals_requires_auth(self):
        import server
        from fastapi.testclient import TestClient
        client = TestClient(server.app)
        r = client.get("/api/insider/signals")
        self.assertIn(r.status_code, (401, 403))

    def test_leaderboard_requires_auth(self):
        import server
        from fastapi.testclient import TestClient
        client = TestClient(server.app)
        r = client.get("/api/insider/leaderboard")
        self.assertIn(r.status_code, (401, 403))

    def test_stats_requires_auth(self):
        import server
        from fastapi.testclient import TestClient
        client = TestClient(server.app)
        r = client.get("/api/insider/stats")
        self.assertIn(r.status_code, (401, 403))


class TestCongressionalStrength(unittest.TestCase):
    def test_committee_boost(self):
        from insider.congressional_trades import CongressionalTradesFetcher
        fetcher = CongressionalTradesFetcher()
        # Small amount + Armed Services committee + defence ticker → boosted
        strength = fetcher.calculate_signal_strength(
            amount_usd=10000,  # would normally be weak
            delay_days=15,
            committees=["Armed Services"],
            ticker="RTX",
            action="bought",
        )
        # Should be boosted from weak to moderate
        self.assertIn(strength, ("moderate", "strong"))

    def test_base_strength(self):
        from insider.congressional_trades import CongressionalTradesFetcher
        fetcher = CongressionalTradesFetcher()
        strong = fetcher.calculate_signal_strength(amount_usd=100000, delay_days=5)
        weak = fetcher.calculate_signal_strength(amount_usd=5000, delay_days=40)
        self.assertEqual(strong, "strong")
        self.assertEqual(weak, "weak")


if __name__ == "__main__":
    unittest.main()

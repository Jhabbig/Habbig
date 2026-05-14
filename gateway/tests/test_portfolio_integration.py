"""Tests for portfolio integration — Kelly, signal overlay, bankroll, sync.

Covers:
  - signal_for_position() agreement logic for YES/NO positions
  - Kelly formula edge cases (no edge → 0, capped at max_cap)
  - db helpers for user_positions and bankroll
  - /api/markets/sync rate limit
  - /api/markets/stats aggregate
  - /api/user/bankroll GET/PATCH validation
  - /api/kelly/calculate response shape + signal-less path
  - Trading add-on gating on all new endpoints
  - Kalshi 401 deactivates the connection
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import time
import unittest
from unittest.mock import AsyncMock, patch

# CRITICAL: pin db.conn BEFORE importing server — mirrors test_environmental_http.py.
os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ["RATE_LIMIT_ENABLED"] = "true"
os.environ["GLOBAL_RATE_LIMIT_PER_MIN"] = "10000"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from cryptography.fernet import Fernet
    os.environ.setdefault("CREDENTIALS_ENCRYPTION_KEY", Fernet.generate_key().decode())
except Exception:
    pass

import db  # noqa: E402

_conn = sqlite3.connect(":memory:", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA foreign_keys = ON")


@contextlib.contextmanager
def _fake_conn():
    try:
        yield _conn
        _conn.commit()
    except Exception:
        _conn.rollback()
        raise


db.conn = _fake_conn
db.init_db()

import migrations  # noqa: E402
migrations.upgrade_to_head()

import server  # noqa: E402
from backend.markets import unified_markets  # noqa: E402
from backend.markets.portfolio_signals import signal_for_position  # noqa: E402
from backend.markets.unified_markets import UnifiedMarket, compute_kelly_sizing  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app)


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_market(
    market_id: str = "poly:fed-hold",
    yes_price: float = 0.67,
    betyc_ev_score: float = 0.07,
    betyc_consensus: str = "YES",
    betyc_prediction_count: int = 3,
    betyc_avg_credibility: float = 0.79,
) -> UnifiedMarket:
    return UnifiedMarket(
        id=market_id, source="polymarket" if market_id.startswith("poly") else "kalshi",
        title="Will Fed hold rates?", category="finance",
        yes_price=yes_price, no_price=1 - yes_price,
        volume_usd=1_000_000, liquidity_usd=10_000,
        close_time=None, status="active", outcome=None,
        url="https://polymarket.com/x",
        betyc_ev_score=betyc_ev_score,
        betyc_avg_credibility=betyc_avg_credibility,
        betyc_prediction_count=betyc_prediction_count,
        betyc_consensus=betyc_consensus,
    )


def _make_trader_user(email: str, username: str) -> tuple[int, str]:
    uid = db.create_user(email, "TestPass123!", username=username)
    now = int(time.time())
    db.set_trading_addon(uid, True, period_end=now + 30 * 86400)
    token = db.create_session(uid)
    return uid, token


def _make_plain_user(email: str, username: str) -> tuple[int, str]:
    """User with no Trading Add-on — should 403 on every new route."""
    uid = db.create_user(email, "TestPass123!", username=username)
    token = db.create_session(uid)
    return uid, token


def _auth(token: str) -> dict:
    return {"Cookie": f"pm_gateway_session={token}"}


def _prime_csrf(token: str) -> str:
    """Prime a CSRF cookie by issuing a GET and return the token value.

    CSRFMiddleware (server.py:971) blocks JSON POSTs without a matching
    ``X-CSRF-Token`` header and ``_csrf`` cookie. Mirror the pattern from
    ``test_feedback_routes._prime_csrf`` so portfolio POSTs validate.
    """
    client.get("/feedback", cookies={"pm_gateway_session": token},
               follow_redirects=False)
    return client.cookies.get("_csrf") or ""


def _post_json(path: str, token: str, json_body: dict | None = None):
    """POST with session cookie + matching CSRF cookie/header pair."""
    csrf = _prime_csrf(token)
    return client.post(
        path,
        cookies={"pm_gateway_session": token, "_csrf": csrf},
        headers={"X-CSRF-Token": csrf},
        json=json_body if json_body is not None else {},
    )


# ── Kelly formula (pure) ───────────────────────────────────────────────────


class TestKellyFormula(unittest.TestCase):
    def test_no_edge_returns_zero_recommendation(self):
        # narve agrees with the market exactly → edge = 0
        r = compute_kelly_sizing(betyc_probability=0.60, market_yes_price=0.60, bankroll=10_000)
        self.assertEqual(r["recommended_amount"], 0)
        self.assertEqual(r["kelly_full_fraction"], 0)

    def test_negative_edge_returns_zero(self):
        # narve thinks YES is less likely than market → edge < 0 → bet NO.
        r = compute_kelly_sizing(betyc_probability=0.40, market_yes_price=0.60, bankroll=10_000)
        # side should flip to NO and recommend a positive bet
        self.assertEqual(r["side"], "NO")
        self.assertGreater(r["recommended_amount"], 0)

    def test_capped_at_max_cap(self):
        # Huge edge → Kelly fraction would blow past 25%. Spec default cap is 25%.
        r = compute_kelly_sizing(betyc_probability=0.95, market_yes_price=0.50, bankroll=10_000, fraction=1.0)
        self.assertLessEqual(r["kelly_adjusted_fraction"], 0.25 + 1e-9)
        # And the dollar amount tracks the adjusted fraction.
        self.assertLessEqual(r["recommended_amount"], 2500 + 1e-6)

    def test_half_kelly_is_half_of_full(self):
        full = compute_kelly_sizing(0.70, 0.50, 10_000, fraction=1.0)
        half = compute_kelly_sizing(0.70, 0.50, 10_000, fraction=0.5)
        self.assertAlmostEqual(half["kelly_adjusted_fraction"],
                               full["kelly_adjusted_fraction"] / 2, places=4)

    def test_zero_bankroll_returns_zero(self):
        r = compute_kelly_sizing(0.75, 0.50, 0)
        self.assertEqual(r["recommended_amount"], 0)


# ── signal_for_position (pure) ─────────────────────────────────────────────


class TestSignalForPosition(unittest.TestCase):
    def test_agree_when_user_yes_and_narve_yes(self):
        m = _make_market(betyc_consensus="YES", betyc_ev_score=0.07, yes_price=0.67)
        sig = signal_for_position({"side": "yes"}, m)
        self.assertEqual(sig["agreement"], "agree")
        self.assertAlmostEqual(sig["edge_pp"], 0.07, places=4)
        self.assertAlmostEqual(sig["narve_yes_probability"], 0.74, places=4)

    def test_disagree_when_user_no_but_narve_yes(self):
        m = _make_market(betyc_consensus="YES", betyc_ev_score=0.07, yes_price=0.67)
        sig = signal_for_position({"side": "no"}, m)
        self.assertEqual(sig["agreement"], "disagree")
        # Edge from the NO-holder's perspective is negative.
        self.assertLess(sig["edge_pp"], 0)

    def test_neutral_when_consensus_split(self):
        m = _make_market(betyc_consensus="SPLIT", betyc_ev_score=0.005, yes_price=0.50)
        sig = signal_for_position({"side": "yes"}, m)
        self.assertEqual(sig["agreement"], "neutral")

    def test_no_signal_when_ev_none(self):
        m = _make_market(betyc_consensus=None, betyc_ev_score=None, betyc_prediction_count=0)
        m.betyc_consensus = None
        sig = signal_for_position({"side": "yes"}, m)
        self.assertEqual(sig["agreement"], "no_signal")
        self.assertIsNone(sig["edge_pp"])

    def test_no_signal_when_market_missing(self):
        sig = signal_for_position({"side": "yes"}, None)
        self.assertEqual(sig["agreement"], "no_signal")


# ── DB: bankroll / positions / disconnect / stats ──────────────────────────


class TestDbHelpers(unittest.TestCase):
    def test_bankroll_default_and_set(self):
        uid = db.create_user("db1@test.com", "TestPass123!", username="db1")
        info = db.get_user_bankroll(uid)
        self.assertIsNone(info["bankroll"])
        self.assertAlmostEqual(info["kelly_fraction"], 0.5)

        db.set_user_bankroll(uid, bankroll=12_000, kelly_fraction=0.25)
        info = db.get_user_bankroll(uid)
        self.assertEqual(info["bankroll"], 12_000)
        self.assertAlmostEqual(info["kelly_fraction"], 0.25)

    def test_position_upsert_and_prune(self):
        uid = db.create_user("db2@test.com", "TestPass123!", username="db2")
        db.upsert_user_position(
            user_id=uid, platform="polymarket", market_id="poly:a",
            market_title="A", side="yes", shares=100,
            avg_entry_price=0.5, current_price=0.6,
            unrealised_pnl=10, position_value_usd=60,
        )
        db.upsert_user_position(
            user_id=uid, platform="polymarket", market_id="poly:b",
            market_title="B", side="no", shares=50,
            avg_entry_price=0.4, current_price=0.5,
            unrealised_pnl=5, position_value_usd=25,
        )
        rows = db.get_user_positions(uid)
        self.assertEqual(len(rows), 2)

        # Prune keeps only poly:a
        db.prune_stale_positions(uid, "polymarket", {("poly:a", "yes")})
        rows = db.get_user_positions(uid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["market_id"], "poly:a")

    def test_disconnect_keeps_row_clears_token(self):
        uid = db.create_user("db3@test.com", "TestPass123!", username="db3")
        db.upsert_market_credential(
            uid, "kalshi", kalshi_token="enc.token.blob", kalshi_member_id="m-42",
        )
        self.assertTrue(db.disconnect_market_credential(uid, "kalshi"))
        row = db.get_market_credential(uid, "kalshi")
        self.assertIsNotNone(row)
        self.assertEqual(row["is_active"], 0)
        self.assertIsNone(row["kalshi_token"])
        # member_id survives so the UI can still show "Reconnect jake@email.com".
        self.assertEqual(row["kalshi_member_id"], "m-42")

    def test_stats_aggregates_value_and_pnl(self):
        uid = db.create_user("db4@test.com", "TestPass123!", username="db4")
        db.upsert_user_position(
            user_id=uid, platform="polymarket", market_id="poly:x",
            market_title="X", side="yes", shares=100,
            avg_entry_price=0.5, current_price=0.6,
            unrealised_pnl=10, position_value_usd=60,
        )
        db.upsert_user_position(
            user_id=uid, platform="kalshi", market_id="kalshi:Y",
            market_title="Y", side="no", shares=50,
            avg_entry_price=0.4, current_price=0.5,
            unrealised_pnl=-5, position_value_usd=25,
        )
        stats = db.get_portfolio_stats(uid)
        self.assertEqual(stats["active_positions"], 2)
        self.assertAlmostEqual(stats["total_value_usd"], 85.0)
        self.assertAlmostEqual(stats["unrealised_pnl_usd"], 5.0)


# ── HTTP: bankroll endpoints ───────────────────────────────────────────────


_unique_ctr = 0


def _unique(prefix: str) -> str:
    global _unique_ctr
    _unique_ctr += 1
    return f"{prefix}{_unique_ctr}"


class TestBankrollEndpoint(unittest.TestCase):
    def setUp(self):
        slug = _unique("br")
        self.uid, self.token = _make_trader_user(f"{slug}@test.com", slug)
        client.cookies.clear()

    def test_get_returns_null_bankroll_initially(self):
        r = client.get("/api/user/bankroll", headers=_auth(self.token))
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["bankroll"])

    def test_patch_requires_trading_addon(self):
        slug = _unique("br_noaddon")
        _, t2 = _make_plain_user(f"{slug}@test.com", slug)
        r = client.patch("/api/user/bankroll", headers=_auth(t2), json={"bankroll": 5000})
        self.assertEqual(r.status_code, 403)

    def test_patch_rejects_negative(self):
        r = client.patch("/api/user/bankroll", headers=_auth(self.token), json={"bankroll": -1})
        self.assertEqual(r.status_code, 400)

    def test_patch_updates_and_round_trips(self):
        r = client.patch(
            "/api/user/bankroll", headers=_auth(self.token),
            json={"bankroll": 25_000, "kelly_fraction": 0.25},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["bankroll"], 25_000)
        self.assertAlmostEqual(body["kelly_fraction"], 0.25)

    def test_patch_rejects_kelly_fraction_zero(self):
        r = client.patch(
            "/api/user/bankroll", headers=_auth(self.token),
            json={"kelly_fraction": 0},
        )
        self.assertEqual(r.status_code, 400)


# ── HTTP: Kelly calculator ─────────────────────────────────────────────────


class TestKellyEndpoint(unittest.TestCase):
    def setUp(self):
        slug = _unique("kelly")
        self.uid, self.token = _make_trader_user(f"{slug}@test.com", slug)
        client.cookies.clear()

    def test_requires_market_id(self):
        r = _post_json("/api/kelly/calculate", self.token, {})
        self.assertEqual(r.status_code, 400)

    def test_requires_bankroll(self):
        # No bankroll set, no bankroll in body → 400.
        r = _post_json("/api/kelly/calculate", self.token,
                       {"market_id": "poly:fake"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("bankroll", r.json()["error"].lower())

    def test_404_for_missing_market(self):
        db.set_user_bankroll(self.uid, bankroll=10_000)
        with patch.object(
            unified_markets, "fetch_single_market",
            new=AsyncMock(return_value=None),
        ):
            r = _post_json("/api/kelly/calculate", self.token,
                           {"market_id": "poly:missing"})
        self.assertEqual(r.status_code, 404)

    def test_returns_three_tiers_with_signal(self):
        db.set_user_bankroll(self.uid, bankroll=10_000)
        m = _make_market(betyc_ev_score=0.07, yes_price=0.67)
        with patch.object(
            unified_markets, "fetch_single_market",
            new=AsyncMock(return_value=m),
        ), patch.object(
            unified_markets, "enrich_markets_with_intelligence",
            return_value=[m],
        ):
            r = _post_json("/api/kelly/calculate", self.token,
                           {"market_id": "poly:fed-hold"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["has_signal"])
        labels = {rec["label"] for rec in body["recommendations"]}
        self.assertEqual(labels, {"full", "half", "quarter"})
        # Full > half > quarter (all non-negative)
        by_label = {rec["label"]: rec for rec in body["recommendations"]}
        self.assertGreaterEqual(by_label["full"]["bet_amount_usd"],
                                by_label["half"]["bet_amount_usd"])
        self.assertGreaterEqual(by_label["half"]["bet_amount_usd"],
                                by_label["quarter"]["bet_amount_usd"])

    def test_no_signal_returns_200_with_empty_recs(self):
        db.set_user_bankroll(self.uid, bankroll=10_000)
        m = _make_market(betyc_ev_score=None, betyc_consensus=None, betyc_prediction_count=0)
        m.betyc_consensus = None
        m.betyc_ev_score = None
        with patch.object(
            unified_markets, "fetch_single_market",
            new=AsyncMock(return_value=m),
        ), patch.object(
            unified_markets, "enrich_markets_with_intelligence",
            return_value=[m],
        ):
            r = _post_json("/api/kelly/calculate", self.token,
                           {"market_id": "poly:fed-hold"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["has_signal"])
        self.assertEqual(r.json()["recommendations"], [])

    def test_requires_trading_addon(self):
        slug = _unique("kelly_noaddon")
        _, t2 = _make_plain_user(f"{slug}@test.com", slug)
        r = _post_json("/api/kelly/calculate", t2, {"market_id": "poly:x"})
        self.assertEqual(r.status_code, 403)


# ── HTTP: sync + stats + addon gate ────────────────────────────────────────


class TestSyncStats(unittest.TestCase):
    def setUp(self):
        slug = _unique("sync")
        self.uid, self.token = _make_trader_user(f"{slug}@test.com", slug)
        client.cookies.clear()
        # Purge prior sync rate-limit so the 1/min limit doesn't bleed between tests.
        import server as _s
        _s._rate_store.clear()

    def test_stats_returns_zeros_for_fresh_user(self):
        r = client.get("/api/markets/stats", headers=_auth(self.token))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["active_positions"], 0)
        self.assertEqual(body["total_value_usd"], 0)
        self.assertIsNone(body["win_rate"])

    def test_sync_requires_trading_addon(self):
        slug = _unique("sync_noaddon")
        _, t2 = _make_plain_user(f"{slug}@test.com", slug)
        r = client.post("/api/markets/sync", headers=_auth(t2))
        self.assertEqual(r.status_code, 403)

    def test_sync_rate_limited_1_per_minute(self):
        # Stub out the expensive portfolio fetch so the test is fast and
        # deterministic — we're only checking the rate limiter.
        # NOTE: _build_enriched_portfolio now lives in market_routes (the
        # route module), not server.py — see market_routes.py:64.
        import market_routes as _mr  # noqa: E402

        async def _fake_build(user_id):
            return {"combined_total_usd": 0, "polymarket": {"positions": []},
                    "kalshi": {"positions": []}}

        with patch.object(_mr, "_build_enriched_portfolio", new=_fake_build):
            r1 = _post_json("/api/markets/sync", self.token)
            r2 = _post_json("/api/markets/sync", self.token)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 429)


# ── HTTP: portfolio endpoint (signal overlay + Kalshi 401) ────────────────


class TestPortfolioEndpoint(unittest.TestCase):
    def setUp(self):
        slug = _unique("port")
        self.uid, self.token = _make_trader_user(f"{slug}@test.com", slug)
        client.cookies.clear()

    def test_kalshi_token_expired_deactivates_connection(self):
        db.upsert_market_credential(
            self.uid, "kalshi", kalshi_token="enc.abc", kalshi_member_id="m-1",
        )
        # Simulate the aggregator surfacing a Kalshi 401.
        async def _fake(*args, **kwargs):
            return {
                "kalshi": {"connected": True, "positions": [], "balance": 0,
                           "total_value": 0, "error": "token_expired"},
                "polymarket": {"connected": False, "positions": [], "balance_usdc": 0,
                               "total_value": 0},
                "combined_total_usd": 0,
            }

        with patch(
            "backend.markets.portfolio_sync.get_combined_portfolio",
            new=_fake,
        ), patch.object(
            unified_markets, "fetch_unified_markets",
            new=AsyncMock(return_value=[]),
        ), patch.object(
            unified_markets, "enrich_markets_with_intelligence",
            return_value=[],
        ):
            r = client.get("/api/markets/portfolio", headers=_auth(self.token))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["kalshi"]["is_active"])
        # Credential row flipped to inactive but is NOT deleted.
        row = db.get_market_credential(self.uid, "kalshi")
        self.assertIsNotNone(row)
        self.assertEqual(row["is_active"], 0)

    def test_positions_get_enriched_with_narve_signal(self):
        db.upsert_market_credential(
            self.uid, "polymarket", polymarket_wallet_address="0xabc",
        )
        m = _make_market(market_id="poly:fed-hold", yes_price=0.67,
                         betyc_ev_score=0.07, betyc_consensus="YES")

        async def _fake(*args, **kwargs):
            return {
                "kalshi": {"connected": False, "positions": [], "total_value": 0,
                           "balance": 0},
                "polymarket": {
                    "connected": True, "total_value": 100.0, "balance_usdc": 0,
                    "positions": [{
                        "market_id": "poly:fed-hold",
                        "market_title": "Fed hold?",
                        "platform": "polymarket",
                        "side": "yes", "shares": 100, "avg_price": 0.55,
                        "current_price": 0.67, "pnl": 12.0, "value": 67.0,
                    }],
                },
                "combined_total_usd": 100.0,
            }

        with patch(
            "backend.markets.portfolio_sync.get_combined_portfolio",
            new=_fake,
        ), patch.object(
            unified_markets, "fetch_unified_markets",
            new=AsyncMock(return_value=[m]),
        ), patch.object(
            unified_markets, "enrich_markets_with_intelligence",
            return_value=[m],
        ):
            r = client.get("/api/markets/portfolio", headers=_auth(self.token))
        self.assertEqual(r.status_code, 200)
        poly_positions = r.json()["polymarket"]["positions"]
        self.assertEqual(len(poly_positions), 1)
        sig = poly_positions[0]["narve_signal"]
        self.assertEqual(sig["agreement"], "agree")
        self.assertAlmostEqual(sig["edge_pp"], 0.07, places=3)
        # Persistence: row should now exist in user_positions.
        rows = db.get_user_positions(self.uid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["market_id"], "poly:fed-hold")


if __name__ == "__main__":
    unittest.main()

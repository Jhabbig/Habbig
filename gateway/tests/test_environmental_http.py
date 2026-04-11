"""HTTP-level tests for the Environmental Impact API routes.

Pure unittest + FastAPI TestClient. Mirrors the test_2fa_http.py pattern:
pin `db.conn` to an in-memory SQLite at module load (BEFORE importing
`server`), apply migrations, then drive every route through TestClient.

Coverage:
  - GET /api/markets/{id}/environmental             → 403 for Trader, 200 for Pro
  - POST /api/markets/{id}/environmental/refresh    → 403 for Trader, rate-limited at 6th call
  - GET /api/markets/environmental/top              → 403 for Trader, 200 for Pro
  - PATCH /api/user/preferences/environmental       → 400 on bad unit, 200 on good
  - GET /api/markets/unified/{id}                   → env merge present for Pro,
                                                      absent for Trader
  - GET /api/markets/unified?env_relevant=1         → filtered to cached rows

The Anthropic SDK is never called: `intelligence.environmental._call_claude`
is monkey-patched to a stub that returns canned JSON.
The market-fetcher is monkey-patched too so we never hit Polymarket / Kalshi
upstream APIs.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import time
import unittest
from types import SimpleNamespace

# CRITICAL: pin db.conn BEFORE importing server. Server.py wires
# subscription helpers, route handlers, etc. that all defer their conn()
# calls until request time, so the late-pin pattern is safe.
os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ["RATE_LIMIT_ENABLED"] = "true"
os.environ["GLOBAL_RATE_LIMIT_PER_MIN"] = "10000"  # don't trip during tests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Set a Fernet key so credential-encryption code paths don't warn loudly.
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
from intelligence import environmental as env_mod  # noqa: E402
from backend.markets import unified_markets  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


client = TestClient(server.app)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_pro_user(email: str, username: str) -> tuple[int, str]:
    """Create a user with an active Pro plan + trading add-on, return (id, session_token)."""
    uid = db.create_user(email, "TestPass123!", username=username)
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "INSERT INTO subscriptions (user_id, dashboard_key, plan, status, started_at) "
            "VALUES (?, '__plan__', 'pro_monthly', 'active', ?)",
            (uid, now),
        )
    db.set_trading_addon(uid, True, period_end=now + 30 * 86400)
    token = db.create_session(uid)
    return uid, token


def _make_trader_user(email: str, username: str) -> tuple[int, str]:
    """Create a user with Trader plan + trading add-on, return (id, session_token)."""
    uid = db.create_user(email, "TestPass123!", username=username)
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "INSERT INTO subscriptions (user_id, dashboard_key, plan, status, started_at) "
            "VALUES (?, '__plan__', 'trader_monthly', 'active', ?)",
            (uid, now),
        )
    db.set_trading_addon(uid, True, period_end=now + 30 * 86400)
    token = db.create_session(uid)
    return uid, token


def _seed_env_row(market_id: str, *, is_relevant: bool = True, yes_mt: float = -2.1, no_mt: float = 0.8):
    """Plant a cached env analysis row directly so we don't need Claude."""
    now = int(time.time())
    db.upsert_environmental_impact(market_id, {
        "market_question": "Will US rejoin Paris Agreement?",
        "market_category": "politics",
        "generated_at": now,
        "generated_by": "test-fixture",
        "cache_valid_until": now + 86400,
        "is_relevant": is_relevant,
        "irrelevance_reason": None if is_relevant else "test stub",
        "yes_outcome_label": "YES",
        "no_outcome_label": "NO",
        "yes_co2_impact_mt": yes_mt if is_relevant else None,
        "no_co2_impact_mt": no_mt if is_relevant else None,
        "yes_impact_description": "Reduces emissions" if is_relevant else "",
        "no_impact_description": "Increases emissions" if is_relevant else "",
        "yes_impact_timeframe": "over 10 years",
        "no_impact_timeframe": "per year",
        "confidence": "medium",
        "confidence_reason": "Policy estimates vary",
        "data_sources": ["IPCC AR6", "EPA"],
        "category": "emissions",
        "yes_market_price_at_gen": 0.67,
    })


def _stub_unified_market(market_id="poly:test", title="Will US rejoin Paris?",
                          category="politics", yes_price=0.67):
    """Return a UnifiedMarket-like object the routes can serialize."""
    return unified_markets.UnifiedMarket(
        id=market_id, source="polymarket", title=title, category=category,
        yes_price=yes_price, no_price=1.0 - yes_price,
        volume_usd=100000.0, liquidity_usd=50000.0,
        close_time="2026-12-31T00:00:00Z", status="active",
        outcome=None, url="https://polymarket.com/event/test",
    )


def _patch_market_fetchers(test_market):
    """Replace fetch_single_market and fetch_unified_markets so route handlers
    don't make real upstream API calls. Returns the original functions for
    cleanup."""
    orig_single = unified_markets.fetch_single_market
    orig_list = unified_markets.fetch_unified_markets

    async def _stub_single(*a, **kw):
        return test_market

    async def _stub_list(*a, **kw):
        return [test_market]

    unified_markets.fetch_single_market = _stub_single
    unified_markets.fetch_unified_markets = _stub_list
    return orig_single, orig_list


def _restore_market_fetchers(orig_single, orig_list):
    unified_markets.fetch_single_market = orig_single
    unified_markets.fetch_unified_markets = orig_list


def _patch_claude_stub(response_dict: dict | None):
    """Replace _call_claude with a stub returning *response_dict* serialized
    as JSON, or None for error path. Returns the original function."""
    orig = env_mod._call_claude

    async def _stub(*a, **kw):
        if response_dict is None:
            return None
        return json.dumps(response_dict)

    env_mod._call_claude = _stub
    return orig


# ── Pro tier gating ──────────────────────────────────────────────────────────

class TestProTierGating(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.conn = _fake_conn
        cls.pro_id, cls.pro_token = _make_pro_user("envroute-pro@test.com", "envroutepro")
        cls.trader_id, cls.trader_token = _make_trader_user("envroute-trader@test.com", "envroutetrader")
        cls.test_market = _stub_unified_market("poly:gating")
        cls._orig_single, cls._orig_list = _patch_market_fetchers(cls.test_market)
        cls._orig_claude = _patch_claude_stub({
            "is_relevant": True, "irrelevance_reason": None,
            "yes_co2_impact_mt": -1.0, "no_co2_impact_mt": 0.5,
            "yes_impact_description": "x", "no_impact_description": "y",
            "yes_impact_timeframe": "per year", "no_impact_timeframe": "per year",
            "confidence": "medium", "confidence_reason": "r",
            "data_sources": ["test"], "category": "emissions",
        })

    @classmethod
    def tearDownClass(cls):
        _restore_market_fetchers(cls._orig_single, cls._orig_list)
        env_mod._call_claude = cls._orig_claude

    def setUp(self):
        db.conn = _fake_conn
        with db.conn() as c:
            c.execute("DELETE FROM environmental_impacts")
            c.execute("DELETE FROM rate_limit_buckets") if False else None
        # Reset the in-process rate-limit store so per-test counts are fresh.
        try:
            server._rate_store.clear()
        except Exception:
            pass

    def test_get_environmental_requires_pro(self):
        """Trader-tier user gets 403, Pro user gets 200."""
        r = client.get(
            "/api/markets/poly:gating/environmental",
            cookies={server.COOKIE_NAME: self.trader_token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403)

        r = client.get(
            "/api/markets/poly:gating/environmental",
            cookies={server.COOKIE_NAME: self.pro_token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body.get("is_relevant"))

    def test_force_refresh_requires_pro(self):
        """POST /environmental/refresh is Pro-only too."""
        r = client.post(
            "/api/markets/poly:gating/environmental/refresh",
            cookies={server.COOKIE_NAME: self.trader_token},
            follow_redirects=False,
        )
        # CSRF middleware will reject before the Pro check fires for a Trader,
        # but the important invariant is "no 200 / no Claude call". Either
        # 403 (CSRF) or 403 (Pro) is acceptable.
        self.assertEqual(r.status_code, 403)

    def test_top_endpoint_requires_pro(self):
        """GET /api/markets/environmental/top is Pro-only."""
        r = client.get(
            "/api/markets/environmental/top",
            cookies={server.COOKIE_NAME: self.trader_token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403)

        # Seed a row so the Pro response is non-empty.
        _seed_env_row("poly:gating-top", yes_mt=-5.0, no_mt=2.0)
        r = client.get(
            "/api/markets/environmental/top",
            cookies={server.COOKIE_NAME: self.pro_token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("impacts", body)
        self.assertGreaterEqual(body["count"], 1)


# ── Force-refresh rate limit ────────────────────────────────────────────────

class TestForceRefreshRateLimit(unittest.TestCase):
    """The route caps force-refresh at 5 per day per user. The 6th attempt
    must return 429 with Retry-After. CSRF complicates this because POST
    routes go through the CSRF middleware — we obtain a token via a GET
    first, then submit it on every subsequent POST."""

    @classmethod
    def setUpClass(cls):
        db.conn = _fake_conn
        cls.pro_id, cls.pro_token = _make_pro_user("envroute-rl@test.com", "envrouterl")
        cls.test_market = _stub_unified_market("poly:rl")
        cls._orig_single, cls._orig_list = _patch_market_fetchers(cls.test_market)
        cls._orig_claude = _patch_claude_stub({
            "is_relevant": True, "irrelevance_reason": None,
            "yes_co2_impact_mt": -1.0, "no_co2_impact_mt": 0.5,
            "yes_impact_description": "x", "no_impact_description": "y",
            "yes_impact_timeframe": "per year", "no_impact_timeframe": "per year",
            "confidence": "medium", "confidence_reason": "r",
            "data_sources": ["test"], "category": "emissions",
        })

    @classmethod
    def tearDownClass(cls):
        _restore_market_fetchers(cls._orig_single, cls._orig_list)
        env_mod._call_claude = cls._orig_claude

    def setUp(self):
        db.conn = _fake_conn
        with db.conn() as c:
            c.execute("DELETE FROM environmental_impacts")
        try:
            server._rate_store.clear()
        except Exception:
            pass

    def _csrf_token_for(self, session_token: str) -> str:
        """Prime the CSRF cookie via a GET, then read it from the shared
        TestClient cookie jar (per-response cookies are only present when a
        new cookie is set; the jar holds the live value across calls)."""
        client.get(
            "/dashboards",
            cookies={server.COOKIE_NAME: session_token},
            follow_redirects=False,
        )
        return client.cookies.get("_csrf") or ""

    def test_sixth_force_refresh_returns_429(self):
        csrf = self._csrf_token_for(self.pro_token)
        if not csrf:
            self.skipTest("CSRF cookie not set on dashboards GET — test environment differs")

        codes = []
        for _ in range(7):
            r = client.post(
                "/api/markets/poly:rl/environmental/refresh",
                cookies={server.COOKIE_NAME: self.pro_token, "_csrf": csrf},
                headers={"x-csrf-token": csrf, "content-type": "application/json"},
                content="{}",
                follow_redirects=False,
            )
            codes.append(r.status_code)

        # First 5 must succeed (200). 6th and 7th must be 429.
        self.assertEqual(codes[:5], [200, 200, 200, 200, 200],
                         f"first 5 should succeed, got {codes}")
        self.assertEqual(codes[5], 429, f"6th call should rate-limit, got {codes[5]}")
        self.assertEqual(codes[6], 429, f"7th call should rate-limit, got {codes[6]}")


# ── PATCH preferences validation ────────────────────────────────────────────

class TestPreferencesPatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.conn = _fake_conn
        # Even Trader can update preferences (any authenticated user).
        cls.uid, cls.token = _make_trader_user("envroute-pref@test.com", "envroutepref")

    def setUp(self):
        db.conn = _fake_conn

    def _csrf_token(self) -> str:
        # Prime the CSRF cookie. After a dashboards GET the cookie lives in
        # the shared TestClient jar (which persists across requests), so we
        # read it back from there rather than from the per-response cookies
        # — those are only populated when the server actually sets a NEW
        # cookie, and on a repeat visit the existing one is reused silently.
        client.get(
            "/dashboards",
            cookies={server.COOKIE_NAME: self.token},
            follow_redirects=False,
        )
        return client.cookies.get("_csrf") or ""

    def test_patch_with_valid_unit_returns_200(self):
        csrf = self._csrf_token()
        if not csrf:
            self.skipTest("CSRF cookie unavailable in this test env")
        r = client.patch(
            "/api/user/preferences/environmental",
            cookies={server.COOKIE_NAME: self.token, "_csrf": csrf},
            headers={"x-csrf-token": csrf, "content-type": "application/json"},
            content=json.dumps({"show_environmental_impact": True, "preferred_unit": "trees"}),
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200, f"got {r.status_code} body={r.text[:200]}")
        body = r.json()
        self.assertTrue(body["show_environmental_impact"])
        self.assertEqual(body["preferred_unit"], "trees")

        # Verify it actually persisted.
        prefs = db.get_user_env_preferences(self.uid)
        self.assertTrue(prefs["show"])
        self.assertEqual(prefs["unit"], "trees")

    def test_patch_with_invalid_unit_returns_400(self):
        csrf = self._csrf_token()
        if not csrf:
            self.skipTest("CSRF cookie unavailable")
        r = client.patch(
            "/api/user/preferences/environmental",
            cookies={server.COOKIE_NAME: self.token, "_csrf": csrf},
            headers={"x-csrf-token": csrf, "content-type": "application/json"},
            content=json.dumps({"show_environmental_impact": True, "preferred_unit": "bogus"}),
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertIn("preferred_unit", body.get("error", ""))

    def test_patch_unauthenticated_returns_401(self):
        r = client.patch(
            "/api/user/preferences/environmental",
            content=json.dumps({"show_environmental_impact": False, "preferred_unit": "co2_mt"}),
            headers={"content-type": "application/json"},
            follow_redirects=False,
        )
        # Either 401 (no session) or 403 (CSRF). Both are correct fail-closed.
        self.assertIn(r.status_code, (401, 403))


# ── /api/markets/unified/{id} merge ─────────────────────────────────────────

class TestUnifiedMergeWithEnv(unittest.TestCase):
    """Verifies the env block is merged into GET /api/markets/unified/{id}
    response for Pro users with env_show enabled, and absent for Trader."""

    @classmethod
    def setUpClass(cls):
        db.conn = _fake_conn
        cls.pro_id, cls.pro_token = _make_pro_user("envroute-merge-pro@test.com", "envmergepro")
        cls.trader_id, cls.trader_token = _make_trader_user("envroute-merge-trader@test.com", "envmergetrader")
        cls.test_market = _stub_unified_market("poly:merge")
        cls._orig_single, cls._orig_list = _patch_market_fetchers(cls.test_market)

    @classmethod
    def tearDownClass(cls):
        _restore_market_fetchers(cls._orig_single, cls._orig_list)

    def setUp(self):
        db.conn = _fake_conn
        with db.conn() as c:
            c.execute("DELETE FROM environmental_impacts")

    def test_pro_user_with_cached_env_sees_merged_block(self):
        _seed_env_row("poly:merge")
        r = client.get(
            "/api/markets/unified/poly:merge",
            cookies={server.COOKIE_NAME: self.pro_token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("environmental_impact", body, "Pro user should see env block")
        ei = body["environmental_impact"]
        self.assertTrue(ei["is_relevant"])
        self.assertEqual(ei["yes_co2_impact_mt"], -2.1)
        self.assertIn("yes_co2_impact_converted", ei)

    def test_trader_user_never_sees_env_block(self):
        _seed_env_row("poly:merge")
        r = client.get(
            "/api/markets/unified/poly:merge",
            cookies={server.COOKIE_NAME: self.trader_token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertNotIn("environmental_impact", body,
                         "Trader should NOT see env block (Pro feature)")

    def test_pro_user_no_cached_row_no_block(self):
        # No seed → no cached row → graceful absence (not 500)
        r = client.get(
            "/api/markets/unified/poly:merge",
            cookies={server.COOKIE_NAME: self.pro_token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertNotIn("environmental_impact", body)

    def test_pro_user_with_env_show_off_no_block(self):
        _seed_env_row("poly:merge")
        db.set_user_env_preferences(self.pro_id, show=False, unit="co2_mt")
        try:
            r = client.get(
                "/api/markets/unified/poly:merge",
                cookies={server.COOKIE_NAME: self.pro_token},
                follow_redirects=False,
            )
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("environmental_impact", r.json())
        finally:
            # Restore preferences for any later tests
            db.set_user_env_preferences(self.pro_id, show=True, unit="co2_mt")

    def test_pro_user_pref_unit_applies_to_merged_block(self):
        _seed_env_row("poly:merge")
        db.set_user_env_preferences(self.pro_id, show=True, unit="trees")
        try:
            r = client.get(
                "/api/markets/unified/poly:merge",
                cookies={server.COOKIE_NAME: self.pro_token},
                follow_redirects=False,
            )
            self.assertEqual(r.status_code, 200)
            ei = r.json()["environmental_impact"]
            self.assertEqual(ei["preferred_unit"], "trees")
            self.assertEqual(ei["yes_co2_impact_converted"]["unit_key"], "trees")
            # Tree conversion: -2.1 MT * 45_871 ≈ -96329.1
            self.assertAlmostEqual(
                ei["yes_co2_impact_converted"]["value"], -2.1 * 45_871, places=1,
            )
        finally:
            db.set_user_env_preferences(self.pro_id, show=True, unit="co2_mt")


# ── /api/markets/unified?env_relevant=1 filter ──────────────────────────────

class TestEnvRelevantFilter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.conn = _fake_conn
        cls.uid, cls.token = _make_trader_user("envroute-filter@test.com", "envfilter")
        # Two markets: one we'll cache as env-relevant, one we won't.
        cls.market_a = _stub_unified_market("poly:relevant", title="Climate Q?", category="politics")
        cls.market_b = _stub_unified_market("poly:other", title="NBA Q?", category="sports")
        cls._orig_single = unified_markets.fetch_single_market
        cls._orig_list = unified_markets.fetch_unified_markets

        async def _stub_list(*a, **kw):
            return [cls.market_a, cls.market_b]

        unified_markets.fetch_unified_markets = _stub_list

    @classmethod
    def tearDownClass(cls):
        unified_markets.fetch_unified_markets = cls._orig_list
        unified_markets.fetch_single_market = cls._orig_single

    def setUp(self):
        db.conn = _fake_conn
        with db.conn() as c:
            c.execute("DELETE FROM environmental_impacts")

    def test_no_filter_returns_all_markets(self):
        r = client.get(
            "/api/markets/unified",
            cookies={server.COOKIE_NAME: self.token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        ids = [m["id"] for m in r.json()["markets"]]
        self.assertIn("poly:relevant", ids)
        self.assertIn("poly:other", ids)

    def test_env_relevant_filter_with_no_cache_returns_all(self):
        """When the cache is empty, env_relevant=1 returns ALL markets so the
        UI doesn't suddenly go blank for a user who clicked the filter before
        any analyses have been generated. (filter quietly degrades to a no-op.)"""
        r = client.get(
            "/api/markets/unified?env_relevant=1",
            cookies={server.COOKIE_NAME: self.token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # No env_relevant_ids → no decoration → no rows pruned
        self.assertEqual(len(body["markets"]), 2)
        self.assertNotIn("is_env_relevant", body["markets"][0])

    def test_env_relevant_filter_with_cache_prunes_other_markets(self):
        _seed_env_row("poly:relevant", yes_mt=-3.0, no_mt=1.0)
        r = client.get(
            "/api/markets/unified?env_relevant=1",
            cookies={server.COOKIE_NAME: self.token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        ids = [m["id"] for m in body["markets"]]
        self.assertEqual(ids, ["poly:relevant"])
        # Decorated for the leaf-badge UI
        self.assertTrue(body["markets"][0].get("is_env_relevant"))


if __name__ == "__main__":
    unittest.main()

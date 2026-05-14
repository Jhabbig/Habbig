"""Tests for /admin/cost-alerts — Anthropic AI cost monitoring.

Covers the four guarantees the prompt asked for:

  - The page renders for an admin (200 + key sections present).
  - Anonymous / non-admin callers get 403 (or a redirect to gate).
  - The kill-switch toggle endpoint requires a valid CSRF token —
    missing token returns 403 from the global middleware.
  - The per-feature breakdown sums to the same total as
    :func:`queries.ai_cost.get_total_cost` for the same window.

Auth setup mirrors ``test_admin_jobs.py`` — seed an admin user + session
in the SQLite DB, mark it 2FA-verified, then drive the FastAPI app via
``TestClient``.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import server  # noqa: E402


_CSRF_TOKEN = "test-csrf-token-cost-alerts-suite"


def _create_admin_session(*, super_admin: bool = True) -> str:
    """Create (or reuse) an admin user and return a 2FA-verified session.

    ``super_admin`` controls admin_level — the kill-switch toggle endpoint
    requires admin_level >= 2, so the default mirrors the prompt's
    "admin-gated" requirement for the destructive route.
    """
    role = 2 if super_admin else 1
    email = f"cost_alerts_admin_{os.getpid()}_{role}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        user_id = existing["id"]
    else:
        user_id = db.create_user(
            email, "Password1!verylong",
            username=f"cost_alerts_admin_{os.getpid()}_{role}",
        )
    db.set_user_role(user_id, role)
    try:
        db.set_user_2fa_method(user_id, "email_otp")
    except Exception:
        pass
    token = db.create_session(user_id)
    try:
        db.mark_session_two_fa_verified(token)
    except Exception:
        pass
    return token


def _create_regular_session() -> str:
    email = f"cost_alerts_user_{os.getpid()}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        uid = existing["id"]
        db.set_user_role(uid, 0)
    else:
        uid = db.create_user(
            email, "Password1!verylong",
            username=f"cost_alerts_user_{os.getpid()}",
        )
        db.set_user_role(uid, 0)
    return db.create_session(uid)


def _admin_cookies(session_token: str) -> dict:
    return {
        server.COOKIE_NAME: session_token,
        server.CSRF_COOKIE_NAME: _CSRF_TOKEN,
    }


def _csrf_headers() -> dict:
    return {server.CSRF_HEADER_NAME: _CSRF_TOKEN}


def _seed_usage_row(*, feature: str, cost_usd: float,
                    ts_offset_seconds: int = -300,
                    input_tokens: int = 1000,
                    output_tokens: int = 500,
                    cached_hit: int = 0) -> int:
    """Insert one row into ``claude_usage_log``. Returns the row id."""
    now = int(time.time()) + ts_offset_seconds
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO claude_usage_log "
            "(timestamp, feature, model, input_tokens, output_tokens, "
            " cost_usd, cached_hit) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, feature, "claude-haiku-4-5", input_tokens, output_tokens,
             float(cost_usd), cached_hit),
        )
        return int(cur.lastrowid or 0)


def _seed_alert_row(*, alert_date: str, threshold_usd: float,
                    total_cost_usd: float, sent_offset: int = -60) -> int:
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO claude_cost_alerts "
            "(alert_date, threshold_usd, total_cost_usd, sent_at) "
            "VALUES (?, ?, ?, ?)",
            (alert_date, float(threshold_usd), float(total_cost_usd),
             int(time.time()) + sent_offset),
        )
        return int(cur.lastrowid or 0)


class AdminCostAlertsTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import migrations as _migrations
            _migrations.upgrade_to_head()
        except Exception:
            pass
        cls.client = TestClient(server.app)
        cls.admin_session = _create_admin_session(super_admin=True)
        cls.admin_l1_session = _create_admin_session(super_admin=False)
        cls.user_session = _create_regular_session()

    def setUp(self):
        # Clear the cost + alert tables so each test runs against a known
        # slate. Failures here mean the migration isn't applied — the
        # outer setUpClass already attempted to ``upgrade_to_head`` so we
        # surface that as a hard failure rather than silently masking it.
        with db.conn() as c:
            c.execute("DELETE FROM claude_usage_log")
            c.execute("DELETE FROM claude_cost_alerts")
            # Reset the kill-switch to OFF so cross-test state can't leak.
            try:
                c.execute(
                    "UPDATE claude_kill_switch SET active=0, reason=NULL, "
                    "triggered_at=NULL, triggered_by=NULL WHERE id=1"
                )
            except Exception:
                pass

    # ── Auth ─────────────────────────────────────────────────────────

    def test_page_anonymous_denied(self):
        """Anonymous callers must not see the cost-alerts page."""
        r = self.client.get(
            "/admin/cost-alerts",
            cookies={},
            follow_redirects=False,
        )
        # _denied_response redirects unauth users to /gate (302/303) and
        # returns the 403 page for authed-but-not-admin. Both are valid
        # "not authorised" outcomes for an anonymous caller.
        self.assertIn(r.status_code, (302, 303, 403))

    def test_page_non_admin_403(self):
        r = self.client.get(
            "/admin/cost-alerts",
            cookies={server.COOKIE_NAME: self.user_session},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403)

    def test_refresh_api_anonymous_403(self):
        r = self.client.get("/admin/api/ai-cost/refresh", cookies={})
        self.assertEqual(r.status_code, 403)

    def test_refresh_api_non_admin_403(self):
        r = self.client.get(
            "/admin/api/ai-cost/refresh",
            cookies={server.COOKIE_NAME: self.user_session},
        )
        self.assertEqual(r.status_code, 403)

    # ── Page renders ─────────────────────────────────────────────────

    def test_page_admin_200_renders_sections(self):
        _seed_usage_row(feature="extraction", cost_usd=0.42)
        _seed_usage_row(feature="categorisation", cost_usd=0.18)
        _seed_alert_row(alert_date="2026-05-13",
                        threshold_usd=50.0, total_cost_usd=72.5)
        r = self.client.get(
            "/admin/cost-alerts",
            cookies=_admin_cookies(self.admin_session),
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        body = r.text
        # Hero in Instrument Serif Italic.
        self.assertIn("AI Cost Alerts", body)
        self.assertIn("cost-alerts__display", body)
        # Stat cards (mtd + 24h).
        self.assertIn("Spend · month-to-date", body)
        self.assertIn("Spend · trailing 24h", body)
        # Kill-switch + table sections.
        self.assertIn("Kill-switch", body)
        self.assertIn("Daily spend", body)
        self.assertIn("Per-feature spend", body)
        self.assertIn("Recent cost alerts", body)
        # Per-feature row surfaces seeded data.
        self.assertIn("extraction", body)
        self.assertIn("categorisation", body)
        # Alert log surfaces seeded data.
        self.assertIn("2026-05-13", body)
        # The toggle endpoint URL is rendered into the form action.
        self.assertIn("/admin/ai-cost/kill-switch", body)

    def test_page_empty_state(self):
        """Page must render cleanly when no usage / alerts have been logged."""
        r = self.client.get(
            "/admin/cost-alerts",
            cookies=_admin_cookies(self.admin_session),
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        body = r.text
        self.assertIn("AI Cost Alerts", body)
        # Empty-state copy for both tables.
        self.assertIn("No cost alerts logged", body)
        self.assertIn("No Claude calls in the last 24 hours", body)

    # ── JSON refresh ─────────────────────────────────────────────────

    def test_refresh_api_returns_expected_shape(self):
        _seed_usage_row(feature="summarisation", cost_usd=1.25)
        r = self.client.get(
            "/admin/api/ai-cost/refresh",
            cookies=_admin_cookies(self.admin_session),
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        for key in ("mtd_usd", "trailing_24h_usd", "daily", "features",
                    "alerts", "kill_switch", "generated_at"):
            self.assertIn(key, data, f"missing key: {key}")
        # daily must always be a 30-element series so the chart x-axis
        # paints evenly even on an idle install.
        self.assertEqual(len(data["daily"]), 30)
        features = data["features"]
        self.assertTrue(any(f["feature"] == "summarisation" for f in features))

    # ── Per-feature sums to total ────────────────────────────────────

    def test_per_feature_breakdown_sums_to_total(self):
        """The per-feature table and the headline number must agree.

        The 24h total comes from :func:`get_total_cost`; the table sum
        comes from :func:`get_per_feature_costs`. Both read the same
        rows, but the test pins down that future refactors don't drift
        the rollup logic out of sync (e.g. by dropping cache-hit rows
        from one path but not the other).
        """
        from queries import ai_cost as ai_cost_q

        _seed_usage_row(feature="extraction", cost_usd=0.10)
        _seed_usage_row(feature="extraction", cost_usd=0.20)
        _seed_usage_row(feature="categorisation", cost_usd=0.35)
        _seed_usage_row(feature="summarisation", cost_usd=1.05)
        # Cache hits log $0 cost — they should appear in calls counts
        # but not affect the cost sum either way.
        _seed_usage_row(feature="extraction", cost_usd=0.0, cached_hit=1)
        # An old row (>24h ago) must be excluded from both sums.
        _seed_usage_row(
            feature="extraction", cost_usd=99.99,
            ts_offset_seconds=-60 * 60 * 26,
        )

        total = ai_cost_q.get_total_cost(window_hours=24)
        features = ai_cost_q.get_per_feature_costs(window_hours=24)
        per_feature_sum = round(sum(f["cost_usd"] for f in features), 4)

        # Both sums should match each other AND the seeded fresh-data total.
        self.assertAlmostEqual(total, per_feature_sum, places=4)
        self.assertAlmostEqual(total, 1.70, places=4)
        # And the >24h row must NOT have been counted.
        self.assertNotIn(99.99, [f["cost_usd"] for f in features])
        # Cache-hit row contributes a call but no cost.
        extraction = next(f for f in features if f["feature"] == "extraction")
        self.assertEqual(extraction["calls"], 3)  # 2 paid + 1 cached
        self.assertAlmostEqual(extraction["cost_usd"], 0.30, places=4)

    # ── CSRF enforcement on the toggle endpoint ──────────────────────

    def test_kill_switch_requires_csrf_token(self):
        """A POST without an ``x-csrf-token`` header must be rejected.

        The double-submit cookie pattern requires the header to match
        the cookie value. Posting without either is the classic CSRF
        attack vector — the middleware should 403 before the route runs.
        """
        # Caller has a session cookie but no CSRF cookie + no header.
        # We deliberately bypass _admin_cookies() to ensure the _csrf
        # cookie isn't carried over — that's the property we're testing.
        r = self.client.post(
            "/admin/ai-cost/kill-switch",
            cookies={server.COOKIE_NAME: self.admin_session},
            json={"active": True, "reason": "manual test"},
        )
        # 403 from the CSRF middleware (the route never gets to run).
        self.assertEqual(r.status_code, 403)

    def test_kill_switch_toggles_with_valid_csrf(self):
        """A super-admin with a matching CSRF header + cookie can toggle."""
        from queries import ai_cost as ai_cost_q

        # Sanity: start OFF.
        self.assertFalse(ai_cost_q.get_kill_switch_status()["active"])
        r = self.client.post(
            "/admin/ai-cost/kill-switch",
            cookies=_admin_cookies(self.admin_session),
            headers=_csrf_headers(),
            json={"active": True, "reason": "test trip"},
        )
        self.assertEqual(r.status_code, 200, msg=r.text)
        body = r.json()
        self.assertTrue(body.get("active"))
        self.assertEqual(body.get("reason"), "test trip")
        # Round-trips through the canonical state-of-truth.
        self.assertTrue(ai_cost_q.get_kill_switch_status()["active"])

        # Deactivate via the same path.
        r2 = self.client.post(
            "/admin/ai-cost/kill-switch",
            cookies=_admin_cookies(self.admin_session),
            headers=_csrf_headers(),
            json={"active": False},
        )
        self.assertEqual(r2.status_code, 200, msg=r2.text)
        self.assertFalse(r2.json().get("active"))
        self.assertFalse(ai_cost_q.get_kill_switch_status()["active"])

    def test_kill_switch_requires_super_admin(self):
        """A level-1 admin should not be able to flip the kill-switch.

        admin_level 1 sees the page but the toggle is gated behind 2.
        """
        r = self.client.post(
            "/admin/ai-cost/kill-switch",
            cookies=_admin_cookies(self.admin_l1_session),
            headers=_csrf_headers(),
            json={"active": True, "reason": "should fail"},
        )
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()

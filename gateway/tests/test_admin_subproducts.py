"""Tests for the polished /admin/subproducts page (13-subproduct rollup).

Covers:
  * Auth: anon is bounced (302 to /gate) or 403, never 200.
  * Page render: every one of the 13 subproducts appears on the page.
  * Pro tier rollup: visible at the top with Pro MRR math (£180 * active).
  * Per-product MRR math: sum of (active_subscribers * price_usd_cents).
  * Helper unit tests:
      - count_active_subscribers
      - get_mrr_by_dashboard
      - get_churn_rate
      - get_new_signups
      - get_signups_daily_series
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
import subproduct as _sp  # noqa: E402


def _wipe_subs() -> None:
    with db.conn() as c:
        c.execute("DELETE FROM subscriptions")


def _seed_user(suffix: str) -> int:
    email = f"adsubprod_{suffix}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        return int(existing["id"])
    return int(
        db.create_user(email, "Password1!verylong", username=f"adsubprod_{suffix}")
    )


def _create_admin_session() -> str:
    uid = _seed_user(f"admin_{os.getpid()}")
    db.set_user_role(uid, 2)
    try:
        db.set_user_2fa_method(uid, "email_otp")
    except Exception:
        pass
    token = db.create_session(uid)
    try:
        db.mark_session_two_fa_verified(token)
    except Exception:
        pass
    return token


def _seed_active_sub(user_suffix: str, dashboard_key: str, *,
                     started_offset_days: int = 0,
                     duration_days: int = 30) -> None:
    """Seed one active subscription, optionally backdating started_at."""
    uid = _seed_user(user_suffix)
    db.upsert_subscription(
        user_id=uid,
        dashboard_key=dashboard_key,
        plan="monthly",
        duration_days=duration_days,
        source="placeholder",
    )
    if started_offset_days:
        backdated = int(time.time()) - started_offset_days * 86400
        with db.conn() as c:
            c.execute(
                "UPDATE subscriptions SET started_at = ? "
                "WHERE user_id = ? AND dashboard_key = ?",
                (backdated, uid, dashboard_key),
            )


# ── Pure helper tests (no HTTP) ─────────────────────────────────────────────


class TestSubscriptionHelpers(unittest.TestCase):
    def setUp(self) -> None:
        _wipe_subs()

    def test_count_active_subscribers_zero_when_empty(self):
        self.assertEqual(db.count_active_subscribers("sports"), 0)

    def test_count_active_subscribers_counts_active_only(self):
        _seed_active_sub("ca_1", "sports")
        _seed_active_sub("ca_2", "sports")
        _seed_active_sub("ca_3", "weather")
        self.assertEqual(db.count_active_subscribers("sports"), 2)
        self.assertEqual(db.count_active_subscribers("weather"), 1)
        self.assertEqual(db.count_active_subscribers("crypto"), 0)

    def test_get_mrr_by_dashboard_uses_catalogue_prices(self):
        _seed_active_sub("mrr_1", "sports")  # $19.99 → 1999¢
        _seed_active_sub("mrr_2", "sports")
        _seed_active_sub("mrr_3", "weather")  # $7.99 → 799¢
        out = db.get_mrr_by_dashboard()
        self.assertEqual(out["sports"], 2 * 1999)
        self.assertEqual(out["weather"], 1 * 799)
        # Products with no active subscribers come back at 0.
        self.assertEqual(out.get("crypto"), 0)

    def test_get_mrr_by_dashboard_has_all_thirteen_keys(self):
        out = db.get_mrr_by_dashboard()
        # One entry per subproduct dashboard_key.
        for slug in _sp.SUBPRODUCTS:
            dk = _sp.DASHBOARD_KEY_FOR_SLUG[slug]
            self.assertIn(dk, out)
        self.assertEqual(len(out), 13)

    def test_get_new_signups_window(self):
        _seed_active_sub("ns_1", "sports", started_offset_days=2)
        _seed_active_sub("ns_2", "sports", started_offset_days=45)
        # 30-day window catches the first but not the second.
        self.assertEqual(db.get_new_signups(window_days=30, dashboard_key="sports"), 1)
        # 60-day window catches both.
        self.assertEqual(db.get_new_signups(window_days=60, dashboard_key="sports"), 2)

    def test_get_churn_rate_zero_when_no_history(self):
        self.assertEqual(db.get_churn_rate(window_days=7), 0.0)

    def test_get_churn_rate_in_unit_interval(self):
        _seed_active_sub("cr_1", "sports")
        _seed_active_sub("cr_2", "sports")
        rate = db.get_churn_rate(window_days=7, dashboard_key="sports")
        self.assertGreaterEqual(rate, 0.0)
        self.assertLessEqual(rate, 1.0)

    def test_get_signups_daily_series_length(self):
        series = db.get_signups_daily_series(window_days=90, dashboard_key="sports")
        self.assertEqual(len(series), 90)
        # All zero by default — but the structure should still be a list of ints.
        self.assertTrue(all(isinstance(v, int) and v >= 0 for v in series))

    def test_get_signups_daily_series_buckets_recent_signup(self):
        _seed_active_sub("ds_1", "sports", started_offset_days=0)
        series = db.get_signups_daily_series(window_days=90, dashboard_key="sports")
        # The signup landed inside the window — somewhere in the array.
        self.assertEqual(sum(series), 1)


# ── HTTP integration tests via FastAPI TestClient ──────────────────────────


class TestAdminSubproductsPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
        os.environ.pop("PRODUCTION", None)
        try:
            import migrations as _migrations
            _migrations.upgrade_to_head()
        except Exception:
            pass
        cls.client = TestClient(server.app)
        cls.admin_cookies = {server.COOKIE_NAME: _create_admin_session()}

    def setUp(self) -> None:
        _wipe_subs()
        # Seed at least one active subscription per subproduct so MRR > 0 and
        # the page renders representative numbers.
        for i, slug in enumerate(_sp.SUBPRODUCTS):
            dk = _sp.DASHBOARD_KEY_FOR_SLUG[slug]
            _seed_active_sub(f"per_sp_{i}", dk)
        # Two Pro subs (the bundle marker).
        _seed_active_sub("pro_a", "__plan__")
        _seed_active_sub("pro_b", "__plan__")

    def test_anon_does_not_get_200(self):
        # Anon users hit /admin/* — must not reach the page. The shipping
        # behaviour is a 302 to /gate; the contract is "not 200" so we don't
        # tightly couple to the redirect choice.
        r = self.client.get("/admin/subproducts", cookies={}, follow_redirects=False)
        self.assertNotEqual(r.status_code, 200)
        # Most useful in practice: it should either be 302 (gate redirect)
        # or 403 (denied).
        self.assertIn(r.status_code, (302, 303, 401, 403))

    def test_admin_renders_200(self):
        r = self.client.get(
            "/admin/subproducts", cookies=self.admin_cookies, follow_redirects=False
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("Subproducts", r.text)

    def test_all_thirteen_subproducts_listed(self):
        r = self.client.get("/admin/subproducts", cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        for slug, cfg in _sp.SUBPRODUCTS.items():
            self.assertIn(
                cfg["name"], r.text, f"Subproduct {slug!r} ({cfg['name']!r}) missing from page",
            )

    def test_pro_rollup_present(self):
        r = self.client.get("/admin/subproducts", cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        # Pro hero
        self.assertIn("narve.ai Pro", r.text)
        # Pro MRR: 2 active * £180 = £360
        self.assertIn("&pound;360", r.text)
        # Active Pro subs label + value
        self.assertIn("Active Pro subs", r.text)

    def test_mrr_math_matches_helper(self):
        r = self.client.get("/admin/subproducts", cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        mrr_by_dk = db.get_mrr_by_dashboard()
        total_mrr_cents = sum(mrr_by_dk.values())
        # The summary MRR label "Subproduct MRR (sum)" should reflect the
        # total of every product MRR in cents.
        expected_str = f"${total_mrr_cents / 100:,.2f}"
        self.assertIn(expected_str, r.text)


if __name__ == "__main__":
    unittest.main()

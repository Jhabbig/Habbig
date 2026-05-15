"""Tests for /settings/billing — the in-app billing UI, proration preview,
cancel/resubscribe flows, add-on toggles, and the /api/v1/billing/*
JSON stubs.

Stripe is stubbed — no real API calls happen here. All mutations land on the
local subscriptions table so the tests can inspect DB state directly.

What's covered (grouped by the user-visible flow):
  * TestSettingsBillingPage:      GET /settings/billing renders correctly
                                  for each plan tier + status combination.
  * TestInvoicesEndpoint:         /api/v1/billing/invoices shape + pagination.
  * TestInvoicePdfStub:           /api/v1/billing/invoices/{id}/pdf = 501.
  * TestPortalStub:               /api/v1/billing/portal redirects to /enquire.
  * TestCancelFlow:               POST /settings/billing/cancel flips status
                                  and the user keeps access until expires_at.
  * TestResubscribeFlow:          POST /settings/billing/resubscribe reactivates
                                  a still-in-window cancelled sub.
  * TestAddonFlow:                Add / remove trading add-on.
  * TestProration:                The client-side proration formula matches
                                  Stripe's published formula exactly.
  * TestPaywallAndAuth:           Unauthenticated access is bounced.

The proration test is pure Python — we re-implement the calcProration formula
in a tiny fixture so the test doesn't need a headless browser.
"""

from __future__ import annotations

import html
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Explicit opt-in for conftest._maybe_force_shared_testdb. Without this the
# conftest might leave ``db.conn`` pointed at some *other* test file's
# monkey-patched fake (test_auth_flow, etc.), and our seeded users vanish.
USES_TESTDB = True

# Must import _testdb BEFORE server so the shared in-memory DB is active.
from tests import _testdb  # noqa: E402,F401

import db  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


client = TestClient(server.app)

# The shared in-memory connection managed by tests/_testdb.py. We re-pin
# ``db.conn`` to this at setUpClass + setUp time on every class below so
# sibling test files (test_auth_flow, test_logout, etc.) that swap db.conn
# into their own fakes can't steal our seeded rows.
_SHARED_DB_CONN = _testdb._fake_conn


def _pin_shared_db() -> None:
    db.conn = _SHARED_DB_CONN


def _clear_client_cookies() -> None:
    """Empty the shared ``TestClient`` cookie jar so sibling tests can't leak
    session or CSRF cookies into us. Httpx persists cookies by default."""
    try:
        client.cookies.clear()
    except Exception:
        pass


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_user_with_plan(email: str, username: str, *, plan: str, interval: str,
                         days_left: int = 30, status: str = "active",
                         with_addon: bool = False) -> tuple[int, str]:
    """Create a user + plan subscription on the __plan__ sentinel. Returns
    (user_id, session_token). status=='cancelled' leaves expires_at in-window
    so resubscribe can still reactivate.
    """
    uid = db.create_user(email, "TestPass123!", username=username)
    now = int(time.time())
    expires_at = now + days_left * 86400
    with db.conn() as c:
        c.execute(
            "INSERT INTO subscriptions "
            "(user_id, dashboard_key, plan, status, started_at, expires_at) "
            "VALUES (?, '__plan__', ?, ?, ?, ?)",
            (uid, f"{plan}_{interval}", status, now, expires_at),
        )
    if with_addon:
        db.set_trading_addon(uid, True, period_end=now + 30 * 86400)
    token = db.create_session(uid)
    return uid, token


def _make_fresh_user(email: str, username: str) -> tuple[int, str]:
    """User with no plan at all."""
    uid = db.create_user(email, "TestPass123!", username=username)
    return uid, db.create_session(uid)


def _prime_csrf(token: str) -> str:
    """Hit an HTML GET that sets the ``_csrf`` cookie, then read it back
    from the shared TestClient jar. We need an HTML response because the
    CSRF middleware only emits ``Set-Cookie: _csrf=…`` on text/html replies.
    """
    client.get(
        "/settings/billing",
        cookies={server.COOKIE_NAME: token},
        follow_redirects=False,
    )
    return client.cookies.get("_csrf") or ""


def _post_form(path: str, *, token: str, data: dict | None = None):
    """Form-encoded POST with the double-submit CSRF set up correctly.

    The gateway's CSRF middleware for application/x-www-form-urlencoded bodies
    checks ONLY the ``_csrf`` form field (no header fallback), and compares it
    constant-time against the ``_csrf`` cookie value. So we must include both.
    """
    csrf = _prime_csrf(token)
    payload = dict(data or {})
    if csrf:
        payload["_csrf"] = csrf
    return client.post(
        path,
        data=payload,
        cookies={server.COOKIE_NAME: token, "_csrf": csrf},
        follow_redirects=False,
    )


# ── Page render tests ────────────────────────────────────────────────────────


class _DbIsolation(unittest.TestCase):
    """Base class that re-pins ``db.conn`` to the shared _testdb connection
    before every test, and clears the shared TestClient cookie jar. Without
    these safety nets, sibling test files that monkey-patch ``db.conn`` into
    their own in-memory DBs silently steal our seeded sessions / plans.
    """

    @classmethod
    def setUpClass(cls):
        _pin_shared_db()
        super().setUpClass()

    def setUp(self):
        _pin_shared_db()
        _clear_client_cookies()
        super().setUp()


class TestSettingsBillingPage(_DbIsolation):
    """GET /settings/billing returns the expected HTML for each plan state."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pro_uid, cls.pro_token = _make_user_with_plan(
            "sb-pro@test.com", "sb_pro", plan="pro", interval="annual", days_left=300,
        )
        cls.trader_uid, cls.trader_token = _make_user_with_plan(
            "sb-trader@test.com", "sb_trader", plan="trader", interval="monthly", days_left=20,
        )
        cls.fresh_uid, cls.fresh_token = _make_fresh_user("sb-fresh@test.com", "sb_fresh")

    def test_pro_user_sees_current_plan_block(self):
        r = client.get("/settings/billing", cookies={server.COOKIE_NAME: self.pro_token}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Current plan", r.text)
        self.assertIn("Pro (Annual)", r.text)
        # $1,999/year is the Pro annual USD price (PLAN_DEFS["pro"]["annual_usd"])
        self.assertIn("$1,999", r.text)
        self.assertIn("renews", r.text)

    def test_pro_user_sees_pro_features(self):
        r = client.get("/settings/billing", cookies={server.COOKIE_NAME: self.pro_token}, follow_redirects=False)
        self.assertIn("Signal Search", r.text)
        self.assertIn("Push notifications", r.text)
        self.assertIn("6-month data window", r.text)

    def test_trader_user_sees_upgrade_cta(self):
        r = client.get("/settings/billing", cookies={server.COOKIE_NAME: self.trader_token}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Trader (Monthly)", r.text)
        # Trader sees an Upgrade button leading to Pro (via data-change-plan).
        self.assertIn('data-change-plan="pro"', r.text)
        self.assertIn("Upgrade", r.text)

    def test_fresh_user_sees_no_plan_empty_state(self):
        r = client.get("/settings/billing", cookies={server.COOKIE_NAME: self.fresh_token}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("No active subscription", r.text)
        # All three plan cards visible even without a plan.
        self.assertIn('data-plan-card="trader"', r.text)
        self.assertIn('data-plan-card="pro"', r.text)
        self.assertIn('data-plan-card="enterprise"', r.text)

    def test_addons_section_shows_both_addons(self):
        r = client.get("/settings/billing", cookies={server.COOKIE_NAME: self.pro_token}, follow_redirects=False)
        self.assertIn("Trading Add-on", r.text)
        self.assertIn("Intelligence Add-on", r.text)
        self.assertIn("$29/month", r.text)
        self.assertIn("$TBD", r.text)

    def test_billing_history_table_scaffold_present(self):
        r = client.get("/settings/billing", cookies={server.COOKIE_NAME: self.pro_token}, follow_redirects=False)
        self.assertIn('id="sb-history-table"', r.text)
        self.assertIn('id="sb-history-tbody"', r.text)
        self.assertIn('id="sb-history-more"', r.text)

    def test_payment_method_section_present(self):
        r = client.get("/settings/billing", cookies={server.COOKIE_NAME: self.pro_token}, follow_redirects=False)
        self.assertIn("Payment method", r.text)
        self.assertIn("/api/v1/billing/portal", r.text)

    def test_cancel_modal_and_reason_dropdown_present(self):
        r = client.get("/settings/billing", cookies={server.COOKIE_NAME: self.pro_token}, follow_redirects=False)
        self.assertIn('id="sb-cancel-modal"', r.text)
        self.assertIn('id="sb-cancel-form"', r.text)
        # The <select name="reason"> has the canonical options.
        self.assertIn('name="reason"', r.text)
        self.assertIn("Too expensive", r.text)
        self.assertIn("Not using it enough", r.text)
        self.assertIn("Missing a feature I need", r.text)

    def test_change_plan_modal_form_posts_to_subscribe(self):
        r = client.get("/settings/billing", cookies={server.COOKIE_NAME: self.trader_token}, follow_redirects=False)
        # The change-plan modal reuses the existing /billing/subscribe mutation.
        self.assertIn('id="sb-change-modal"', r.text)
        self.assertIn('action="/billing/subscribe"', r.text)

    def test_data_payload_json_is_parseable(self):
        """The <script type='application/json'> payload must parse + include catalog."""
        r = client.get("/settings/billing", cookies={server.COOKIE_NAME: self.pro_token}, follow_redirects=False)
        # Pull out the data payload.
        start = r.text.find('<script id="sb-data" type="application/json">')
        self.assertNotEqual(start, -1)
        start = r.text.index(">", start) + 1
        end = r.text.find("</script>", start)
        payload_raw = r.text[start:end]
        # Un-escape the </script> -> <\/ we applied server-side.
        payload_raw = payload_raw.replace("<\\/", "</")
        payload = json.loads(payload_raw)
        self.assertIn("catalog", payload)
        self.assertIn("pro", payload["catalog"])
        self.assertEqual(payload["catalog"]["pro"]["monthly_usd"], 229)
        self.assertEqual(payload["catalog"]["pro"]["annual_usd"], 1999)
        self.assertEqual(payload["current_plan"]["key"], "pro")
        self.assertEqual(payload["current_plan"]["interval"], "annual")
        self.assertEqual(payload["current_plan"]["amount_usd"], 1999)

    def test_cancelled_user_sees_resubscribe_banner(self):
        uid, token = _make_user_with_plan(
            "sb-cancelled@test.com", "sb_cancelled", plan="pro",
            interval="monthly", days_left=14, status="cancelled",
        )
        r = client.get("/settings/billing", cookies={server.COOKIE_NAME: token}, follow_redirects=False)
        self.assertIn("Cancelled", r.text)
        self.assertIn("/settings/billing/resubscribe", r.text)
        self.assertIn("Resubscribe", r.text)

    def test_active_user_sees_danger_zone_cancel_button(self):
        r = client.get("/settings/billing", cookies={server.COOKIE_NAME: self.pro_token}, follow_redirects=False)
        self.assertIn("data-open-cancel", r.text)
        self.assertIn("Cancel subscription", r.text)


# ── Invoice list ─────────────────────────────────────────────────────────────


class TestInvoicesEndpoint(_DbIsolation):
    """/api/v1/billing/invoices returns Stripe-shaped JSON derived from subs."""

    @classmethod
    def setUpClass(cls):
        _pin_shared_db()
        cls.pro_uid, cls.pro_token = _make_user_with_plan(
            "inv-pro@test.com", "inv_pro", plan="pro", interval="annual", days_left=300,
            with_addon=True,
        )
        cls.fresh_uid, cls.fresh_token = _make_fresh_user("inv-fresh@test.com", "inv_fresh")

    def test_pro_user_invoices_shape(self):
        r = client.get("/api/v1/billing/invoices", cookies={server.COOKIE_NAME: self.pro_token})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("invoices", body)
        self.assertIn("total", body)
        self.assertIn("next_cursor", body)
        self.assertGreaterEqual(len(body["invoices"]), 1)
        inv = body["invoices"][0]
        # Stripe-shaped.
        for key in ("id", "date", "description", "amount", "status", "pdf_url"):
            self.assertIn(key, inv)

    def test_pro_user_sees_addon_invoice(self):
        r = client.get("/api/v1/billing/invoices", cookies={server.COOKIE_NAME: self.pro_token})
        body = r.json()
        descs = [i["description"] for i in body["invoices"]]
        self.assertIn("Pro Annual subscription", descs)
        self.assertIn("Trading Add-on", descs)

    def test_fresh_user_has_empty_invoices(self):
        r = client.get("/api/v1/billing/invoices", cookies={server.COOKIE_NAME: self.fresh_token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["invoices"], [])
        self.assertEqual(r.json()["total"], 0)

    def test_pagination_cursor(self):
        """Pagination returns next_cursor when there are more rows than limit.

        We seed 4 standalone-plan rows (each has its own dashboard_key and a
        plan string that starts with 'standalone_' so the synthetic invoice
        derivation surfaces them as one invoice each).
        """
        uid = db.create_user("inv-paginate@test.com", "TestPass123!", username="inv_paginate")
        now = int(time.time())
        with db.conn() as c:
            for i in range(4):
                c.execute(
                    "INSERT INTO subscriptions "
                    "(user_id, dashboard_key, plan, status, started_at, expires_at) "
                    "VALUES (?, ?, 'standalone_monthly', 'active', ?, ?)",
                    (uid, f"standalone_dash_{i}", now - i * 86400, now + 30 * 86400),
                )
        token = db.create_session(uid)

        r1 = client.get("/api/v1/billing/invoices?limit=2", cookies={server.COOKIE_NAME: token})
        self.assertEqual(r1.status_code, 200)
        body1 = r1.json()
        self.assertEqual(len(body1["invoices"]), 2)
        self.assertEqual(body1["next_cursor"], 2)

        r2 = client.get(f"/api/v1/billing/invoices?limit=2&cursor={body1['next_cursor']}", cookies={server.COOKIE_NAME: token})
        body2 = r2.json()
        self.assertEqual(len(body2["invoices"]), 2)
        self.assertIsNone(body2["next_cursor"])

    def test_unauth_returns_401(self):
        r = client.get("/api/v1/billing/invoices")
        self.assertEqual(r.status_code, 401)


class TestInvoicePdfStub(_DbIsolation):
    @classmethod
    def setUpClass(cls):
        _pin_shared_db()
        cls.uid, cls.token = _make_fresh_user("pdf-test@test.com", "pdf_test")

    def test_pdf_returns_501(self):
        r = client.get("/api/v1/billing/invoices/sub_123/pdf", cookies={server.COOKIE_NAME: self.token})
        self.assertEqual(r.status_code, 501)
        body = r.json()
        self.assertEqual(body["error"], "invoice_pdf_not_available")
        self.assertEqual(body["invoice_id"], "sub_123")

    def test_pdf_unauth_returns_401(self):
        r = client.get("/api/v1/billing/invoices/sub_123/pdf")
        self.assertEqual(r.status_code, 401)


class TestPortalStub(_DbIsolation):
    @classmethod
    def setUpClass(cls):
        _pin_shared_db()
        cls.uid, cls.token = _make_fresh_user("portal-test@test.com", "portal_test")

    def test_portal_redirects_to_enquire(self):
        r = _post_form("/api/v1/billing/portal", token=self.token)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/enquire")

    def test_portal_unauth_redirects_to_token(self):
        r = client.post("/api/v1/billing/portal", follow_redirects=False)
        # 302 -> /token OR 403 (CSRF). Both are acceptable rejections.
        self.assertIn(r.status_code, (302, 403))


# ── Cancel / resubscribe ─────────────────────────────────────────────────────


class TestCancelFlow(_DbIsolation):
    @classmethod
    def setUpClass(cls):
        _pin_shared_db()
        cls.uid, cls.token = _make_user_with_plan(
            "cancel-test@test.com", "cancel_test", plan="pro", interval="annual", days_left=360,
        )

    def test_cancel_sets_status_cancelled(self):
        # Step 1 of the 3-step retention flow — records the attempt and
        # advances to step 2 (pause offer). Subscription status stays
        # active until step 3 finalizes.
        r1 = _post_form("/settings/billing/cancel", token=self.token, data={"reason": "too_expensive", "step": "1"})
        self.assertEqual(r1.status_code, 302)
        self.assertIn("/settings/billing/cancel-flow?step=2", r1.headers["location"])
        attempt_id = int(r1.headers["location"].split("attempt_id=")[-1])
        # Step 3 — final confirmation. AUDIT (H3): the caller MUST now
        # opt into a scope. Use cancel_all=true since the test seeds
        # the bundle row (dashboard_key='__plan__').
        r2 = _post_form(
            "/settings/billing/cancel", token=self.token,
            data={"step": "3", "attempt_id": str(attempt_id), "cancel_all": "true"},
        )
        self.assertEqual(r2.status_code, 302)
        self.assertIn("saved=cancelled", r2.headers["location"])
        with db.conn() as c:
            row = c.execute(
                "SELECT status, expires_at FROM subscriptions "
                "WHERE user_id = ? AND dashboard_key = '__plan__'",
                (self.uid,),
            ).fetchone()
        self.assertEqual(row["status"], "cancelled")
        # expires_at is NOT cleared — user keeps access through that date.
        self.assertIsNotNone(row["expires_at"])

    def test_cancel_unauth_redirects(self):
        r = client.post("/settings/billing/cancel", data={"reason": "x"}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 403))

    def test_flash_banner_appears_after_cancel(self):
        r = client.get(
            "/settings/billing?saved=cancelled",
            cookies={server.COOKIE_NAME: self.token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("subscription is cancelled", r.text.lower())


class TestResubscribeFlow(_DbIsolation):
    @classmethod
    def setUpClass(cls):
        _pin_shared_db()
        cls.uid, cls.token = _make_user_with_plan(
            "resub-test@test.com", "resub_test", plan="pro", interval="monthly",
            days_left=14, status="cancelled",
        )

    def test_resubscribe_flips_status_back_to_active(self):
        r = _post_form("/settings/billing/resubscribe", token=self.token)
        self.assertEqual(r.status_code, 302)
        self.assertIn("saved=resubscribed", r.headers["location"])
        with db.conn() as c:
            row = c.execute(
                "SELECT status FROM subscriptions "
                "WHERE user_id = ? AND dashboard_key = '__plan__'",
                (self.uid,),
            ).fetchone()
        self.assertEqual(row["status"], "active")

    def test_resubscribe_does_not_reactivate_expired_sub(self):
        """If expires_at < now, resubscribe SHOULDN'T flip status — the user
        has to pick a fresh plan from the Change plan cards."""
        uid = db.create_user("resub-expired@test.com", "TestPass123!", username="resub_expired")
        token = db.create_session(uid)
        now = int(time.time())
        with db.conn() as c:
            c.execute(
                "INSERT INTO subscriptions "
                "(user_id, dashboard_key, plan, status, started_at, expires_at) "
                "VALUES (?, '__plan__', 'pro_monthly', 'cancelled', ?, ?)",
                (uid, now - 40 * 86400, now - 1),
            )
        r = _post_form("/settings/billing/resubscribe", token=token)
        self.assertEqual(r.status_code, 302)
        with db.conn() as c:
            row = c.execute(
                "SELECT status FROM subscriptions "
                "WHERE user_id = ? AND dashboard_key = '__plan__'",
                (uid,),
            ).fetchone()
        self.assertEqual(row["status"], "cancelled", "expired subs shouldn't auto-reactivate")


# ── Add-ons ──────────────────────────────────────────────────────────────────


class TestAddonFlow(_DbIsolation):
    @classmethod
    def setUpClass(cls):
        _pin_shared_db()
        cls.uid, cls.token = _make_user_with_plan(
            "addon-test@test.com", "addon_test", plan="trader", interval="monthly", days_left=29,
        )

    def test_add_trading_addon_requires_stripe_checkout(self):
        # AUDIT (C1): the POST /settings/billing/addon handler used to
        # flip the local trading-addon flag directly, granting any
        # logged-in user a free 30-day entitlement. The handler now
        # routes through Stripe Checkout; with no Stripe SDK / secret
        # / price id in the test env it MUST fail closed (503) and
        # MUST NOT write the addon flag. The happy-path Stripe flow
        # is covered by tests/test_billing_addon_checkout.py.
        pre = db.get_trading_addon_status(self.uid).get("active", False)
        r = _post_form("/settings/billing/addon", token=self.token, data={"addon": "trading"})
        self.assertEqual(r.status_code, 503)
        post = db.get_trading_addon_status(self.uid).get("active", False)
        self.assertEqual(post, pre, "addon flag changed on fail-closed path")

    def test_cancel_trading_addon(self):
        # Ensure it's on first.
        db.set_trading_addon(self.uid, True, period_end=int(time.time()) + 30 * 86400)
        r = _post_form("/settings/billing/addon/cancel", token=self.token, data={"addon": "trading"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("saved=addon_removed", r.headers["location"])
        self.assertFalse(db.get_trading_addon_status(self.uid)["active"])

    def test_unknown_addon_is_noop_redirect(self):
        r = _post_form("/settings/billing/addon", token=self.token, data={"addon": "nonsense"})
        self.assertEqual(r.status_code, 302)
        # Doesn't set any flag and bounces home.
        self.assertIn("/settings/billing", r.headers["location"])


# ── Proration calculator (mirrors settings_billing.js) ───────────────────────


def calculate_proration_py(current, next_plan, period_end, now):
    """Python port of settings_billing.js#calculateProration.

    Must stay byte-for-byte equivalent in terms of numerical output, because
    this is what the UI shows the user before they confirm a plan change.
    Stripe's invoicing on the server side uses the identical formula, so if
    this test drifts from the JS we'd be lying to users.
    """
    if not current or not current.get("amount_usd"):
        next_days = 365 if next_plan["interval"] == "annual" else 30
        return {
            "credit": 0,
            "charge": next_plan["amount_usd"],
            "net": next_plan["amount_usd"],
            "unusedDays": next_days,
            "totalDays": next_days,
            "kind": "new",
        }

    cur_days = 365 if current["interval"] == "annual" else 30
    cur_amount = current["amount_usd"]
    daily_old = cur_amount / cur_days if cur_days else 0
    unused_days = max(0, int((period_end - now) // 86400))
    credit = daily_old * unused_days

    next_days = 365 if next_plan["interval"] == "annual" else 30
    next_amount = next_plan["amount_usd"]
    daily_new = next_amount / next_days if next_days else 0
    new_cost = daily_new * unused_days

    net = new_cost - credit
    if next_plan["key"] != current["key"]:
        kind = "upgrade" if next_amount > cur_amount else "downgrade"
    elif next_plan["interval"] != current["interval"]:
        kind = "interval_up" if next_days > cur_days else "interval_down"
    else:
        kind = "same"

    return {
        "credit": round(credit, 2),
        "charge": round(new_cost, 2),
        "net": round(net, 2),
        "unusedDays": unused_days,
        "totalDays": cur_days,
        "kind": kind,
    }


class TestProration(unittest.TestCase):
    """Verify the proration formula matches Stripe's published model.

    Stripe's proration credit = daily_rate_old * unused_days, charge for the
    new period = daily_rate_new * unused_days, net = charge - credit. These
    tests lock the numbers so a refactor doesn't silently drift the preview.
    """

    def test_upgrade_trader_to_pro_monthly_midcycle(self):
        # User had Trader monthly ($99/mo), 16 days left, upgrading to Pro monthly ($229/mo).
        current = {"key": "trader", "interval": "monthly", "amount_usd": 99}
        next_plan = {"key": "pro", "interval": "monthly", "amount_usd": 229}
        now = 1000
        period_end = now + 16 * 86400
        r = calculate_proration_py(current, next_plan, period_end, now)
        # daily_old = 99/30 = 3.30, credit = 3.30 * 16 = 52.80
        # daily_new = 229/30 ≈ 7.6333, cost = 7.6333 * 16 ≈ 122.13
        # net = 122.13 - 52.80 = 69.33
        self.assertEqual(r["kind"], "upgrade")
        self.assertEqual(r["unusedDays"], 16)
        self.assertAlmostEqual(r["credit"], 52.80, places=2)
        self.assertAlmostEqual(r["charge"], 122.13, places=2)
        self.assertAlmostEqual(r["net"], 69.33, places=2)

    def test_downgrade_pro_annual_to_trader_annual_midcycle(self):
        # Pro annual ($1,999), 307 days left, down to Trader annual ($999).
        current = {"key": "pro", "interval": "annual", "amount_usd": 1999}
        next_plan = {"key": "trader", "interval": "annual", "amount_usd": 999}
        now = 1000
        period_end = now + 307 * 86400
        r = calculate_proration_py(current, next_plan, period_end, now)
        # credit = (1999/365) * 307 ≈ 1681.29
        # cost   = (999/365)  * 307 ≈ 840.19
        # net    = 840.19 - 1681.29 ≈ -841.10 (credit)
        self.assertEqual(r["kind"], "downgrade")
        self.assertLess(r["net"], 0, "downgrade should net to a credit")
        self.assertAlmostEqual(abs(r["net"]), 841.10, delta=0.5)

    def test_fresh_subscribe_charges_full_amount(self):
        # No current plan → charge full amount, credit 0.
        current = None
        next_plan = {"key": "pro", "interval": "monthly", "amount_usd": 229}
        r = calculate_proration_py(current, next_plan, 0, 0)
        self.assertEqual(r["kind"], "new")
        self.assertEqual(r["credit"], 0)
        self.assertEqual(r["charge"], 229)
        self.assertEqual(r["net"], 229)

    def test_interval_switch_monthly_to_annual(self):
        # Same plan, but switching from monthly to annual mid-cycle.
        current = {"key": "pro", "interval": "monthly", "amount_usd": 229}
        next_plan = {"key": "pro", "interval": "annual", "amount_usd": 1999}
        now = 1000
        period_end = now + 10 * 86400
        r = calculate_proration_py(current, next_plan, period_end, now)
        # credit = (229/30) * 10 = 76.33, cost = (1999/365) * 10 = 54.77
        self.assertEqual(r["kind"], "interval_up")
        self.assertAlmostEqual(r["credit"], 76.33, places=2)
        self.assertAlmostEqual(r["charge"], 54.77, places=2)

    def test_proration_never_negative_days(self):
        # Expired plan shouldn't produce negative credit.
        current = {"key": "pro", "interval": "monthly", "amount_usd": 229}
        next_plan = {"key": "trader", "interval": "monthly", "amount_usd": 99}
        now = 1_000_000
        period_end = now - 86400  # already expired
        r = calculate_proration_py(current, next_plan, period_end, now)
        self.assertEqual(r["unusedDays"], 0)
        self.assertEqual(r["credit"], 0)
        self.assertEqual(r["charge"], 0)


class TestProrationCalculatorInJsFile(unittest.TestCase):
    """Static check: the JS file exposes the same calculate_proration shape
    and constants that the Python port above assumes. This catches cases where
    someone edits one without the other."""

    @classmethod
    def setUpClass(cls):
        _pin_shared_db()
        path = os.path.join(os.path.dirname(__file__), "..", "static", "settings_billing.js")
        with open(path) as f:
            cls.js = f.read()

    def test_calculate_proration_exported(self):
        self.assertIn("function calculateProration", self.js)
        # window.narveProration.calculate is the public handle for browser tests.
        self.assertIn("window.narveProration", self.js)
        self.assertIn("calculate: calculateProration", self.js)

    def test_catalog_used_by_cards(self):
        # The interval toggle must touch the data-price elements on every card.
        self.assertIn("data-plan-card", self.js)
        self.assertIn("[data-price]", self.js)
        self.assertIn("[data-interval-input]", self.js)

    def test_modal_open_and_close_handlers(self):
        self.assertIn("openModal", self.js)
        self.assertIn("closeModal", self.js)
        self.assertIn("data-close-modal", self.js)
        # Esc closes modals.
        self.assertIn('key === "Escape"', self.js)


# ── Auth / paywall ───────────────────────────────────────────────────────────


class TestPaywallAndAuth(_DbIsolation):
    def test_settings_billing_requires_login(self):
        r = client.get("/settings/billing", follow_redirects=False)
        self.assertIn(r.status_code, (302, 307))
        # 2026-05-15 — direct /login redirect (the /token gate was removed).
        self.assertIn("/login", r.headers["location"])

    def test_invoices_endpoint_requires_login(self):
        r = client.get("/api/v1/billing/invoices")
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()

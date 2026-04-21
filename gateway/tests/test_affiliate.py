"""Tests for the private affiliate program.

Covers every checklist item in the spec plus a handful of paranoia
tests (re-attribution guard, inactive affiliates, admin gate) that
aren't strictly required but would bite in production if regressed.

Uses the shared in-memory DB from ``tests._testdb`` so migrations
(including 033_affiliate_program) run exactly once per pytest
collection phase.
"""

from __future__ import annotations

import os
import time
import unittest

# Must come before `import server` so gate + dev bypass behave.
os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("EMAIL_DRY_RUN", "true")

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations
import db  # noqa: E402
import db_affiliate as da  # noqa: E402
import server  # noqa: E402
import server_features  # noqa: F401,E402 — loads affiliate_routes transitively
from fastapi.testclient import TestClient  # noqa: E402


client = TestClient(server.app, follow_redirects=False)


_seq = 0


def _uniq(prefix: str) -> str:
    """Unique-per-invocation identifier. Tests share the in-memory DB so
    username/email collisions would false-fail otherwise.
    """
    global _seq
    _seq += 1
    return f"{prefix}_{int(time.time() * 1000)}_{_seq}"


def _make_user(prefix: str = "u", is_admin: bool = False) -> int:
    uname = _uniq(prefix)
    uid = db.create_user(
        f"{uname}@test.local", "TestPw!!1234", username=uname, is_admin=is_admin,
    )
    # Admin routes may enforce 2FA. The 2FA columns (``two_fa_method``,
    # ``totp_enabled``, ``totp_secret``) exist in some branches and not
    # others — migration 019 removed them from `users`, but server.py's
    # _two_fa_redirect still checks. We try to set them; on schema
    # mismatch we swallow the error and rely on the ``_dev_bypass`` flag
    # that current_user() sets from localhost request context.
    if is_admin:
        try:
            with db.conn() as c:
                c.execute(
                    "UPDATE users SET two_fa_method = 'totp', totp_enabled = 1, "
                    "totp_secret = ? WHERE id = ?",
                    ("JBSWY3DPEHPK3PXP", uid),
                )
        except Exception:
            pass
    return uid


_CSRF_TOKEN = "test-csrf-token-affiliate-suite"


def _session_cookies(uid: int) -> dict:
    """Session + CSRF cookie. The CSRF middleware expects the double-submit
    pattern (cookie + header); tests that POST/PATCH must also pass
    ``headers=_csrf_headers()`` to pair with this cookie.

    Admin routes may also enforce 2FA, so we try to flip the session's
    ``two_fa_verified`` flag. Schema drift across branches is swallowed.
    """
    token = db.create_session(uid)
    try:
        db.mark_session_two_fa_verified(token)
    except Exception:
        pass
    return {server.COOKIE_NAME: token, server.CSRF_COOKIE_NAME: _CSRF_TOKEN}


def _csrf_headers() -> dict:
    """Header to pair with the _csrf cookie set in ``_session_cookies``."""
    return {server.CSRF_HEADER_NAME: _CSRF_TOKEN}


def _make_affiliate(rate: float = 0.20, tier: str = "partner"):
    admin_id = _make_user("adm", is_admin=True)
    user_id = _make_user("partner")
    aff_id = da.create_affiliate_account(
        user_id,
        commission_rate=rate,
        tier=tier,
        approved_by_admin_id=admin_id,
        payout_method="paypal",
        payout_email=f"pay-{_uniq('p')}@test.local",
    )
    return aff_id, user_id, admin_id


# ── Code generation ────────────────────────────────────────────────────


class TestAffiliateCodeGeneration(unittest.TestCase):
    def test_codes_are_unique_across_many_accounts(self):
        """Generate 50 affiliates; no two get the same affiliate_code."""
        codes = set()
        for _ in range(50):
            aff_id, _, _ = _make_affiliate()
            aff = da.get_affiliate_by_id(aff_id)
            self.assertNotIn(aff["affiliate_code"], codes)
            codes.add(aff["affiliate_code"])
        self.assertEqual(len(codes), 50)

    def test_affiliate_code_distinct_from_referral_code(self):
        """Newsletter referral codes are 8 chars; affiliate codes 10 chars.
        Same namespace but different lengths so a code can't straddle."""
        aff_id, _, _ = _make_affiliate()
        code = da.get_affiliate_by_id(aff_id)["affiliate_code"]
        self.assertEqual(len(code), 10)

    def test_duplicate_account_per_user_blocked(self):
        aff_id, user_id, admin_id = _make_affiliate()
        with self.assertRaises(ValueError):
            da.create_affiliate_account(
                user_id, commission_rate=0.25, tier="partner",
                approved_by_admin_id=admin_id,
            )

    def test_commission_rate_validation(self):
        user_id = _make_user("bad_rate")
        with self.assertRaises(ValueError):
            da.create_affiliate_account(
                user_id, commission_rate=0.99, tier="partner",
                approved_by_admin_id=None,
            )
        with self.assertRaises(ValueError):
            da.create_affiliate_account(
                user_id, commission_rate=0.01, tier="partner",
                approved_by_admin_id=None,
            )

    def test_tier_validation(self):
        user_id = _make_user("bad_tier")
        with self.assertRaises(ValueError):
            da.create_affiliate_account(
                user_id, commission_rate=0.20, tier="gold_platinum",
                approved_by_admin_id=None,
            )


# ── Click tracking + cookie ────────────────────────────────────────────


class TestPartnerClickEndpoint(unittest.TestCase):
    def test_valid_code_sets_cookie_and_redirects(self):
        aff_id, _, _ = _make_affiliate()
        code = da.get_affiliate_by_id(aff_id)["affiliate_code"]
        r = client.get(f"/partner/{code}")
        self.assertEqual(r.status_code, 302)
        # Cookie set with 90-day max-age
        set_cookie = r.headers.get("set-cookie", "")
        self.assertIn("affiliate_code=", set_cookie)
        self.assertIn(str(da.AFFILIATE_COOKIE_MAX_AGE_SECONDS), set_cookie)

    def test_invalid_code_redirects_silently(self):
        """A tampered/unknown code shouldn't leak existence — same
        behavior as a valid code minus the cookie set."""
        r = client.get("/partner/THIS_CODE_DOES_NOT_EXIST")
        self.assertEqual(r.status_code, 302)
        # No affiliate_code cookie set on an invalid code.
        set_cookie = r.headers.get("set-cookie", "")
        self.assertNotIn("affiliate_code=", set_cookie)

    def test_inactive_affiliate_redirects_silently(self):
        aff_id, user_id, admin_id = _make_affiliate()
        code = da.get_affiliate_by_id(aff_id)["affiliate_code"]
        da.update_affiliate_account(aff_id, is_active=False)
        r = client.get(f"/partner/{code}")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("affiliate_code=", r.headers.get("set-cookie", ""))

    def test_click_recorded_in_db(self):
        aff_id, _, _ = _make_affiliate()
        code = da.get_affiliate_by_id(aff_id)["affiliate_code"]
        before = da.sum_affiliate_commissions(aff_id)["click_count"]
        client.get(f"/partner/{code}")
        after = da.sum_affiliate_commissions(aff_id)["click_count"]
        self.assertEqual(after, before + 1)

    def test_short_alias_works(self):
        aff_id, _, _ = _make_affiliate()
        code = da.get_affiliate_by_id(aff_id)["affiliate_code"]
        r = client.get(f"/p/{code}")
        self.assertEqual(r.status_code, 302)
        self.assertIn("affiliate_code=", r.headers.get("set-cookie", ""))

    def test_custom_campaign_link_increments_link_clicks(self):
        aff_id, _, _ = _make_affiliate()
        code = da.get_affiliate_by_id(aff_id)["affiliate_code"]
        link_id = da.create_affiliate_link(aff_id, "podcast_ep_47")

        r = client.get(f"/p/{code}", params={"c": "podcast_ep_47"})
        self.assertEqual(r.status_code, 302)
        link = da.get_affiliate_link_by_campaign(aff_id, "podcast_ep_47")
        self.assertEqual(link["clicks"], 1)
        self.assertEqual(link["id"], link_id)


# ── Signup attribution ────────────────────────────────────────────────


class TestSignupAttribution(unittest.TestCase):
    def test_attach_signup_claims_recent_click(self):
        aff_id, _, _ = _make_affiliate()
        da.record_affiliate_click(aff_id)
        new_user_id = _make_user("joined")

        conv_id = da.attach_signup_to_affiliate(aff_id, new_user_id)
        self.assertIsNotNone(conv_id)
        # Row now has the user attached
        conv = da.get_affiliate_conversion_for_user(new_user_id)
        self.assertIsNotNone(conv)
        self.assertEqual(conv["affiliate_account_id"], aff_id)
        self.assertIsNotNone(conv["signed_up_at"])

    def test_attach_signup_without_click_creates_fresh_row(self):
        """Cookie survives a DB wipe / click row missing — we still
        record the signup with source_note=cookie_without_click."""
        aff_id, _, _ = _make_affiliate()
        new_user_id = _make_user("nocklick")
        conv_id = da.attach_signup_to_affiliate(aff_id, new_user_id)
        self.assertIsNotNone(conv_id)
        conv = da.get_affiliate_conversion_for_user(new_user_id)
        self.assertEqual(conv["source_note"], "cookie_without_click")

    def test_re_attribution_guard(self):
        """A user already attached to affiliate A cannot be pulled over
        to affiliate B by a second click. attach_signup returns None."""
        aff_a_id, _, _ = _make_affiliate()
        aff_b_id, _, _ = _make_affiliate()
        new_user_id = _make_user("pulltest")

        first = da.attach_signup_to_affiliate(aff_a_id, new_user_id)
        second = da.attach_signup_to_affiliate(aff_b_id, new_user_id)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

        conv = da.get_affiliate_conversion_for_user(new_user_id)
        self.assertEqual(conv["affiliate_account_id"], aff_a_id)

    def test_conversion_counts_bump_on_signup(self):
        aff_id, _, _ = _make_affiliate()
        before = da.get_affiliate_by_id(aff_id)["total_conversions"]
        new_user_id = _make_user("bumpme")
        da.attach_signup_to_affiliate(aff_id, new_user_id)
        after = da.get_affiliate_by_id(aff_id)["total_conversions"]
        self.assertEqual(after, before + 1)


# ── Commission calculation ────────────────────────────────────────────


class TestCommissionCalculation(unittest.TestCase):
    def test_commission_correct_for_rate(self):
        aff_id, _, _ = _make_affiliate(rate=0.20)
        ref_id = _make_user("paid1")
        da.record_affiliate_click(aff_id)
        conv_id = da.attach_signup_to_affiliate(aff_id, ref_id)
        da.mark_affiliate_conversion_paid(conv_id, first_payment_amount_pence=10000)

        # Run the job directly
        import asyncio
        from jobs.affiliate_jobs import calculate_affiliate_commissions
        res = asyncio.run(calculate_affiliate_commissions())
        self.assertGreaterEqual(res["processed"], 1)

        summary = da.sum_affiliate_commissions(aff_id)
        # £100 × 20% = £20 = 2000p
        self.assertEqual(summary["earned_pence"], 2000)
        self.assertEqual(summary["pending_pence"], 2000)

    def test_different_rate_produces_different_commission(self):
        aff_id, _, _ = _make_affiliate(rate=0.30)
        ref_id = _make_user("paid2")
        da.record_affiliate_click(aff_id)
        conv_id = da.attach_signup_to_affiliate(aff_id, ref_id)
        da.mark_affiliate_conversion_paid(conv_id, first_payment_amount_pence=10000)

        import asyncio
        from jobs.affiliate_jobs import calculate_affiliate_commissions
        asyncio.run(calculate_affiliate_commissions())
        summary = da.sum_affiliate_commissions(aff_id)
        # £100 × 30% = £30 = 3000p
        self.assertEqual(summary["earned_pence"], 3000)

    def test_commission_job_idempotent(self):
        """Running the job twice for the same paid conversion doesn't
        double-count earnings."""
        aff_id, _, _ = _make_affiliate(rate=0.25)
        ref_id = _make_user("idem")
        da.record_affiliate_click(aff_id)
        conv_id = da.attach_signup_to_affiliate(aff_id, ref_id)
        da.mark_affiliate_conversion_paid(conv_id, first_payment_amount_pence=8000)

        import asyncio
        from jobs.affiliate_jobs import calculate_affiliate_commissions
        asyncio.run(calculate_affiliate_commissions())
        first = da.sum_affiliate_commissions(aff_id)["earned_pence"]
        asyncio.run(calculate_affiliate_commissions())
        second = da.sum_affiliate_commissions(aff_id)["earned_pence"]
        self.assertEqual(first, second)

    def test_mark_paid_is_idempotent_on_stripe_webhook(self):
        aff_id, _, _ = _make_affiliate()
        ref_id = _make_user("stripe_dup")
        da.record_affiliate_click(aff_id)
        conv_id = da.attach_signup_to_affiliate(aff_id, ref_id)
        first = da.mark_affiliate_conversion_paid(conv_id, 5000)
        second = da.mark_affiliate_conversion_paid(conv_id, 5000)
        self.assertTrue(first)
        self.assertFalse(second)  # second firing is a no-op


# ── Payout request ────────────────────────────────────────────────────


class TestPayoutRequest(unittest.TestCase):
    def test_below_threshold_rejected(self):
        """£50 minimum. Anything less returns 400."""
        aff_id, user_id, _ = _make_affiliate()
        ref_id = _make_user("cheap")
        da.record_affiliate_click(aff_id)
        conv_id = da.attach_signup_to_affiliate(aff_id, ref_id)
        da.mark_affiliate_conversion_paid(conv_id, first_payment_amount_pence=1000)
        import asyncio
        from jobs.affiliate_jobs import calculate_affiliate_commissions
        asyncio.run(calculate_affiliate_commissions())

        r = client.post(
            "/api/v1/affiliate/payout-request",
            cookies=_session_cookies(user_id),
            headers=_csrf_headers(),
            json={},
        )
        self.assertEqual(r.status_code, 400)

    def test_over_threshold_accepted(self):
        aff_id, user_id, _ = _make_affiliate(rate=0.25)
        ref_id = _make_user("bigpay")
        da.record_affiliate_click(aff_id)
        conv_id = da.attach_signup_to_affiliate(aff_id, ref_id)
        # £500 × 25% = £125 (well above £50)
        da.mark_affiliate_conversion_paid(conv_id, first_payment_amount_pence=50000)
        import asyncio
        from jobs.affiliate_jobs import calculate_affiliate_commissions
        asyncio.run(calculate_affiliate_commissions())

        r = client.post(
            "/api/v1/affiliate/payout-request",
            cookies=_session_cookies(user_id),
            headers=_csrf_headers(),
            json={},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])


# ── Admin gates ───────────────────────────────────────────────────────


class TestAdminGate(unittest.TestCase):
    def test_no_public_affiliate_application_endpoint(self):
        """Spec: 'no public affiliate application form'. Confirm the
        admin-create endpoint rejects non-admin callers with 403."""
        plain_user_id = _make_user("plain")
        r = client.post(
            "/admin/affiliates",
            cookies=_session_cookies(plain_user_id),
            headers=_csrf_headers(),
            json={"user_email": "x@y.com", "commission_rate": 0.20, "tier": "partner"},
        )
        self.assertEqual(r.status_code, 403)

    def test_admin_can_create_affiliate(self):
        admin_id = _make_user("fresh_admin", is_admin=True)
        new_user_id = _make_user("new_partner")
        new_email = db.get_user_by_id(new_user_id)["email"]
        r = client.post(
            "/admin/affiliates",
            cookies=_session_cookies(admin_id),
            headers=_csrf_headers(),
            json={
                "user_email": new_email,
                "commission_rate": 0.20,
                "tier": "partner",
                "payout_method": "paypal",
                "payout_email": "pay@test.local",
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["tier"], "partner")
        self.assertAlmostEqual(body["commission_rate"], 0.20)
        self.assertEqual(len(body["affiliate_code"]), 10)

    def test_admin_can_update_commission_rate(self):
        admin_id = _make_user("upd_admin", is_admin=True)
        aff_id, _, _ = _make_affiliate(rate=0.20)
        r = client.patch(
            f"/admin/affiliates/{aff_id}",
            cookies=_session_cookies(admin_id),
            headers=_csrf_headers(),
            json={"commission_rate": 0.35},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertAlmostEqual(r.json()["commission_rate"], 0.35)

    def test_admin_mark_paid_flips_conversions(self):
        admin_id = _make_user("pay_admin", is_admin=True)
        aff_id, _, _ = _make_affiliate(rate=0.25)
        ref_id = _make_user("ready_to_pay")
        da.record_affiliate_click(aff_id)
        conv_id = da.attach_signup_to_affiliate(aff_id, ref_id)
        da.mark_affiliate_conversion_paid(conv_id, first_payment_amount_pence=50000)
        import asyncio
        from jobs.affiliate_jobs import calculate_affiliate_commissions
        asyncio.run(calculate_affiliate_commissions())

        before = da.sum_affiliate_commissions(aff_id)
        self.assertGreater(before["pending_pence"], 0)

        r = client.post(
            f"/admin/affiliates/{aff_id}/payout",
            cookies=_session_cookies(admin_id),
            headers=_csrf_headers(),
            json={},
        )
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(r.json()["rows"], 1)

        after = da.sum_affiliate_commissions(aff_id)
        self.assertEqual(after["pending_pence"], 0)
        self.assertEqual(after["paid_pence"], before["earned_pence"])


# ── Dashboard ─────────────────────────────────────────────────────────


class TestAffiliateDashboard(unittest.TestCase):
    def test_non_affiliate_sees_info_page(self):
        user_id = _make_user("nonaff")
        r = client.get(
            "/settings/affiliate",
            cookies=_session_cookies(user_id),
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("invite-only", r.text.lower())

    def test_affiliate_sees_dashboard(self):
        aff_id, user_id, _ = _make_affiliate()
        r = client.get(
            "/settings/affiliate",
            cookies=_session_cookies(user_id),
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("Affiliate dashboard", r.text)
        # Shows the default link
        self.assertIn(da.get_affiliate_by_id(aff_id)["affiliate_code"], r.text)

    def test_api_affiliate_returns_summary(self):
        aff_id, user_id, _ = _make_affiliate(rate=0.30)
        r = client.get(
            "/api/v1/affiliate",
            cookies=_session_cookies(user_id),
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["commission_rate"], 0.30)
        self.assertEqual(body["tier"], "partner")
        self.assertIn("summary_pence", body)
        self.assertIn("summary_gbp", body)

    def test_create_custom_tracking_link(self):
        aff_id, user_id, _ = _make_affiliate()
        r = client.post(
            "/api/v1/affiliate/links",
            cookies=_session_cookies(user_id),
            headers=_csrf_headers(),
            json={"utm_campaign": "Podcast Ep 47", "utm_content": "Main episode mention"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # utm_campaign normalized to lowercase alphanumeric
        self.assertEqual(body["utm_campaign"], "podcastep47")

    def test_api_affiliate_denies_non_affiliate(self):
        user_id = _make_user("rando")
        r = client.get(
            "/api/v1/affiliate",
            cookies=_session_cookies(user_id),
        )
        self.assertEqual(r.status_code, 403)


# ── Email anonymisation ──────────────────────────────────────────────


class TestEmailAnonymisation(unittest.TestCase):
    def test_anonymise_strips_domain(self):
        self.assertEqual(da.anonymise_email("jake@example.com"), "jake@.com")
        self.assertEqual(da.anonymise_email("foo.bar@weird.co.uk"), "foo.bar@.com")

    def test_anonymise_handles_edge_cases(self):
        self.assertEqual(da.anonymise_email(None), "—")
        self.assertEqual(da.anonymise_email(""), "—")
        self.assertEqual(da.anonymise_email("noatsymbol"), "—")
        self.assertEqual(da.anonymise_email("@nolocal.com"), "—")


if __name__ == "__main__":
    unittest.main()

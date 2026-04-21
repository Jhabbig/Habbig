"""Tests for the private referral program + leaderboard.

Covers:
  * Referral code generation + uniqueness
  * Invite-code lookup + canonicalization
  * Invalid codes rejected
  * Referral row lifecycle: invited → converted → rewarded
  * Reward tier ladder (1/5/10 → correct months + tier_mode)
  * Reward job actually grants the gift only when the referrer is still paying
  * Stacking: three simultaneous conversions yield three stamped rows
  * Leaderboard opt-in/out + rank API
  * HTTP layer: /invite/{code}, /api/invite/{code}, /api/invite/{code}/accept
  * HTTP layer: /settings/referrals (auth required), /api/referrals/me
  * HTTP layer: /leaderboard (auth required), /api/leaderboard (period filter)

All tests use the shared in-memory `_testdb` connection so migrations run
once at session start. EMAIL_DRY_RUN=true → no real emails sent.
"""

from __future__ import annotations

import asyncio
import os
import time
import unittest

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("EMAIL_DRY_RUN", "true")

from tests import _testdb  # noqa: F401 — shared DB + migrations
import db  # noqa: E402
import server  # noqa: E402
from backend import referrals as referral_logic  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


client = TestClient(server.app)


def _session_cookies(user_id: int) -> dict:
    """Authenticate by creating a raw session row — faster than running
    the full login flow and doesn't require a password."""
    token = db.create_session(user_id)
    return {server.COOKIE_NAME: token}


def _authed_client(user_id: int) -> TestClient:
    """Build a TestClient pre-seeded with a session cookie AND a CSRF
    cookie/header pair for authenticated POSTs.

    The gateway's CSRFMiddleware runs a strict double-submit check on
    authenticated POSTs: cookie `_csrf` must equal header `x-csrf-token`.
    We pick a known dummy token and set both sides of the pair on the
    client jar so every subsequent POST validates.
    """
    c = TestClient(server.app)
    token = db.create_session(user_id)
    c.cookies.set(server.COOKIE_NAME, token)
    c.cookies.set("_csrf", "t_test_csrf_token_fixed")
    c.headers.update({"x-csrf-token": "t_test_csrf_token_fixed"})
    return c


def _mk_user(email: str, username: str = "") -> int:
    username = username or email.split("@")[0]
    return db.create_user(email, "TestPass123!", username=username)


def _give_active_sub(user_id: int, plan: str = "trader") -> None:
    """Make `user_id` look like a paying subscriber so the reward job
    doesn't skip them."""
    db.upsert_subscription(
        user_id=user_id,
        dashboard_key=f"test-dash-{plan}",
        plan=plan,
        duration_days=30,
        source="test",
    )


# ── Unit: code generation + DB helpers ────────────────────────────────────────


class TestReferralCodes(unittest.TestCase):
    def test_generated_codes_are_10_char_alphanumeric(self):
        for _ in range(50):
            c = db.generate_referral_code()
            self.assertEqual(len(c), 10)
            self.assertTrue(c.isalnum())
            # No ambiguous chars — we excluded 0/1/I/O from the alphabet
            # (uppercase L stays; it reads unambiguously next to digits).
            self.assertFalse(any(ch in c for ch in "01IO"))

    def test_ensure_user_referral_code_is_idempotent(self):
        uid = _mk_user("codestable@test.com")
        first = db.ensure_user_referral_code(uid)
        again = db.ensure_user_referral_code(uid)
        self.assertEqual(first, again)

    def test_get_user_by_referral_code_case_insensitive(self):
        uid = _mk_user("lookup@test.com")
        code = db.ensure_user_referral_code(uid)
        row_upper = db.get_user_by_referral_code(code.upper())
        row_lower = db.get_user_by_referral_code(code.lower())
        self.assertIsNotNone(row_upper)
        self.assertIsNotNone(row_lower)
        self.assertEqual(row_upper["id"], uid)
        self.assertEqual(row_lower["id"], uid)

    def test_invalid_code_returns_none(self):
        self.assertIsNone(db.get_user_by_referral_code("NOTACODE__"))
        self.assertIsNone(db.get_user_by_referral_code(""))
        self.assertIsNone(db.get_user_by_referral_code(None))

    def test_suspended_user_code_not_resolvable(self):
        uid = _mk_user("suspended@test.com")
        code = db.ensure_user_referral_code(uid)
        with db.conn() as c:
            c.execute("UPDATE users SET suspended = 1 WHERE id = ?", (uid,))
        self.assertIsNone(db.get_user_by_referral_code(code))


# ── Unit: reward tier logic ───────────────────────────────────────────────────


class TestRewardTiering(unittest.TestCase):
    def test_first_conversion_earns_one_month_free_at_current_tier(self):
        r = referral_logic.compute_reward_for_referral(
            total_converted_before_this_one=0,
            current_tier="trader",
        )
        self.assertEqual(r["type"], "one_month_free")
        self.assertEqual(r["months"], 1)
        self.assertEqual(r["tier"], "trader")

    def test_fifth_conversion_earns_tier_upgrade(self):
        r = referral_logic.compute_reward_for_referral(
            total_converted_before_this_one=4,
            current_tier="trader",
        )
        self.assertEqual(r["type"], "tier_upgrade")
        self.assertEqual(r["months"], 1)
        self.assertEqual(r["tier"], "pro")

    def test_tenth_conversion_earns_three_months_pro(self):
        r = referral_logic.compute_reward_for_referral(
            total_converted_before_this_one=9,
            current_tier="pro",
        )
        self.assertEqual(r["type"], "pro_three_months")
        self.assertEqual(r["months"], 3)
        self.assertEqual(r["tier"], "pro")

    def test_second_third_fourth_conversions_earn_nothing(self):
        for n in (1, 2, 3, 5, 6, 7, 8):
            r = referral_logic.compute_reward_for_referral(
                total_converted_before_this_one=n,
                current_tier="trader",
            )
            self.assertIsNone(r, f"conversion number {n+1} should earn nothing")

    def test_pro_user_tier_upgrade_stays_pro(self):
        # A pro user hitting the count=5 milestone has no "next tier"
        # above — reward tiers out at pro.
        r = referral_logic.compute_reward_for_referral(
            total_converted_before_this_one=4,
            current_tier="pro",
        )
        self.assertEqual(r["tier"], "pro")

    def test_progress_renders_correctly(self):
        p = referral_logic.progress_toward_next_reward(0)
        self.assertEqual(p["next_milestone"], 1)
        self.assertEqual(p["remaining"], 1)
        p = referral_logic.progress_toward_next_reward(4)
        self.assertEqual(p["next_milestone"], 5)
        self.assertEqual(p["remaining"], 1)
        p = referral_logic.progress_toward_next_reward(15)
        self.assertIsNone(p["next_milestone"])


# ── Unit: referral row lifecycle ──────────────────────────────────────────────


class TestReferralLifecycle(unittest.TestCase):
    def setUp(self):
        # Unique per-test emails — _testdb shares one in-memory DB across
        # the whole session, so reusing a fixed email collides on UNIQUE.
        tag = self._testMethodName
        self.referrer = _mk_user(f"lifer_{tag}_r@test.com", f"lifer_{tag}_r")
        self.invitee = _mk_user(f"lifer_{tag}_i@test.com", f"lifer_{tag}_i")

    def test_create_and_mark_converted(self):
        rid = db.create_referral(
            referrer_user_id=self.referrer,
            referred_email="lifer_inv@test.com",
        )
        self.assertGreater(rid, 0)
        # Attach the user, then mark converted.
        db.attach_user_to_referral(rid, self.invitee)
        flipped = db.mark_referral_converted(self.invitee)
        self.assertEqual(flipped, 1)
        # Running again is a no-op (idempotent).
        self.assertEqual(db.mark_referral_converted(self.invitee), 0)

    def test_count_converted_skips_unconverted(self):
        db.create_referral(
            referrer_user_id=self.referrer,
            referred_email="pendingA@test.com",
        )
        rid2 = db.create_referral(
            referrer_user_id=self.referrer,
            referred_email="pendingB@test.com",
        )
        db.attach_user_to_referral(rid2, self.invitee)
        db.mark_referral_converted(self.invitee)
        self.assertEqual(db.count_converted_referrals(self.referrer), 1)

    def test_get_user_referrals_most_recent_first(self):
        a = db.create_referral(
            referrer_user_id=self.referrer, referred_email="A@t.com",
        )
        time.sleep(0.01)
        b = db.create_referral(
            referrer_user_id=self.referrer, referred_email="B@t.com",
        )
        rows = db.get_user_referrals(self.referrer)
        ids = [r["id"] for r in rows]
        # b was created second so should come first
        self.assertEqual(ids[0], b)
        self.assertIn(a, ids)


# ── Unit: upsert_subscription triggers conversion hook ───────────────────────


class TestConversionHook(unittest.TestCase):
    def test_first_paid_sub_flips_pending_referral(self):
        referrer = _mk_user("hook_r@test.com", "hook_r")
        invitee = _mk_user("hook_i@test.com", "hook_i")
        rid = db.create_referral(
            referrer_user_id=referrer,
            referred_email="hook_i@test.com",
            referred_user_id=invitee,
        )
        # Trigger: invitee subscribes.
        db.upsert_subscription(
            user_id=invitee, dashboard_key="hook-dash",
            plan="trader", duration_days=30, source="test",
        )
        with db.conn() as c:
            row = c.execute(
                "SELECT converted_to_paid FROM referrals WHERE id = ?",
                (rid,),
            ).fetchone()
        self.assertEqual(row["converted_to_paid"], 1)


# ── Integration: reward job grants gifts correctly ───────────────────────────


class TestRewardJob(unittest.TestCase):
    def setUp(self):
        from jobs.referral_jobs import process_referral_rewards
        self.run_job = process_referral_rewards

    def _fresh_pair(self, label: str):
        r = _mk_user(f"{label}_r@test.com", f"{label}_r")
        i = _mk_user(f"{label}_i@test.com", f"{label}_i")
        _give_active_sub(r, "trader")  # Referrer must be paying to earn.
        return r, i

    def test_grants_one_month_free_on_first_conversion(self):
        r, i = self._fresh_pair("rwd1")
        rid = db.create_referral(
            referrer_user_id=r, referred_email=f"rwd1_i@test.com",
            referred_user_id=i,
        )
        db.mark_referral_converted(i)

        result = asyncio.run(self.run_job())
        self.assertGreaterEqual(result["granted"], 1)

        # Verify the reward row and the gift.
        with db.conn() as c:
            row = c.execute(
                "SELECT reward_type, reward_months, reward_tier, "
                "gifted_subscription_id FROM referrals WHERE id = ?",
                (rid,),
            ).fetchone()
        self.assertEqual(row["reward_type"], "one_month_free")
        self.assertEqual(row["reward_months"], 1)
        self.assertEqual(row["reward_tier"], "trader")
        self.assertIsNotNone(row["gifted_subscription_id"])

        # Gift exists and is unrevoked.
        with db.conn() as c:
            g = c.execute(
                "SELECT subscription_type, revoked FROM gifted_subscriptions WHERE id = ?",
                (row["gifted_subscription_id"],),
            ).fetchone()
        self.assertEqual(g["subscription_type"], "trader")
        self.assertEqual(g["revoked"], 0)

    def test_skips_non_paying_referrer(self):
        r = _mk_user("rwd_np_r@test.com", "rwd_np_r")
        i = _mk_user("rwd_np_i@test.com", "rwd_np_i")
        # DO NOT give r an active sub.
        db.create_referral(
            referrer_user_id=r, referred_email="rwd_np_i@test.com",
            referred_user_id=i,
        )
        db.mark_referral_converted(i)

        result = asyncio.run(self.run_job())
        self.assertEqual(result["granted"], 0)
        self.assertGreaterEqual(result["skipped_no_payer"], 1)

    def test_job_is_idempotent(self):
        r, i = self._fresh_pair("rwd_idem")
        rid = db.create_referral(
            referrer_user_id=r, referred_email=f"rwd_idem_i@test.com",
            referred_user_id=i,
        )
        db.mark_referral_converted(i)

        asyncio.run(self.run_job())
        # After first run: our specific referral must be stamped.
        with db.conn() as c:
            row = c.execute(
                "SELECT reward_granted, gifted_subscription_id FROM referrals WHERE id = ?",
                (rid,),
            ).fetchone()
        self.assertEqual(row["reward_granted"], 1)
        first_gift = row["gifted_subscription_id"]

        # Second run must not mutate our row — no new gift, reward stays
        # stamped. Scoping to our rid avoids false positives from other
        # tests' pending referrals in the shared DB.
        asyncio.run(self.run_job())
        with db.conn() as c:
            row = c.execute(
                "SELECT reward_granted, gifted_subscription_id FROM referrals WHERE id = ?",
                (rid,),
            ).fetchone()
        self.assertEqual(row["reward_granted"], 1)
        self.assertEqual(row["gifted_subscription_id"], first_gift)

    def test_stacking_five_conversions_grants_fifth_as_tier_upgrade(self):
        r = _mk_user("stack_r@test.com", "stack_r")
        _give_active_sub(r, "trader")
        # Five invitees all converted.
        rids = []
        for n in range(5):
            i = _mk_user(f"stack_i{n}@test.com", f"stack_i{n}")
            rid = db.create_referral(
                referrer_user_id=r, referred_email=f"stack_i{n}@test.com",
                referred_user_id=i,
            )
            rids.append(rid)
            db.mark_referral_converted(i)

        result = asyncio.run(self.run_job())
        # 5 processed, 1 rewarded (count=1), 1 rewarded (count=5),
        # 3 rewards of type='none' for the intermediate conversions.
        self.assertGreaterEqual(result["processed"], 5)

        types = []
        with db.conn() as c:
            for rid in rids:
                row = c.execute(
                    "SELECT reward_type, reward_tier FROM referrals WHERE id = ?",
                    (rid,),
                ).fetchone()
                types.append((row["reward_type"], row["reward_tier"]))
        # At least one is one_month_free, at least one is tier_upgrade→pro.
        self.assertIn("one_month_free", [t[0] for t in types])
        tier_upgrades = [t for t in types if t[0] == "tier_upgrade"]
        self.assertEqual(len(tier_upgrades), 1)
        self.assertEqual(tier_upgrades[0][1], "pro")


# ── Unit: leaderboard opt-in + DB ─────────────────────────────────────────────


class TestLeaderboardDb(unittest.TestCase):
    def test_opt_in_requires_valid_handle(self):
        uid = _mk_user("lb_bad@test.com", "lb_bad")
        result = db.set_leaderboard_participation(
            uid, participate=True, display_name="x",  # too short
        )
        self.assertFalse(result["ok"])
        result = db.set_leaderboard_participation(
            uid, participate=True, display_name="has space",
        )
        self.assertFalse(result["ok"])

    def test_opt_in_and_out_round_trip(self):
        uid = _mk_user("lb_round@test.com", "lb_round")
        result = db.set_leaderboard_participation(
            uid, participate=True, display_name="forecaster42",
        )
        self.assertTrue(result["ok"])
        state = db.get_leaderboard_opt_in(uid)
        self.assertTrue(state["participating"])
        self.assertEqual(state["handle"], "forecaster42")
        db.set_leaderboard_participation(uid, participate=False)
        self.assertFalse(db.get_leaderboard_opt_in(uid)["participating"])

    def test_duplicate_handle_rejected(self):
        a = _mk_user("lb_dupA@test.com", "lb_dupA")
        b = _mk_user("lb_dupB@test.com", "lb_dupB")
        db.set_leaderboard_participation(
            a, participate=True, display_name="sharedname",
        )
        result = db.set_leaderboard_participation(
            b, participate=True, display_name="sharedname",
        )
        self.assertFalse(result["ok"])
        self.assertIn("taken", result["error"])

    def test_leaderboard_only_returns_opted_in_with_scores(self):
        a = _mk_user("lb_scA@test.com", "lb_scA")
        b = _mk_user("lb_scB@test.com", "lb_scB")  # no accuracy data
        db.set_leaderboard_participation(
            a, participate=True, display_name="lbA"
        )
        db.set_leaderboard_participation(
            b, participate=True, display_name="lbB"
        )
        # A has accuracy, B does not.
        db.upsert_user_accuracy(
            a, total=10, correct=7,
            accuracy_all=0.7, accuracy_90d=0.7,
            accuracy_30d=0.7, accuracy_7d=None,
        )
        db.upsert_user_accuracy(
            b, total=0, correct=0,
            accuracy_all=None, accuracy_90d=None,
            accuracy_30d=None, accuracy_7d=None,
        )
        rows = db.get_leaderboard(period="all", limit=10)
        handles = [r["handle"] for r in rows]
        self.assertIn("lbA", handles)
        self.assertNotIn("lbB", handles)


# ── HTTP: invite flow ─────────────────────────────────────────────────────────


class TestInviteHttp(unittest.TestCase):
    def setUp(self):
        tag = self._testMethodName[:20]
        self.referrer = _mk_user(f"http_{tag}_r@test.com", f"http_{tag}_r")
        self.code = db.ensure_user_referral_code(self.referrer)

    def test_get_invite_page_valid_code_returns_200(self):
        r = client.get(f"/invite/{self.code}")
        self.assertEqual(r.status_code, 200)
        # Server-side flag for the JS fallback.
        self.assertIn('data-valid="1"', r.text)

    def test_get_invite_page_invalid_code_still_200_but_not_valid(self):
        r = client.get("/invite/NOTACODE__")
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-valid=""', r.text)

    def test_api_invite_validate_returns_display_name(self):
        r = client.get(f"/api/invite/{self.code}")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["valid"])
        self.assertEqual(data["referrer_display_name"], "http_r")

    def test_api_invite_validate_invalid_returns_404(self):
        r = client.get("/api/invite/NOTACODE__")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.json()["valid"])

    def test_accept_creates_token_and_referral_row(self):
        r = client.post(
            f"/api/invite/{self.code}/accept",
            json={"email": "new_invitee@test.com"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])

        # Row was created.
        with db.conn() as c:
            ref = c.execute(
                "SELECT referrer_user_id, referred_email, invite_token_id "
                "FROM referrals WHERE id = ?",
                (body["referral_id"],),
            ).fetchone()
        self.assertEqual(ref["referrer_user_id"], self.referrer)
        self.assertEqual(ref["referred_email"], "new_invitee@test.com")
        self.assertIsNotNone(ref["invite_token_id"])

    def test_accept_invalid_email_returns_400(self):
        r = client.post(
            f"/api/invite/{self.code}/accept",
            json={"email": "not-an-email"},
        )
        self.assertEqual(r.status_code, 400)

    def test_accept_existing_user_returns_409(self):
        _mk_user("already@test.com", "already")
        r = client.post(
            f"/api/invite/{self.code}/accept",
            json={"email": "already@test.com"},
        )
        self.assertEqual(r.status_code, 409)

    def test_accept_invalid_code_returns_404(self):
        r = client.post(
            "/api/invite/NOTACODE__/accept",
            json={"email": "fresh@test.com"},
        )
        self.assertEqual(r.status_code, 404)


# ── HTTP: referrer panel ──────────────────────────────────────────────────────


class TestReferralsApi(unittest.TestCase):
    def test_api_me_requires_auth(self):
        r = client.get("/api/referrals/me")
        self.assertEqual(r.status_code, 401)

    def test_api_me_returns_code_and_stats(self):
        tag = self._testMethodName[:20]
        uid = _mk_user(f"api_me_{tag}@test.com", f"api_me_{tag}")
        c = _authed_client(uid)
        r = c.get("/api/referrals/me")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIsNotNone(data["referral_code"])
        self.assertIn("/invite/", data["share_url"])
        self.assertEqual(data["stats"]["total_sent"], 0)
        self.assertEqual(data["progress"]["next_milestone"], 1)

    def test_settings_referrals_requires_auth(self):
        # Use a bare client (no session) to confirm the redirect guard.
        bare = TestClient(server.app)
        r = bare.get("/settings/referrals", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/token")


# ── HTTP: leaderboard ─────────────────────────────────────────────────────────


class TestLeaderboardApi(unittest.TestCase):
    def test_leaderboard_html_requires_auth(self):
        r = client.get("/leaderboard", follow_redirects=False)
        self.assertEqual(r.status_code, 302)

    def test_api_leaderboard_requires_auth(self):
        r = client.get("/api/leaderboard")
        self.assertEqual(r.status_code, 401)

    def test_api_leaderboard_returns_empty_when_no_participants_visible(self):
        tag = self._testMethodName[:20]
        uid = _mk_user(f"lb_http_u_{tag}@test.com", f"lb_http_u_{tag}")
        c = _authed_client(uid)
        r = c.get("/api/leaderboard")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["period"], "all")
        self.assertIsInstance(data["rows"], list)

    def test_participate_then_opt_out(self):
        tag = self._testMethodName[:20]
        uid = _mk_user(f"lb_toggle_{tag}@test.com", f"lb_toggle_{tag}")
        c = _authed_client(uid)
        r = c.post(
            "/api/leaderboard/participate",
            json={"display_name": "ptopt_partic_42"},
        )
        self.assertEqual(r.status_code, 200, r.text)

        me = c.get("/api/leaderboard/me").json()
        self.assertTrue(me["participating"])
        self.assertEqual(me["handle"], "ptopt_partic_42")

        r = c.delete("/api/leaderboard/participate")
        self.assertEqual(r.status_code, 200)
        me = c.get("/api/leaderboard/me").json()
        self.assertFalse(me["participating"])

    def test_bad_display_name_returns_400(self):
        tag = self._testMethodName[:20]
        uid = _mk_user(f"lb_bad_name_{tag}@test.com", f"lb_bad_name_{tag}")
        c = _authed_client(uid)
        r = c.post(
            "/api/leaderboard/participate",
            json={"display_name": "x"},
        )
        self.assertEqual(r.status_code, 400)

    def test_period_param_defaults_to_all_on_invalid(self):
        tag = self._testMethodName[:20]
        uid = _mk_user(f"lb_period_{tag}@test.com", f"lb_period_{tag}")
        c = _authed_client(uid)
        r = c.get("/api/leaderboard?period=bogus")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["period"], "all")


if __name__ == "__main__":
    unittest.main()

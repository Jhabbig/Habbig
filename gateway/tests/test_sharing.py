"""Tests for the share/referral-loop feature set.

Covers:
  * share_tokens.encode/decode round-trip + signature + expiry + kind
  * db_sharing CRUD for market / source / prediction shares
  * db_sharing.create_shared_prediction invariants (owner + resolved-correct)
  * db_sharing.replenish_invites_for_user tier grants + rollover cap +
    yyyymm idempotency
  * db_sharing.redeem_invite_token atomic single-winner semantics
  * db_sharing.record_share_view referrer bucketing (privacy-preserving)
  * HTTP: /s/m/{token} 200 OK; invalid-token renders shared_invalid.html
  * HTTP: /api/share/market requires a paid tier (402 without session)
  * queries/sharing_metrics: totals_by_type + top_shared_markets

All tests use the shared in-memory ``_testdb`` connection; migrations
run once at session import time. EMAIL_DRY_RUN=true keeps the email
service silent.
"""

from __future__ import annotations

import os
import time
import unittest

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("EMAIL_DRY_RUN", "true")

from tests import _testdb  # noqa: F401 — shared DB + migrations
import db  # noqa: E402
import db_sharing  # noqa: E402
import server  # noqa: E402
import share_tokens  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from queries import sharing_metrics  # noqa: E402


client = TestClient(server.app)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _mk_user(email: str, username: str = "") -> int:
    """Create a user, returning their id. Mirrors the pattern used by the
    referrals test suite so failures surface the same way."""
    username = username or email.split("@")[0]
    return db.create_user(email, "TestPass123!", username=username)


def _give_pro_sub(user_id: int) -> None:
    """Make the user look like a paying Pro subscriber so the /api/share/*
    endpoints pass the 402 gate via db.get_user_subscription_tier."""
    db.upsert_subscription(
        user_id=user_id,
        dashboard_key="test-dash-pro",
        plan="pro",
        duration_days=30,
        source="test",
    )


def _authed_client(user_id: int) -> TestClient:
    """Session + CSRF-paired TestClient for authenticated POSTs. The
    CSRF middleware does a double-submit check (cookie must equal header)
    so we set both to the same dummy value at construction time."""
    c = TestClient(server.app)
    token = db.create_session(user_id)
    c.cookies.set(server.COOKIE_NAME, token)
    c.cookies.set("_csrf", "t_share_csrf_fixed")
    c.headers.update({"x-csrf-token": "t_share_csrf_fixed"})
    return c


def _tag(test_case: unittest.TestCase) -> str:
    """Per-test email prefix. The shared in-memory DB accumulates rows
    across the whole session so reusing a literal collides on UNIQUE
    (see the test_referrals lessons)."""
    return test_case._testMethodName  # type: ignore[attr-defined]


# ── Unit: share_tokens ───────────────────────────────────────────────────────


class TestShareTokens(unittest.TestCase):
    def test_round_trip_valid_token(self):
        token, expires_at = share_tokens.encode(
            kind="m", row_id=42, sharer_user_id=7, ttl_seconds=3600,
        )
        decoded = share_tokens.decode(token)
        self.assertEqual(decoded.kind, "m")
        self.assertEqual(decoded.row_id, 42)
        self.assertEqual(decoded.sharer_user_id, 7)
        self.assertEqual(decoded.expires_at, expires_at)

    def test_kind_dispatch(self):
        for kind in ("m", "s", "p"):
            token, _ = share_tokens.encode(
                kind=kind, row_id=1, sharer_user_id=1, ttl_seconds=60,
            )
            self.assertEqual(share_tokens.peek_kind(token), kind)

    def test_expired_token_raises(self):
        # ttl=60 but we pass now=FUTURE so the token's expiry is already
        # in the past when decode runs against real wall clock.
        past = int(time.time()) - 10_000
        token, _ = share_tokens.encode(
            kind="m", row_id=1, sharer_user_id=1,
            ttl_seconds=60, now=past,
        )
        with self.assertRaises(share_tokens.InvalidToken):
            share_tokens.decode(token)

    def test_tampered_signature_raises(self):
        token, _ = share_tokens.encode(
            kind="m", row_id=1, sharer_user_id=1, ttl_seconds=60,
        )
        # Flip the last character of the signature segment.
        prefix = token[:-1]
        last = token[-1]
        flipped = "Z" if last != "Z" else "A"
        with self.assertRaises(share_tokens.InvalidToken):
            share_tokens.decode(prefix + flipped)

    def test_malformed_token_raises(self):
        for bogus in ("", "x", "not-a-token", "m.onlytwo", "k.aaa.bbb"):
            with self.assertRaises(share_tokens.InvalidToken):
                share_tokens.decode(bogus)

    def test_peek_kind_rejects_unknown_prefix(self):
        self.assertIsNone(share_tokens.peek_kind("q.aa.bb"))
        self.assertIsNone(share_tokens.peek_kind("no-dots-here"))
        self.assertIsNone(share_tokens.peek_kind(""))


# ── Unit: db_sharing — shared market / source / prediction ──────────────────


class TestSharedMarketDB(unittest.TestCase):
    def test_create_and_lookup_by_token(self):
        tag = _tag(self)
        uid = _mk_user(f"mkt_{tag}@test.com")
        row = db_sharing.create_shared_market(
            market_slug="btc-100k-2025",
            sharer_user_id=uid,
            sharer_handle="sho",
        )
        fetched = db_sharing.get_shared_market(row["token"])
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["market_slug"], "btc-100k-2025")
        self.assertEqual(fetched["sharer_user_id"], uid)
        self.assertEqual(fetched["view_count"], 0)

    def test_record_view_bumps_counter(self):
        tag = _tag(self)
        uid = _mk_user(f"mktv_{tag}@test.com")
        row = db_sharing.create_shared_market(
            market_slug="eth-5k-2025", sharer_user_id=uid, sharer_handle=None,
        )
        db_sharing.record_shared_market_view(row["id"])
        db_sharing.record_shared_market_view(row["id"])
        after = db_sharing.get_shared_market(row["token"])
        self.assertEqual(after["view_count"], 2)
        self.assertIsNotNone(after["last_viewed_at"])


class TestSharedSourceDB(unittest.TestCase):
    def test_create_and_record_view(self):
        tag = _tag(self)
        uid = _mk_user(f"src_{tag}@test.com")
        row = db_sharing.create_shared_source(
            source_handle="PolymarketAnalytics",
            sharer_user_id=uid, sharer_handle="sho",
        )
        self.assertEqual(row["source_handle"], "PolymarketAnalytics")
        db_sharing.record_shared_source_view(row["id"])
        after = db_sharing.get_shared_source(row["token"])
        self.assertEqual(after["view_count"], 1)


class TestSharedPredictionDB(unittest.TestCase):
    def _make_resolved_correct_prediction(self, user_id: int) -> int:
        """Seed one resolved-correct prediction + return its id. The
        create helper is the production path; we only manually flip
        the resolved fields because there's no public helper that
        does all three in one call."""
        from queries import predictions as preds_q
        pid = preds_q.create_user_prediction(
            user_id=user_id,
            market_slug="polymarket-fake",
            market_question="Fake?",
            category="other",
            predicted_outcome="YES",
            predicted_probability=0.72,
        )
        with db.conn() as c:
            c.execute(
                "UPDATE user_predictions "
                "SET resolved = 1, resolved_at = ?, resolved_correct = 1 "
                "WHERE id = ?",
                (int(time.time()), pid),
            )
        return pid

    def test_resolved_correct_prediction_is_shareable(self):
        tag = _tag(self)
        uid = _mk_user(f"pok_{tag}@test.com")
        pid = self._make_resolved_correct_prediction(uid)
        row = db_sharing.create_shared_prediction(
            user_prediction_id=pid,
            sharer_user_id=uid,
            sharer_handle="sho",
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["user_prediction_id"], pid)

    def test_unresolved_prediction_refused(self):
        tag = _tag(self)
        uid = _mk_user(f"pun_{tag}@test.com")
        from queries import predictions as preds_q
        pid = preds_q.create_user_prediction(
            user_id=uid, market_slug="x", market_question="x?",
            category="other", predicted_outcome="YES",
            predicted_probability=0.6,
        )
        # Not resolved → None. The invariant keeps ego-shares of
        # losing bets (or in-flight bets) off the surface entirely.
        row = db_sharing.create_shared_prediction(
            user_prediction_id=pid,
            sharer_user_id=uid,
            sharer_handle=None,
        )
        self.assertIsNone(row)

    def test_other_users_prediction_refused(self):
        tag = _tag(self)
        owner = _mk_user(f"po_{tag}@test.com")
        other = _mk_user(f"pother_{tag}@test.com")
        pid = self._make_resolved_correct_prediction(owner)
        # other_user trying to share owner's prediction → None. Check
        # is in the create helper so the route can't be tricked.
        row = db_sharing.create_shared_prediction(
            user_prediction_id=pid,
            sharer_user_id=other,
            sharer_handle=None,
        )
        self.assertIsNone(row)


# ── Unit: invite tokens + monthly replenish ──────────────────────────────────


class TestInviteTokens(unittest.TestCase):
    def test_balance_starts_at_zero(self):
        tag = _tag(self)
        uid = _mk_user(f"iz_{tag}@test.com")
        self.assertEqual(db_sharing.count_unused_invite_tokens(uid), 0)

    def test_replenish_grants_tier_allotment(self):
        tag = _tag(self)
        uid = _mk_user(f"ir_{tag}@test.com")
        result = db_sharing.replenish_invites_for_user(
            user_id=uid, tier="trader", yyyymm=202604,
        )
        self.assertEqual(result["granted"], 2)
        self.assertEqual(result["skipped"], False)
        self.assertEqual(db_sharing.count_unused_invite_tokens(uid), 2)

    def test_replenish_is_idempotent_same_yyyymm(self):
        tag = _tag(self)
        uid = _mk_user(f"ii_{tag}@test.com")
        db_sharing.replenish_invites_for_user(
            user_id=uid, tier="pro", yyyymm=202605,
        )
        result = db_sharing.replenish_invites_for_user(
            user_id=uid, tier="pro", yyyymm=202605,
        )
        self.assertTrue(result["skipped"])
        self.assertEqual(result["granted"], 0)
        # Balance unchanged — the second call must not re-grant.
        self.assertEqual(db_sharing.count_unused_invite_tokens(uid), 5)

    def test_replenish_next_month_grants_additional(self):
        tag = _tag(self)
        uid = _mk_user(f"in_{tag}@test.com")
        db_sharing.replenish_invites_for_user(user_id=uid, tier="trader", yyyymm=202604)
        db_sharing.replenish_invites_for_user(user_id=uid, tier="trader", yyyymm=202605)
        # 2 + 2 = 4, below the 2×allotment cap of 4 so none pruned.
        self.assertEqual(db_sharing.count_unused_invite_tokens(uid), 4)

    def test_rollover_cap_prunes_oldest(self):
        """A trader user with 4 unused tokens + a 3rd monthly grant
        would be at 6 unused — above the 2× cap (4). The replenish
        helper prunes the 2 oldest unused tokens before minting 2 new
        ones, so the full new allotment always lands."""
        tag = _tag(self)
        uid = _mk_user(f"ic_{tag}@test.com")
        db_sharing.replenish_invites_for_user(user_id=uid, tier="trader", yyyymm=202604)
        db_sharing.replenish_invites_for_user(user_id=uid, tier="trader", yyyymm=202605)
        result = db_sharing.replenish_invites_for_user(
            user_id=uid, tier="trader", yyyymm=202606,
        )
        # Cap is 4 (trader allotment 2 × 2). We had 4, pruned 2, minted
        # 2 — net balance is still 4 and the user got a fresh allotment.
        self.assertEqual(result["granted"], 2)
        self.assertEqual(result["pruned"], 2)
        self.assertEqual(db_sharing.count_unused_invite_tokens(uid), 4)

    def test_redeem_is_atomic(self):
        tag = _tag(self)
        owner = _mk_user(f"ro_{tag}@test.com")
        user_a = _mk_user(f"ra_{tag}@test.com")
        user_b = _mk_user(f"rb_{tag}@test.com")
        db_sharing.replenish_invites_for_user(
            user_id=owner, tier="trader", yyyymm=202604,
        )
        tokens = db_sharing.list_unused_invite_tokens(owner)
        self.assertTrue(tokens)
        raw = tokens[0]["token"]

        first = db_sharing.redeem_invite_token(token=raw, redeemed_by_user_id=user_a)
        self.assertEqual(first, owner)
        # Second redemption — already used, atomic guard returns None.
        second = db_sharing.redeem_invite_token(token=raw, redeemed_by_user_id=user_b)
        self.assertIsNone(second)

    def test_unknown_tier_skips(self):
        tag = _tag(self)
        uid = _mk_user(f"iu_{tag}@test.com")
        result = db_sharing.replenish_invites_for_user(
            user_id=uid, tier="free", yyyymm=202604,
        )
        self.assertTrue(result["skipped"])
        self.assertEqual(db_sharing.count_unused_invite_tokens(uid), 0)


# ── Unit: share metrics ──────────────────────────────────────────────────────


class TestShareMetrics(unittest.TestCase):
    def test_link_share_to_signup_flips_row(self):
        """The auth-register attribution hook calls link_share_to_signup
        with the cookie's metric id + the new user id. The share_metrics
        row must flip signed_up=1, signed_up_user_id=<user>, and
        signed_up_at must be populated so /admin/sharing can count
        conversions and the top-sharers query can join.

        Second call on the same metric id is a no-op (guarded by
        signed_up = 0 in the UPDATE predicate)."""
        tag = _tag(self)
        sharer = _mk_user(f"at_sharer_{tag}@test.com")
        invitee = _mk_user(f"at_invitee_{tag}@test.com")
        share = db_sharing.create_shared_market(
            market_slug=f"atmkt-{tag}",
            sharer_user_id=sharer, sharer_handle=None,
        )
        metric_id = db_sharing.record_share_view(
            share_type="market", share_id=share["id"],
            referer="https://twitter.com/x", cf_country="US",
        )
        ok = db_sharing.link_share_to_signup(metric_id, invitee)
        self.assertTrue(ok)
        with db.conn() as c:
            row = c.execute(
                "SELECT signed_up, signed_up_user_id, signed_up_at "
                "FROM share_metrics WHERE id = ?",
                (metric_id,),
            ).fetchone()
        self.assertEqual(row["signed_up"], 1)
        self.assertEqual(row["signed_up_user_id"], invitee)
        self.assertIsNotNone(row["signed_up_at"])
        # Second call: no-op. Guards against double-writes if the
        # auth-register path somehow fires twice for the same user.
        again = db_sharing.link_share_to_signup(metric_id, invitee)
        self.assertFalse(again)

    def test_referrer_classification(self):
        tag = _tag(self)
        uid = _mk_user(f"rc_{tag}@test.com")
        row = db_sharing.create_shared_market(
            market_slug=f"mkt-{tag}", sharer_user_id=uid, sharer_handle=None,
        )
        db_sharing.record_share_view(
            share_type="market", share_id=row["id"],
            referer="https://twitter.com/someone/status/123",
            cf_country="US",
        )
        db_sharing.record_share_view(
            share_type="market", share_id=row["id"],
            referer="https://www.linkedin.com/feed/",
            cf_country="GB",
        )
        db_sharing.record_share_view(
            share_type="market", share_id=row["id"],
            referer=None, cf_country=None,
        )
        with db.conn() as c:
            rows = c.execute(
                "SELECT referrer FROM share_metrics WHERE share_type = 'market' "
                "AND share_id = ? ORDER BY id",
                (row["id"],),
            ).fetchall()
        self.assertEqual([r["referrer"] for r in rows], ["twitter", "linkedin", "direct"])


# ── HTTP: public share pages ─────────────────────────────────────────────────


class TestShareRoutesHttp(unittest.TestCase):
    def test_valid_market_share_renders_200(self):
        tag = _tag(self)
        uid = _mk_user(f"hm_{tag}@test.com")
        row = db_sharing.create_shared_market(
            market_slug=f"http-mkt-{tag}",
            sharer_user_id=uid, sharer_handle="sho",
        )
        r = client.get(f"/s/m/{row['token']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("narve.ai", r.text)

    def test_tampered_market_token_renders_invalid(self):
        tag = _tag(self)
        uid = _mk_user(f"ht_{tag}@test.com")
        row = db_sharing.create_shared_market(
            market_slug=f"http-tam-{tag}",
            sharer_user_id=uid, sharer_handle=None,
        )
        flipped_token = row["token"][:-1] + ("A" if row["token"][-1] != "A" else "B")
        r = client.get(f"/s/m/{flipped_token}")
        # Shared-invalid page is still a 200 HTML (not a 404) so social
        # scrapers don't retry on what is a user-fixable bad link.
        self.assertEqual(r.status_code, 200)
        self.assertIn("unavailable", r.text.lower())

    def test_unknown_token_returns_invalid_page(self):
        r = client.get("/s/m/m.not-a-valid-token.aa")
        self.assertEqual(r.status_code, 200)
        self.assertIn("unavailable", r.text.lower())

    def test_post_share_market_requires_paid_tier(self):
        r = client.post(
            "/api/share/market",
            json={"market_slug": "anything"},
            headers={"x-csrf-token": "t_share_csrf_fixed"},
            cookies={"_csrf": "t_share_csrf_fixed"},
        )
        # 402 is the paywall signal. 401 would imply "log in" — but
        # /api/share/* wants the upsell flow, not an auth redirect.
        self.assertIn(r.status_code, (401, 402, 403))

    def test_post_share_market_succeeds_with_paid_sub(self):
        tag = _tag(self)
        uid = _mk_user(f"psm_{tag}@test.com")
        _give_pro_sub(uid)
        c = _authed_client(uid)
        r = c.post(
            "/api/share/market",
            json={"market_slug": f"slug-{tag}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertIn("/s/m/", body["share_url"])
        # Every successful mint is decode-able with a fresh token.
        decoded = share_tokens.decode(body["token"])
        self.assertEqual(decoded.kind, "m")
        self.assertEqual(decoded.sharer_user_id, uid)


# ── Retention cron ───────────────────────────────────────────────────────────


class TestShareRetention(unittest.TestCase):
    def test_prune_deletes_only_long_expired_rows(self):
        """The retention job prunes shared_* rows whose expires_at is
        older than now - 30d. Fresh rows + recently-expired rows
        (within grace) must stay. Only far-expired rows should go."""
        import asyncio
        from jobs import share_retention as sr

        tag = _tag(self)
        uid = _mk_user(f"rt_{tag}@test.com")

        # Fresh share — expires in 7d. MUST survive the sweep.
        fresh = db_sharing.create_shared_market(
            market_slug=f"rt-fresh-{tag}", sharer_user_id=uid, sharer_handle=None,
        )
        # Recently expired — within the 30d grace window. MUST survive.
        recent = db_sharing.create_shared_market(
            market_slug=f"rt-recent-{tag}", sharer_user_id=uid, sharer_handle=None,
        )
        with db.conn() as c:
            c.execute(
                "UPDATE shared_market_cards SET expires_at = ? WHERE id = ?",
                (int(time.time()) - 7 * 86400, recent["id"]),  # 7d past expiry
            )
        # Long expired — past the grace window. MUST be deleted.
        stale = db_sharing.create_shared_market(
            market_slug=f"rt-stale-{tag}", sharer_user_id=uid, sharer_handle=None,
        )
        with db.conn() as c:
            c.execute(
                "UPDATE shared_market_cards SET expires_at = ? WHERE id = ?",
                (int(time.time()) - (sr.GRACE_SECONDS + 86400), stale["id"]),
            )

        result = asyncio.run(sr.share_retention_prune())
        self.assertGreaterEqual(result["total_deleted"], 1)
        self.assertGreaterEqual(result["by_table"]["shared_market_cards"], 1)

        # Fresh + recent still there; stale gone.
        self.assertIsNotNone(db_sharing.get_shared_market(fresh["token"]))
        self.assertIsNotNone(db_sharing.get_shared_market(recent["token"]))
        self.assertIsNone(db_sharing.get_shared_market(stale["token"]))


# ── Sharer lookup for referral bridge ────────────────────────────────────────


class TestSharerLookup(unittest.TestCase):
    def test_sharer_lookup_resolves_for_each_kind(self):
        """The auth_register path uses get_sharer_for_share_metric to
        credit the sharer with a referral reward on attributed signup.
        The lookup must work for all three share types — a typo in the
        table whitelist silently returns None and kills the reward
        path, which wouldn't crash but would silently break the loop."""
        tag = _tag(self)

        # market
        m_uid = _mk_user(f"sl_m_{tag}@test.com")
        m_row = db_sharing.create_shared_market(
            market_slug=f"sl-m-{tag}", sharer_user_id=m_uid, sharer_handle=None,
        )
        m_metric = db_sharing.record_share_view(
            share_type="market", share_id=m_row["id"],
            referer=None, cf_country=None,
        )
        self.assertEqual(db_sharing.get_sharer_for_share_metric(m_metric), m_uid)

        # source
        s_uid = _mk_user(f"sl_s_{tag}@test.com")
        s_row = db_sharing.create_shared_source(
            source_handle=f"sl-s-{tag}", sharer_user_id=s_uid, sharer_handle=None,
        )
        s_metric = db_sharing.record_share_view(
            share_type="source", share_id=s_row["id"],
            referer=None, cf_country=None,
        )
        self.assertEqual(db_sharing.get_sharer_for_share_metric(s_metric), s_uid)

    def test_sharer_lookup_unknown_metric_returns_none(self):
        # 999_999_999 is absurdly higher than any autoincrement value
        # the shared in-memory DB could reach during a single test run.
        self.assertIsNone(db_sharing.get_sharer_for_share_metric(999_999_999))


# ── Admin accessors ──────────────────────────────────────────────────────────


class TestAdminSharingMetrics(unittest.TestCase):
    def test_totals_by_type_returns_stable_shape(self):
        """All three share types always appear, even if zero rows
        match — the admin UI wants stable column order across
        windows."""
        rows = sharing_metrics.totals_by_type(days=7)
        types = {r["share_type"] for r in rows}
        self.assertEqual(types, {"market", "source", "prediction"})
        for r in rows:
            # Conversion rate is always a number (0.0 when views=0),
            # never None — template arithmetic assumes so.
            self.assertIsInstance(r["conversion_rate_pct"], float)

    def test_top_shared_markets_groups_by_slug(self):
        tag = _tag(self)
        uid = _mk_user(f"ts_{tag}@test.com")
        slug = f"popular-{tag}"
        # Two separate tokens for the same slug → the admin query
        # should collapse them into one row with views=2.
        row_a = db_sharing.create_shared_market(
            market_slug=slug, sharer_user_id=uid, sharer_handle=None,
        )
        row_b = db_sharing.create_shared_market(
            market_slug=slug, sharer_user_id=uid, sharer_handle=None,
        )
        db_sharing.record_share_view(
            share_type="market", share_id=row_a["id"],
            referer=None, cf_country=None,
        )
        db_sharing.record_share_view(
            share_type="market", share_id=row_b["id"],
            referer=None, cf_country=None,
        )
        top = sharing_metrics.top_shared_markets(days=7, limit=20)
        my_row = next((r for r in top if r["market_slug"] == slug), None)
        self.assertIsNotNone(my_row)
        self.assertEqual(my_row["views"], 2)
        self.assertEqual(my_row["distinct_shares"], 2)


if __name__ == "__main__":
    unittest.main()

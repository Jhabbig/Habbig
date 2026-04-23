"""Tests for the public + admin feedback routes (migration 130 surface).

Covers the core user-visible behaviours:

  * TestSubmit              — authed submit lands a row; invalid payloads
                              are rejected; the public flag routes
                              correctly.
  * TestPublicList          — /feedback shows public posts, hides private
                              ones, respects type/status/sort filters.
  * TestDetailPage          — public item renders; private 404s for non-
                              owner / non-admin; owner + admin can see.
  * TestVote                — subscriber can upvote; free-tier can't;
                              toggling re-vote decrements.
  * TestAdminTriage         — /admin/feedback renders; non-admin blocked.
  * TestStatusNotification  — admin status change writes a notifications
                              row for the submitter.
  * TestDuplicateAndShip    — admin marks dup + records commit sha.
  * TestComments            — user + admin comments round-trip through
                              the detail page.

Shared DB via tests._testdb — USES_TESTDB marker + manual re-pin keeps
sibling tests from stealing our connection.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

USES_TESTDB = True

from tests import _testdb  # noqa: E402,F401

import db  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


client = TestClient(server.app)

_SHARED_DB_CONN = _testdb._fake_conn


def _pin_shared_db() -> None:
    db.conn = _SHARED_DB_CONN


def _clear_client() -> None:
    try:
        client.cookies.clear()
    except Exception:
        pass


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _pin_shared_db()
        super().setUpClass()

    def setUp(self):
        _pin_shared_db()
        _clear_client()
        # Isolate per-test so ordering doesn't matter.
        with db.conn() as c:
            c.execute("DELETE FROM feedback_comments")
            c.execute("DELETE FROM feedback_votes")
            c.execute("DELETE FROM feedback_items")
        super().setUp()


def _make_user(email: str, username: str, *, sub: bool = True, admin: bool = False) -> tuple[int, str]:
    uid = db.create_user(email, "TestPass123!", username=username)
    if admin:
        with db.conn() as c:
            c.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (uid,))
    if sub:
        now = int(time.time())
        with db.conn() as c:
            c.execute(
                "INSERT INTO subscriptions "
                "(user_id, dashboard_key, plan, status, started_at, expires_at) "
                "VALUES (?, '__plan__', 'pro_annual', 'active', ?, ?)",
                (uid, now, now + 300 * 86400),
            )
    token = db.create_session(uid)
    return uid, token


def _prime_csrf(token: str) -> str:
    client.get("/feedback", cookies={server.COOKIE_NAME: token}, follow_redirects=False)
    return client.cookies.get("_csrf") or ""


def _post_form(path: str, token: str, data: dict | None = None, *, accept_json: bool = False):
    csrf = _prime_csrf(token)
    payload = dict(data or {})
    if csrf:
        payload["_csrf"] = csrf
    headers = {"Accept": "application/json"} if accept_json else {}
    return client.post(
        path,
        data=payload,
        cookies={server.COOKIE_NAME: token, "_csrf": csrf},
        headers=headers,
        follow_redirects=False,
    )


def _seed_item(user_id: int, **overrides) -> int:
    """Insert a feedback_items row directly. Returns the new id."""
    row = {
        "user_id": user_id,
        "type": overrides.get("type", "feature"),
        "title": overrides.get("title", "Sample item"),
        "body": overrides.get("body", "Sample body"),
        "status": overrides.get("status", "open"),
        "is_public": overrides.get("is_public", 1),
    }
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO feedback_items (user_id, type, title, body, status, is_public) "
            "VALUES (:user_id, :type, :title, :body, :status, :is_public)",
            row,
        )
        return int(cur.lastrowid or 0)


# ── Submission ───────────────────────────────────────────────────────────────


class TestSubmit(_Base):
    def test_authed_submit_creates_public_row(self):
        uid, token = _make_user("sub-pub@t.com", "sub_pub")
        r = _post_form(
            "/api/feedback", token,
            data={"type": "bug", "title": "Dark mode flash", "body": "White flash on load", "is_public": "1"},
            accept_json=True,
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["is_public"])
        with db.conn() as c:
            row = c.execute(
                "SELECT type, title, is_public, status FROM feedback_items WHERE id = ?",
                (body["id"],),
            ).fetchone()
        self.assertEqual(row["type"], "bug")
        self.assertEqual(row["is_public"], 1)
        self.assertEqual(row["status"], "open")

    def test_authed_submit_creates_private_row(self):
        uid, token = _make_user("sub-priv@t.com", "sub_priv")
        r = _post_form(
            "/api/feedback", token,
            data={"type": "question", "title": "Something private", "body": "Don't share", "is_public": "0"},
            accept_json=True,
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["is_public"])
        with db.conn() as c:
            row = c.execute(
                "SELECT is_public FROM feedback_items WHERE id = ?",
                (body["id"],),
            ).fetchone()
        self.assertEqual(row["is_public"], 0)

    def test_unauth_submit_rejected(self):
        r = client.post(
            "/api/feedback",
            data={"type": "bug", "title": "Hi", "body": "Hi"},
            follow_redirects=False,
        )
        # CSRF middleware may 403 before auth gate fires; either is a rejection.
        self.assertIn(r.status_code, (401, 403))

    def test_empty_title_rejected(self):
        _, token = _make_user("sub-empty@t.com", "sub_empty")
        r = _post_form(
            "/api/feedback", token,
            data={"type": "feature", "title": "", "body": "some body"},
            accept_json=True,
        )
        self.assertEqual(r.status_code, 400)

    def test_unknown_type_is_coerced_to_feature(self):
        uid, token = _make_user("sub-coerce@t.com", "sub_coerce")
        r = _post_form(
            "/api/feedback", token,
            data={"type": "wut", "title": "Coerce me", "body": "Details"},
            accept_json=True,
        )
        self.assertEqual(r.status_code, 200)
        with db.conn() as c:
            row = c.execute(
                "SELECT type FROM feedback_items WHERE id = ?",
                (r.json()["id"],),
            ).fetchone()
        self.assertEqual(row["type"], "feature")


# ── Public list / filters ────────────────────────────────────────────────────


class TestPublicList(_Base):
    def test_list_shows_public_hides_private(self):
        owner_uid, _ = _make_user("owner@t.com", "owner")
        other_uid, other_token = _make_user("other@t.com", "other")
        _seed_item(owner_uid, title="Public A", is_public=1)
        _seed_item(owner_uid, title="Public B", is_public=1)
        _seed_item(owner_uid, title="Private X", is_public=0)
        r = client.get("/feedback", cookies={server.COOKIE_NAME: other_token}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Public A", r.text)
        self.assertIn("Public B", r.text)
        self.assertNotIn("Private X", r.text)

    def test_type_filter(self):
        uid, token = _make_user("type-filter@t.com", "type_filter")
        _seed_item(uid, type="bug", title="My bug")
        _seed_item(uid, type="feature", title="My feature")
        r = client.get("/feedback?type=bug", cookies={server.COOKIE_NAME: token}, follow_redirects=False)
        self.assertIn("My bug", r.text)
        self.assertNotIn("My feature", r.text)

    def test_status_filter(self):
        uid, token = _make_user("status-filter@t.com", "status_filter")
        _seed_item(uid, title="Open item", status="open")
        _seed_item(uid, title="Shipped item", status="shipped")
        r = client.get("/feedback?status=shipped", cookies={server.COOKIE_NAME: token}, follow_redirects=False)
        self.assertIn("Shipped item", r.text)
        self.assertNotIn("Open item", r.text)

    def test_sort_top_puts_high_votes_first(self):
        uid, token = _make_user("sort-top@t.com", "sort_top")
        low = _seed_item(uid, title="Low votes")
        high = _seed_item(uid, title="High votes")
        with db.conn() as c:
            c.execute("UPDATE feedback_items SET upvotes = 10 WHERE id = ?", (high,))
            c.execute("UPDATE feedback_items SET upvotes = 1 WHERE id = ?", (low,))
        r = client.get("/feedback?sort=top", cookies={server.COOKIE_NAME: token}, follow_redirects=False)
        hi_idx = r.text.find("High votes")
        lo_idx = r.text.find("Low votes")
        self.assertGreater(hi_idx, -1)
        self.assertLess(hi_idx, lo_idx, "'High votes' should render before 'Low votes' when sorting by top")

    def test_unauth_redirected_to_login(self):
        r = client.get("/feedback", follow_redirects=False)
        self.assertIn(r.status_code, (302, 307))
        self.assertIn("/token", r.headers["location"])


# ── Detail page ──────────────────────────────────────────────────────────────


class TestDetailPage(_Base):
    def test_public_item_visible_to_any_user(self):
        owner_uid, _ = _make_user("owner-d@t.com", "owner_d")
        _, viewer_token = _make_user("viewer-d@t.com", "viewer_d")
        item_id = _seed_item(owner_uid, title="Discoverable", is_public=1)
        r = client.get(f"/feedback/{item_id}", cookies={server.COOKIE_NAME: viewer_token}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Discoverable", r.text)

    def test_private_item_hidden_from_other_users(self):
        owner_uid, _ = _make_user("owner-p@t.com", "owner_p")
        _, viewer_token = _make_user("viewer-p@t.com", "viewer_p")
        item_id = _seed_item(owner_uid, title="PrivateTitle", is_public=0)
        r = client.get(f"/feedback/{item_id}", cookies={server.COOKIE_NAME: viewer_token}, follow_redirects=False)
        self.assertEqual(r.status_code, 404)

    def test_private_item_visible_to_owner(self):
        owner_uid, owner_token = _make_user("owner-v@t.com", "owner_v")
        item_id = _seed_item(owner_uid, title="OwnerSecret", is_public=0)
        r = client.get(f"/feedback/{item_id}", cookies={server.COOKIE_NAME: owner_token}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("OwnerSecret", r.text)

    def test_private_item_visible_to_admin(self):
        owner_uid, _ = _make_user("owner-adm@t.com", "owner_adm")
        _, admin_token = _make_user("admin-v@t.com", "admin_v", admin=True)
        item_id = _seed_item(owner_uid, title="AdminCanSee", is_public=0)
        r = client.get(f"/feedback/{item_id}", cookies={server.COOKIE_NAME: admin_token}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("AdminCanSee", r.text)

    def test_missing_item_returns_404(self):
        _, token = _make_user("miss@t.com", "miss")
        r = client.get("/feedback/99999999", cookies={server.COOKIE_NAME: token}, follow_redirects=False)
        self.assertEqual(r.status_code, 404)


# ── Voting ───────────────────────────────────────────────────────────────────


class TestVote(_Base):
    def test_subscriber_can_vote_and_unvote(self):
        author_uid, _ = _make_user("vote-auth@t.com", "vote_auth")
        voter_uid, voter_token = _make_user("vote-voter@t.com", "vote_voter")
        item_id = _seed_item(author_uid, title="Vote target")

        r1 = _post_form(f"/api/feedback/{item_id}/vote", voter_token, data={}, accept_json=True)
        self.assertEqual(r1.status_code, 200)
        self.assertTrue(r1.json()["voted"])
        self.assertEqual(r1.json()["upvotes"], 1)

        # Vote again → toggle off.
        r2 = _post_form(f"/api/feedback/{item_id}/vote", voter_token, data={}, accept_json=True)
        self.assertEqual(r2.status_code, 200)
        self.assertFalse(r2.json()["voted"])
        self.assertEqual(r2.json()["upvotes"], 0)

    def test_free_tier_blocked_from_voting(self):
        author_uid, _ = _make_user("free-auth@t.com", "free_auth")
        _, free_token = _make_user("free-voter@t.com", "free_voter", sub=False)
        item_id = _seed_item(author_uid, title="Payment-gated")
        r = _post_form(f"/api/feedback/{item_id}/vote", free_token, data={}, accept_json=True)
        self.assertEqual(r.status_code, 402)

    def test_vote_on_missing_item_returns_404(self):
        _, token = _make_user("vote-miss@t.com", "vote_miss")
        r = _post_form("/api/feedback/99999999/vote", token, data={}, accept_json=True)
        self.assertEqual(r.status_code, 404)


# ── Admin triage ─────────────────────────────────────────────────────────────


class TestAdminTriage(_Base):
    def test_admin_sees_both_public_and_private(self):
        owner_uid, _ = _make_user("ow-admin@t.com", "ow_admin")
        _, admin_token = _make_user("adm@t.com", "adm", admin=True)
        _seed_item(owner_uid, title="PublicA", is_public=1)
        _seed_item(owner_uid, title="SecretB", is_public=0)
        r = client.get("/admin/feedback", cookies={server.COOKIE_NAME: admin_token}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("PublicA", r.text)
        self.assertIn("SecretB", r.text)

    def test_non_admin_redirected_away(self):
        _, user_token = _make_user("reg@t.com", "reg")
        r = client.get("/admin/feedback", cookies={server.COOKIE_NAME: user_token}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 307, 403))

    def test_unauth_redirected_to_login(self):
        r = client.get("/admin/feedback", follow_redirects=False)
        self.assertIn(r.status_code, (302, 307))


# ── Status notifications ─────────────────────────────────────────────────────


class TestStatusNotification(_Base):
    def test_status_change_creates_notification(self):
        owner_uid, _ = _make_user("notif-owner@t.com", "notif_owner")
        _, admin_token = _make_user("notif-adm@t.com", "notif_adm", admin=True)
        item_id = _seed_item(owner_uid, title="Notify me", is_public=1)

        r = _post_form(
            f"/admin/feedback/{item_id}/status", admin_token,
            data={"status": "in_progress"},
        )
        self.assertEqual(r.status_code, 302)

        with db.conn() as c:
            notif = c.execute(
                "SELECT title, link_url, body, metadata FROM notifications "
                "WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                (owner_uid,),
            ).fetchone()
        self.assertIsNotNone(notif, "submitter should get a notification row")
        self.assertEqual(notif["link_url"], f"/feedback/{item_id}")
        self.assertIn("in_progress", notif["body"])
        meta = json.loads(notif["metadata"]) if notif["metadata"] else {}
        self.assertEqual(meta.get("feedback_id"), item_id)

    def test_admin_self_edit_does_not_self_notify(self):
        """Admin triaging their own submission shouldn't blow up their
        own notification bell."""
        uid, token = _make_user("admin-self@t.com", "admin_self", admin=True)
        item_id = _seed_item(uid, title="Self edit")
        _post_form(f"/admin/feedback/{item_id}/status", token, data={"status": "declined"})
        with db.conn() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE user_id = ?",
                (uid,),
            ).fetchone()["n"]
        self.assertEqual(n, 0)


# ── Duplicate + ship ─────────────────────────────────────────────────────────


class TestDuplicateAndShip(_Base):
    def test_mark_duplicate(self):
        owner_uid, _ = _make_user("dup-own@t.com", "dup_own")
        _, admin_token = _make_user("dup-adm@t.com", "dup_adm", admin=True)
        canonical = _seed_item(owner_uid, title="Canonical")
        dup = _seed_item(owner_uid, title="Duplicate item")
        r = _post_form(
            f"/admin/feedback/{dup}/duplicate", admin_token,
            data={"duplicate_of": str(canonical)},
        )
        self.assertEqual(r.status_code, 302)
        with db.conn() as c:
            row = c.execute(
                "SELECT status, duplicate_of FROM feedback_items WHERE id = ?",
                (dup,),
            ).fetchone()
        self.assertEqual(row["status"], "dup")
        self.assertEqual(row["duplicate_of"], canonical)

    def test_ship_records_commit_sha(self):
        owner_uid, _ = _make_user("ship-own@t.com", "ship_own")
        _, admin_token = _make_user("ship-adm@t.com", "ship_adm", admin=True)
        item_id = _seed_item(owner_uid, title="Shippable")
        r = _post_form(
            f"/admin/feedback/{item_id}/ship", admin_token,
            data={"sha": "abc1234"},
        )
        self.assertEqual(r.status_code, 302)
        with db.conn() as c:
            row = c.execute(
                "SELECT status, shipped_commit_sha FROM feedback_items WHERE id = ?",
                (item_id,),
            ).fetchone()
        self.assertEqual(row["status"], "shipped")
        self.assertEqual(row["shipped_commit_sha"], "abc1234")


# ── Comments ─────────────────────────────────────────────────────────────────


class TestComments(_Base):
    def test_user_comment_renders_on_detail(self):
        uid, token = _make_user("com-u@t.com", "com_u")
        item_id = _seed_item(uid, title="Has comments")
        r = _post_form(f"/api/feedback/{item_id}/comment", token, data={"body": "Follow-up note"})
        self.assertEqual(r.status_code, 302)
        r2 = client.get(f"/feedback/{item_id}", cookies={server.COOKIE_NAME: token}, follow_redirects=False)
        self.assertIn("Follow-up note", r2.text)

    def test_admin_comment_notifies_submitter(self):
        owner_uid, _ = _make_user("com-own@t.com", "com_own")
        _, admin_token = _make_user("com-adm@t.com", "com_adm", admin=True)
        item_id = _seed_item(owner_uid, title="Admin responds")
        _post_form(
            f"/admin/feedback/{item_id}/comment", admin_token,
            data={"body": "Thanks — queued for next sprint."},
        )
        with db.conn() as c:
            notif = c.execute(
                "SELECT title FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                (owner_uid,),
            ).fetchone()
        self.assertIsNotNone(notif)
        self.assertIn("replied", notif["title"].lower())


# ── ENHANCEMENT tests ────────────────────────────────────────────────────────


class TestRateLimit(_Base):
    """#1 — 10 submissions / hour / user cap."""

    def setUp(self):
        super().setUp()
        os.environ.pop("FEEDBACK_RATELIMIT_DISABLED", None)
        # Reset the server's in-memory rate-limit buckets so the tests
        # don't bleed counters across runs.
        try:
            import server
            getattr(server, "_rate_limit_buckets", {}).clear()
        except Exception:
            pass

    def tearDown(self):
        # Re-enable the bypass so sibling test files that seed lots of
        # submissions (smoke/load) keep working.
        os.environ["FEEDBACK_RATELIMIT_DISABLED"] = "1"
        super().tearDown()

    def test_11th_submission_in_an_hour_returns_429(self):
        uid, token = _make_user("rl@t.com", "rl_user")
        for i in range(10):
            r = _post_form(
                "/api/feedback", token,
                data={"type": "bug", "title": f"Rpt {i}", "body": "Body"},
                accept_json=True,
            )
            self.assertEqual(r.status_code, 200, f"submission #{i + 1} should be allowed")
        r11 = _post_form(
            "/api/feedback", token,
            data={"type": "bug", "title": "One too many", "body": "Body"},
            accept_json=True,
        )
        self.assertEqual(r11.status_code, 429)


class TestSelfVoteBlocked(_Base):
    """#2 — authors can't upvote their own feedback."""

    def test_self_vote_returns_400(self):
        uid, token = _make_user("self-vote@t.com", "self_vote")
        item_id = _seed_item(uid, title="My own feedback")
        r = _post_form(f"/api/feedback/{item_id}/vote", token, data={}, accept_json=True)
        self.assertEqual(r.status_code, 400)
        # The global error handler wraps HTTPException.detail into
        # {"error": "...", "message": "..."}. Accept either shape so
        # this test survives a handler refactor either direction.
        body = r.json()
        msg = (body.get("message") or body.get("detail") or "").lower()
        self.assertIn("your own", msg)
        # Verify the upvote counter didn't move.
        with db.conn() as c:
            row = c.execute("SELECT upvotes FROM feedback_items WHERE id = ?", (item_id,)).fetchone()
        self.assertEqual(row["upvotes"], 0)

    def test_other_user_can_still_vote(self):
        author_uid, _ = _make_user("sv-auth@t.com", "sv_auth")
        _, voter_token = _make_user("sv-voter@t.com", "sv_voter")
        item_id = _seed_item(author_uid, title="Cross-user vote")
        r = _post_form(f"/api/feedback/{item_id}/vote", voter_token, data={}, accept_json=True)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["voted"])


class TestMineFilter(_Base):
    """#3 — /feedback?mine=1 scopes to the current user's submissions."""

    def test_mine_filter_shows_only_own(self):
        owner_uid, owner_token = _make_user("mine-own@t.com", "mine_own")
        other_uid, _ = _make_user("mine-other@t.com", "mine_other")
        _seed_item(owner_uid, title="MineA")
        _seed_item(other_uid, title="NotMine")
        r = client.get(
            "/feedback?mine=1",
            cookies={server.COOKIE_NAME: owner_token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("MineA", r.text)
        self.assertNotIn("NotMine", r.text)

    def test_mine_filter_includes_private_own(self):
        """The owner's private posts are visible under ?mine=1 even
        though they're hidden from the default /feedback listing."""
        owner_uid, owner_token = _make_user("mine-priv@t.com", "mine_priv")
        _seed_item(owner_uid, title="MyPrivateOne", is_public=0)
        r = client.get(
            "/feedback?mine=1",
            cookies={server.COOKIE_NAME: owner_token},
            follow_redirects=False,
        )
        self.assertIn("MyPrivateOne", r.text)


class TestEngagementOnFeedback(_Base):
    """#4 — submit + vote fire engagement events."""

    def test_submit_logs_feedback_submit_event(self):
        uid, token = _make_user("eng-sub@t.com", "eng_sub")
        _post_form(
            "/api/feedback", token,
            data={"type": "feature", "title": "Make it faster", "body": "Details"},
            accept_json=True,
        )
        with db.conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM engagement_events "
                "WHERE user_id = ? AND event_type = 'feedback_submit'",
                (uid,),
            ).fetchone()
        self.assertGreaterEqual(row["n"], 1)

    def test_vote_logs_feedback_vote_event(self):
        author_uid, _ = _make_user("eng-va@t.com", "eng_va")
        voter_uid, voter_token = _make_user("eng-vv@t.com", "eng_vv")
        item_id = _seed_item(author_uid, title="Will be voted")
        _post_form(f"/api/feedback/{item_id}/vote", voter_token, data={}, accept_json=True)
        with db.conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM engagement_events "
                "WHERE user_id = ? AND event_type = 'feedback_vote'",
                (voter_uid,),
            ).fetchone()
        self.assertGreaterEqual(row["n"], 1)

    def test_unvote_does_not_log_event(self):
        """Un-voting isn't meaningful signal — only the add direction
        should be recorded."""
        author_uid, _ = _make_user("eng-uva@t.com", "eng_uva")
        voter_uid, voter_token = _make_user("eng-uvv@t.com", "eng_uvv")
        item_id = _seed_item(author_uid, title="Toggle vote")
        _post_form(f"/api/feedback/{item_id}/vote", voter_token, data={}, accept_json=True)
        _post_form(f"/api/feedback/{item_id}/vote", voter_token, data={}, accept_json=True)
        with db.conn() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM engagement_events "
                "WHERE user_id = ? AND event_type = 'feedback_vote'",
                (voter_uid,),
            ).fetchone()["n"]
        self.assertEqual(n, 1)


class TestSimilarSearch(_Base):
    """#5 — /api/feedback/search returns 0-3 matching public items."""

    def test_short_query_returns_empty(self):
        _, token = _make_user("ss-short@t.com", "ss_short")
        r = client.get("/api/feedback/search?q=ab", cookies={server.COOKIE_NAME: token}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["items"], [])

    def test_title_match_returns_items(self):
        owner_uid, _ = _make_user("ss-own@t.com", "ss_own")
        _, viewer_token = _make_user("ss-v@t.com", "ss_v")
        _seed_item(owner_uid, title="Dark mode toggle please")
        _seed_item(owner_uid, title="Login screen dark")
        _seed_item(owner_uid, title="Unrelated telegram thing")
        r = client.get(
            "/api/feedback/search?q=dark",
            cookies={server.COOKIE_NAME: viewer_token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        titles = [i["title"] for i in r.json()["items"]]
        self.assertTrue(any("dark" in t.lower() for t in titles), titles)
        self.assertFalse(any("telegram" in t.lower() for t in titles))

    def test_private_items_excluded(self):
        owner_uid, _ = _make_user("ss-priv-o@t.com", "ss_priv_o")
        _, viewer_token = _make_user("ss-priv-v@t.com", "ss_priv_v")
        _seed_item(owner_uid, title="Hidden secret thing", is_public=0)
        r = client.get(
            "/api/feedback/search?q=hidden",
            cookies={server.COOKIE_NAME: viewer_token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["items"], [])


class TestAdminBulkStatus(_Base):
    """#6 — bulk status change applies to N selected items + notifies."""

    def _post_urlencoded(self, path: str, token: str, pairs: list[tuple[str, str]]):
        """Explicit x-www-form-urlencoded body so the CSRF middleware can
        parse ``_csrf`` out of it. httpx's ``data=list[tuple]`` path
        sometimes upgrades to multipart which the middleware won't read.
        """
        from urllib.parse import urlencode
        csrf = _prime_csrf(token)
        body = urlencode(pairs + [("_csrf", csrf)])
        return client.post(
            path,
            content=body,
            cookies={server.COOKIE_NAME: token, "_csrf": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )

    def test_bulk_marks_multiple_shipped(self):
        owner_uid, _ = _make_user("bulk-own@t.com", "bulk_own")
        _, admin_token = _make_user("bulk-adm@t.com", "bulk_adm", admin=True)
        a = _seed_item(owner_uid, title="Bulk A")
        b = _seed_item(owner_uid, title="Bulk B")
        c_id = _seed_item(owner_uid, title="Bulk C")
        r = self._post_urlencoded(
            "/admin/feedback/bulk-status", admin_token,
            [("ids", str(a)), ("ids", str(b)), ("status", "shipped")],
        )
        self.assertEqual(r.status_code, 302)
        with db.conn() as c:
            statuses = {
                row["id"]: row["status"]
                for row in c.execute("SELECT id, status FROM feedback_items").fetchall()
            }
        self.assertEqual(statuses[a], "shipped")
        self.assertEqual(statuses[b], "shipped")
        self.assertEqual(statuses[c_id], "open", "unchecked row should stay untouched")

    def test_bulk_notifies_submitter_per_item(self):
        owner_uid, _ = _make_user("bulk-notif-own@t.com", "bulk_notif_own")
        _, admin_token = _make_user("bulk-notif-adm@t.com", "bulk_notif_adm", admin=True)
        a = _seed_item(owner_uid, title="Will notify A")
        b = _seed_item(owner_uid, title="Will notify B")
        self._post_urlencoded(
            "/admin/feedback/bulk-status", admin_token,
            [("ids", str(a)), ("ids", str(b)), ("status", "declined")],
        )
        with db.conn() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM notifications "
                "WHERE user_id = ? AND type = 'feedback'",
                (owner_uid,),
            ).fetchone()["n"]
        self.assertEqual(n, 2, "one notification per shipped item")

    def test_bulk_empty_selection_is_noop(self):
        _, admin_token = _make_user("bulk-empty@t.com", "bulk_empty", admin=True)
        csrf = _prime_csrf(admin_token)
        r = client.post(
            "/admin/feedback/bulk-status",
            data={"status": "shipped", "_csrf": csrf},
            cookies={server.COOKIE_NAME: admin_token, "_csrf": csrf},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)

    def test_bulk_non_admin_rejected(self):
        _, user_token = _make_user("bulk-reg@t.com", "bulk_reg")
        r = self._post_urlencoded(
            "/admin/feedback/bulk-status", user_token,
            [("ids", "1"), ("status", "shipped")],
        )
        self.assertEqual(r.status_code, 403)


class TestFeedbackDigest(_Base):
    """#7 — monthly digest queues payloads for submitters + voters."""

    def test_dry_run_picks_up_shipped_items_and_recipients(self):
        from jobs.feedback_digest import compute_feedback_digest_sync
        owner_uid, _ = _make_user("dig-own@t.com", "dig_own")
        voter_uid, _ = _make_user("dig-vote@t.com", "dig_vote")
        nobody_uid, _ = _make_user("dig-nada@t.com", "dig_nada", sub=False)

        shipped_id = _seed_item(owner_uid, title="Shipped last week")
        other_id = _seed_item(owner_uid, title="Still open")
        # Mark shipped + update updated_at to 'now' so the 30d window catches it.
        with db.conn() as c:
            c.execute(
                "UPDATE feedback_items SET status = 'shipped', updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (shipped_id,),
            )
            # Voter cast their vote on the shipped item.
            c.execute(
                "INSERT INTO feedback_votes (user_id, feedback_id) VALUES (?, ?)",
                (voter_uid, shipped_id),
            )

        out = compute_feedback_digest_sync(dry_run=True)
        self.assertEqual(out["shipped"], 1)
        # Submitter + voter should both appear; free-tier user shouldn't.
        recipient_ids = {r["user_id"] for r in out["recipients"]}
        self.assertIn(owner_uid, recipient_ids)
        self.assertIn(voter_uid, recipient_ids)
        self.assertNotIn(nobody_uid, recipient_ids)

    def test_no_shipped_items_returns_empty(self):
        from jobs.feedback_digest import compute_feedback_digest_sync
        _make_user("dig-empty@t.com", "dig_empty")
        out = compute_feedback_digest_sync(dry_run=True)
        self.assertEqual(out["shipped"], 0)
        self.assertEqual(out["queued"], 0)
        self.assertEqual(out["recipients"], [])


if __name__ == "__main__":
    unittest.main()

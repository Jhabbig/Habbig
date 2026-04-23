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


if __name__ == "__main__":
    unittest.main()

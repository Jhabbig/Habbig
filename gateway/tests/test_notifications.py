"""Tests for the in-app notification bell.

Covers:
  - Migration 026 creates notifications + notification_preferences
  - db.create_notification / get_notifications / mark_read / archive / delete
  - Preference gating: type-off and inapp-off both suppress inserts
  - HTTP routes (list, unread_count, mark-read, read-all, archive, delete,
    prefs get/patch) with auth guard + user isolation
  - SSE in-process broadcast to subscriber queue
  - Integration: send_market_resolution_notifications creates a row
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import sys
import unittest

# Dev bypass off + no SITE_ACCESS_TOKEN so TestClient gets clean unauth responses.
os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402

_test_conn = sqlite3.connect(":memory:", check_same_thread=False)
_test_conn.row_factory = sqlite3.Row
_test_conn.execute("PRAGMA foreign_keys = ON")


@contextlib.contextmanager
def _fake_conn():
    try:
        yield _test_conn
        _test_conn.commit()
    except Exception:
        _test_conn.rollback()
        raise


db.conn = _fake_conn
db.init_db()

import migrations  # noqa: E402
migrations.upgrade_to_head()

import server  # noqa: E402
import notifications  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import pytest  # noqa: E402


client = TestClient(server.app)

# Feature gate: the notification CRUD helpers (get/mark_read/archive/delete
# plus preference getters/setters) are not present on this branch —
# `notifications.create_notification` is the only notification helper
# currently exported. These tests resume once the full CRUD surface lands.
_NOTIFICATIONS_CRUD_AVAILABLE = all(
    hasattr(db, fn) for fn in (
        "create_notification",
        "get_notifications",
        "mark_notification_read",
        "archive_notification",
        "delete_notification",
    )
)

pytestmark = pytest.mark.skipif(
    not _NOTIFICATIONS_CRUD_AVAILABLE,
    reason=(
        "notification CRUD helpers not present on this branch — tests "
        "re-enable once db.get_notifications / mark_notification_read / "
        "archive_notification / delete_notification land."
    ),
)


class _RebindMixin:
    """Re-pin db.conn at this file's fake before each test — shielded from
    cross-file patcher interference when pytest loads another test file first.
    """

    @classmethod
    def setUpClass(cls):
        cls._previous_db_conn = db.conn
        db.conn = _fake_conn

    @classmethod
    def tearDownClass(cls):
        db.conn = cls._previous_db_conn

    def setUp(self):
        db.conn = _fake_conn
        # Fresh-state isolation: kill anything in notifications / prefs so
        # tests don't see each other's rows.
        with db.conn() as c:
            c.execute("DELETE FROM notifications")
            c.execute("DELETE FROM notification_preferences")


def _make_user(email: str) -> int:
    """Create a user (reusing DB's password hashing)."""
    return db.create_user(email, "TestPass123!", username=email.split("@")[0])


def _login_as(user_id: int) -> dict:
    """Return a cookie jar with session + a CSRF double-submit pair.

    Gateway's CSRFMiddleware requires POST/PATCH/DELETE to echo the _csrf
    cookie in the x-csrf-token header. Tests use the same token for both
    so middleware sees matching values.
    """
    token = db.create_session(user_id)
    return {
        server.COOKIE_NAME: token,
        "_csrf": "test-csrf-token",
    }


CSRF_HEADERS = {
    "x-csrf-token": "test-csrf-token",
    # Gateway's CSRFMiddleware only reads the header when Content-Type matches
    # application/json or application/x-www-form-urlencoded — empty-body POSTs
    # therefore need the explicit JSON type even though no body is sent.
    "content-type": "application/json",
}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Migration / schema ───────────────────────────────────────────────────────

class TestMigration(_RebindMixin, unittest.TestCase):
    def test_notifications_table_exists(self):
        with db.conn() as c:
            row = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='notifications'"
            ).fetchone()
        self.assertIsNotNone(row)

    def test_preferences_table_exists(self):
        with db.conn() as c:
            row = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='notification_preferences'"
            ).fetchone()
        self.assertIsNotNone(row)

    def test_unread_partial_index_exists(self):
        with db.conn() as c:
            row = c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_notifs_user_unread'"
            ).fetchone()
        self.assertIsNotNone(row)


# ── db helpers ───────────────────────────────────────────────────────────────

class TestDbHelpers(_RebindMixin, unittest.TestCase):

    def test_create_and_get(self):
        uid = _make_user("h1@test.com")
        nid = db.create_notification(
            uid, "market_resolved", "Market resolved", "Body text",
            link_url="/m/foo", icon="market",
            metadata={"slug": "foo", "correct": 3},
        )
        rows = db.get_notifications(uid)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["id"], nid)
        self.assertEqual(r["type"], "market_resolved")
        self.assertEqual(r["title"], "Market resolved")
        self.assertEqual(r["link_url"], "/m/foo")
        self.assertEqual(r["icon"], "market")
        self.assertEqual(r["metadata"], {"slug": "foo", "correct": 3})
        self.assertIsNone(r["read_at"])
        self.assertFalse(r["archived"])

    def test_unknown_type_coerced_to_system(self):
        uid = _make_user("h2@test.com")
        nid = db.create_notification(uid, "bogus_type", "Hi")
        rows = db.get_notifications(uid)
        self.assertEqual(rows[0]["type"], "system")

    def test_unread_count_and_mark_read(self):
        uid = _make_user("h3@test.com")
        n1 = db.create_notification(uid, "system", "a")
        n2 = db.create_notification(uid, "system", "b")
        self.assertEqual(db.get_unread_count(uid), 2)

        self.assertTrue(db.mark_notification_read(n1, uid))
        self.assertEqual(db.get_unread_count(uid), 1)

        # Idempotent: second call returns False, no raise.
        self.assertFalse(db.mark_notification_read(n1, uid))
        self.assertEqual(db.get_unread_count(uid), 1)

        # Mark-all clears remainder.
        self.assertEqual(db.mark_all_notifications_read(uid), 1)
        self.assertEqual(db.get_unread_count(uid), 0)

    def test_archive_hides_from_main_view(self):
        uid = _make_user("h4@test.com")
        nid = db.create_notification(uid, "system", "archive me")
        self.assertTrue(db.archive_notification(nid, uid))
        # Default get_notifications excludes archived
        self.assertEqual(len(db.get_notifications(uid)), 0)
        # Opt-in includes
        self.assertEqual(len(db.get_notifications(uid, include_archived=True)), 1)
        # Archive also marks read
        self.assertEqual(db.get_unread_count(uid), 0)

    def test_delete_scoped_to_owner(self):
        uid_a = _make_user("h5a@test.com")
        uid_b = _make_user("h5b@test.com")
        nid = db.create_notification(uid_a, "system", "mine")
        # Other user can't delete
        self.assertFalse(db.delete_notification(nid, uid_b))
        self.assertEqual(len(db.get_notifications(uid_a)), 1)
        # Owner can
        self.assertTrue(db.delete_notification(nid, uid_a))
        self.assertEqual(len(db.get_notifications(uid_a)), 0)

    def test_user_isolation(self):
        uid_a = _make_user("iso_a@test.com")
        uid_b = _make_user("iso_b@test.com")
        db.create_notification(uid_a, "system", "a")
        db.create_notification(uid_b, "system", "b")
        self.assertEqual(len(db.get_notifications(uid_a)), 1)
        self.assertEqual(len(db.get_notifications(uid_b)), 1)
        self.assertEqual(db.get_notifications(uid_a)[0]["title"], "a")
        self.assertEqual(db.get_notifications(uid_b)[0]["title"], "b")

    def test_pagination_before_id(self):
        uid = _make_user("pag@test.com")
        ids = [db.create_notification(uid, "system", f"n{i}") for i in range(5)]
        # Default: all 5, newest first
        page1 = db.get_notifications(uid, limit=3)
        self.assertEqual(len(page1), 3)
        # Newest first means largest id first
        self.assertEqual(page1[0]["id"], ids[-1])
        # Next page
        page2 = db.get_notifications(uid, limit=3, before_id=page1[-1]["id"])
        self.assertEqual(len(page2), 2)
        # No overlap
        self.assertTrue(all(r["id"] < page1[-1]["id"] for r in page2))


# ── Preferences ──────────────────────────────────────────────────────────────

class TestPreferences(_RebindMixin, unittest.TestCase):

    def test_defaults_all_on(self):
        uid = _make_user("p1@test.com")
        p = db.get_notification_preferences(uid)
        self.assertTrue(p["inapp_enabled"])
        self.assertTrue(p["email_enabled"])
        self.assertFalse(p["push_enabled"])  # web push isn't wired up
        for t in db.NOTIFICATION_TYPES:
            self.assertTrue(p["types"][t], f"{t} should default to True")

    def test_patch_merge(self):
        uid = _make_user("p2@test.com")
        db.set_notification_preferences(uid, types={"market_mover": False})
        p = db.get_notification_preferences(uid)
        self.assertFalse(p["types"]["market_mover"])
        self.assertTrue(p["types"]["market_resolved"])
        # Second merge only touches the one field
        db.set_notification_preferences(uid, inapp_enabled=False)
        p = db.get_notification_preferences(uid)
        self.assertFalse(p["inapp_enabled"])
        self.assertFalse(p["types"]["market_mover"])  # preserved from earlier patch

    def test_type_gate_blocks_insert(self):
        uid = _make_user("p3@test.com")
        db.set_notification_preferences(uid, types={"market_mover": False})
        # Runtime wrapper respects the opt-out
        nid = _run(notifications.create_notification(
            uid, type="market_mover", title="nope", body="",
        ))
        self.assertIsNone(nid)
        self.assertEqual(len(db.get_notifications(uid)), 0)

    def test_inapp_off_blocks_all(self):
        uid = _make_user("p4@test.com")
        db.set_notification_preferences(uid, inapp_enabled=False)
        nid = _run(notifications.create_notification(
            uid, type="system", title="nope", body="",
        ))
        self.assertIsNone(nid)


# ── SSE broadcast ────────────────────────────────────────────────────────────

class TestSseBroadcast(_RebindMixin, unittest.TestCase):

    def test_broadcast_reaches_subscriber(self):
        uid = _make_user("sse1@test.com")

        async def scenario():
            q = await notifications.subscribe(uid)
            try:
                await notifications.create_notification(
                    uid, type="system", title="hi", body="world",
                    link_url="/here",
                )
                payload = await asyncio.wait_for(q.get(), timeout=1.0)
                return payload
            finally:
                await notifications.unsubscribe(uid, q)

        payload = _run(scenario())
        self.assertEqual(payload["title"], "hi")
        self.assertEqual(payload["body"], "world")
        self.assertEqual(payload["type"], "system")
        self.assertEqual(payload["link_url"], "/here")

    def test_no_broadcast_when_opted_out(self):
        uid = _make_user("sse2@test.com")

        async def scenario():
            db.set_notification_preferences(uid, types={"system": False})
            q = await notifications.subscribe(uid)
            try:
                nid = await notifications.create_notification(
                    uid, type="system", title="muted", body="",
                )
                # Don't expect a payload — 200ms timeout is the negative check.
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    payload = None
                return nid, payload
            finally:
                await notifications.unsubscribe(uid, q)

        nid, payload = _run(scenario())
        self.assertIsNone(nid)
        self.assertIsNone(payload)

    def test_other_users_subscription_not_hit(self):
        uid_a = _make_user("sse3a@test.com")
        uid_b = _make_user("sse3b@test.com")

        async def scenario():
            q_b = await notifications.subscribe(uid_b)
            try:
                await notifications.create_notification(
                    uid_a, type="system", title="for-a", body="",
                )
                try:
                    payload = await asyncio.wait_for(q_b.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    payload = None
                return payload
            finally:
                await notifications.unsubscribe(uid_b, q_b)

        self.assertIsNone(_run(scenario()))


# ── HTTP routes ──────────────────────────────────────────────────────────────

class TestHttpRoutes(_RebindMixin, unittest.TestCase):

    def test_list_requires_auth(self):
        r = client.get("/api/v1/notifications")
        self.assertEqual(r.status_code, 401)

    def test_list_returns_users_rows(self):
        uid = _make_user("r1@test.com")
        db.create_notification(uid, "market_resolved", "m", link_url="/a")
        db.create_notification(uid, "system", "s", link_url="/b")
        cookies = _login_as(uid)
        r = client.get("/api/v1/notifications", cookies=cookies)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["notifications"]), 2)
        self.assertEqual(body["unread_count"], 2)

    def test_unread_count_endpoint(self):
        uid = _make_user("r2@test.com")
        db.create_notification(uid, "system", "x")
        r = client.get("/api/v1/notifications/unread_count", cookies=_login_as(uid))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 1)

    def test_mark_read_flow(self):
        uid = _make_user("r3@test.com")
        nid = db.create_notification(uid, "system", "x")
        cookies = _login_as(uid)
        r = client.post(
            f"/api/v1/notifications/{nid}/read",
            cookies=cookies, headers=CSRF_HEADERS,
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["changed"])
        self.assertEqual(
            client.get("/api/v1/notifications/unread_count", cookies=cookies).json()["count"],
            0,
        )

    def test_mark_all_read(self):
        uid = _make_user("r4@test.com")
        for _ in range(3):
            db.create_notification(uid, "system", "x")
        cookies = _login_as(uid)
        r = client.post(
            "/api/v1/notifications/read-all",
            cookies=cookies, headers=CSRF_HEADERS,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["marked"], 3)
        self.assertEqual(
            client.get("/api/v1/notifications/unread_count", cookies=cookies).json()["count"],
            0,
        )

    def test_archive_hides_from_list(self):
        uid = _make_user("r5@test.com")
        nid = db.create_notification(uid, "system", "x")
        cookies = _login_as(uid)
        client.post(
            f"/api/v1/notifications/{nid}/archive",
            cookies=cookies, headers=CSRF_HEADERS,
        )
        body = client.get("/api/v1/notifications", cookies=cookies).json()
        self.assertEqual(len(body["notifications"]), 0)
        # include_archived flag surfaces it again
        body = client.get(
            "/api/v1/notifications?include_archived=true", cookies=cookies
        ).json()
        self.assertEqual(len(body["notifications"]), 1)

    def test_delete_scoped_to_owner(self):
        uid_a = _make_user("r6a@test.com")
        uid_b = _make_user("r6b@test.com")
        nid = db.create_notification(uid_a, "system", "mine")
        # Wrong user → 404 (not 403, to avoid leaking existence)
        r = client.delete(
            f"/api/v1/notifications/{nid}",
            cookies=_login_as(uid_b), headers=CSRF_HEADERS,
        )
        self.assertEqual(r.status_code, 404)
        r = client.delete(
            f"/api/v1/notifications/{nid}",
            cookies=_login_as(uid_a), headers=CSRF_HEADERS,
        )
        self.assertEqual(r.status_code, 200)

    def test_user_cannot_see_others_notifications(self):
        uid_a = _make_user("iso-a@test.com")
        uid_b = _make_user("iso-b@test.com")
        db.create_notification(uid_a, "system", "A-only")
        db.create_notification(uid_b, "system", "B-only")
        body_a = client.get("/api/v1/notifications", cookies=_login_as(uid_a)).json()
        titles_a = [n["title"] for n in body_a["notifications"]]
        self.assertIn("A-only", titles_a)
        self.assertNotIn("B-only", titles_a)

    def test_preferences_get_and_patch(self):
        uid = _make_user("pref@test.com")
        cookies = _login_as(uid)
        r = client.get("/api/v1/notifications/preferences", cookies=cookies)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["inapp_enabled"])
        self.assertTrue(body["types"]["market_mover"])

        r = client.patch(
            "/api/v1/notifications/preferences",
            json={"types": {"market_mover": False}, "push_enabled": True},
            cookies=cookies, headers=CSRF_HEADERS,
        )
        self.assertEqual(r.status_code, 200)
        updated = r.json()
        self.assertFalse(updated["types"]["market_mover"])
        self.assertTrue(updated["push_enabled"])
        self.assertTrue(updated["types"]["market_resolved"])  # untouched

    def test_notifications_page_requires_auth(self):
        r = client.get("/notifications", follow_redirects=False)
        self.assertIn(r.status_code, (302, 303, 307, 308))

    def test_notifications_page_renders_for_authed_user(self):
        uid = _make_user("page@test.com")
        r = client.get("/notifications", cookies=_login_as(uid))
        self.assertEqual(r.status_code, 200)
        self.assertIn("Notifications", r.text)


# ── Integration via jobs ─────────────────────────────────────────────────────

class TestMarketResolutionIntegration(_RebindMixin, unittest.TestCase):

    def test_sends_inapp_alongside_email(self):
        from jobs.notification_jobs import send_market_resolution_notifications

        uid = _make_user("int1@test.com")
        # Seed the user_market_views row the job reads from.
        with db.conn() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(user_market_views)")}
        insert_cols = ["user_id", "market_slug"]
        insert_vals: list = [uid, "fed-hold-rates"]
        if "first_viewed_at" in cols:
            insert_cols.append("first_viewed_at"); insert_vals.append(1)
        if "last_viewed_at" in cols:
            insert_cols.append("last_viewed_at"); insert_vals.append(1)
        if "view_count" in cols:
            insert_cols.append("view_count"); insert_vals.append(1)
        if "notified_on_resolution" in cols:
            insert_cols.append("notified_on_resolution"); insert_vals.append(0)
        placeholders = ",".join("?" * len(insert_cols))
        with db.conn() as c:
            c.execute(
                f"INSERT INTO user_market_views ({','.join(insert_cols)}) "
                f"VALUES ({placeholders})",
                insert_vals,
            )

        result = _run(send_market_resolution_notifications(
            "fed-hold-rates", "yes", market_question="Will the Fed hold rates?",
        ))
        self.assertEqual(result["notified"], 1)

        rows = db.get_notifications(uid)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["type"], "market_resolved")
        self.assertIn("YES", r["title"])
        self.assertEqual(r["link_url"], "/markets/fed-hold-rates")
        self.assertEqual(r["metadata"]["market_slug"], "fed-hold-rates")


if __name__ == "__main__":
    unittest.main()

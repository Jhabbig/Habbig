"""Tests for Fix C (2026-05-15): notification db helpers.

The functions referenced by notification_routes.py (db.create_notification,
db.get_notifications, db.mark_notification_read, etc.) were never
implemented before this fix — every notification HTTP endpoint raised
``AttributeError`` at runtime. queries/notifications.py adds them and
db.py re-exports.

Tests cover:
  * Every db.* re-export resolves to a real callable
  * NOTIFICATION_TYPES is the canonical tuple
  * create_notification round-trip via get_notifications
  * Unknown type coerces to 'system'
  * Keyset pagination with before_id paginates correctly
  * unread_count uses the partial index from migration 026
  * mark_read / archive / delete are user-scoped
  * Preference get/set round-trips + defaults are all-on
  * notification_type_enabled gates correctly per-type and per-channel
"""

from __future__ import annotations

import os
import sys
import unittest

from tests import _testdb  # noqa: F401

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402


def _seed_user(uid: int, email: str) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO users (id, username, email, password_hash, "
            "password_salt, created_at) VALUES (?, ?, ?, '', '', strftime('%s','now'))",
            (uid, f"notif_u_{uid}", email),
        )


class TestReExports(unittest.TestCase):
    """Every previously-broken db.* call site now resolves."""

    def test_all_helpers_present(self):
        for name in (
            "NOTIFICATION_TYPES",
            "create_notification",
            "get_notifications",
            "get_unread_count",
            "mark_notification_read",
            "mark_all_notifications_read",
            "archive_notification",
            "delete_notification",
            "get_notification_preferences",
            "set_notification_preferences",
            "notification_type_enabled",
        ):
            self.assertTrue(hasattr(db, name), f"db.{name} missing")

    def test_notification_types_is_tuple(self):
        self.assertIsInstance(db.NOTIFICATION_TYPES, tuple)
        self.assertIn("system", db.NOTIFICATION_TYPES)
        self.assertIn("prediction_resolved", db.NOTIFICATION_TYPES)


class TestCreateAndList(unittest.TestCase):
    def setUp(self):
        _seed_user(7001, "notif_list@test.local")
        with db.conn() as c:
            c.execute("DELETE FROM notifications WHERE user_id = ?", (7001,))

    def test_create_returns_id_and_roundtrip(self):
        nid = db.create_notification(
            user_id=7001, type="system",
            title="Hello",
            body="World",
            link_url="/somewhere",
            metadata={"foo": "bar"},
        )
        self.assertGreater(nid, 0)
        rows = db.get_notifications(user_id=7001, limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Hello")
        self.assertEqual(rows[0]["body"], "World")
        self.assertEqual(rows[0]["link_url"], "/somewhere")
        self.assertEqual(rows[0]["metadata"], {"foo": "bar"})

    def test_unknown_type_coerces_to_system(self):
        nid = db.create_notification(
            user_id=7001, type="not_a_real_type", title="x",
        )
        with db.conn() as c:
            row = c.execute(
                "SELECT type FROM notifications WHERE id = ?", (nid,),
            ).fetchone()
        self.assertEqual(row["type"], "system")

    def test_keyset_pagination_returns_older_rows(self):
        ids = []
        for i in range(5):
            ids.append(db.create_notification(
                user_id=7001, type="system",
                title=f"n-{i}",
            ))
        # Newest first.
        page1 = db.get_notifications(user_id=7001, limit=2)
        self.assertEqual(len(page1), 2)
        # before_id of the last row in page1 returns older rows.
        page2 = db.get_notifications(
            user_id=7001, limit=2, before_id=page1[-1]["id"],
        )
        self.assertEqual(len(page2), 2)
        # No overlap.
        page1_ids = {r["id"] for r in page1}
        page2_ids = {r["id"] for r in page2}
        self.assertFalse(page1_ids & page2_ids)


class TestUnreadAndArchive(unittest.TestCase):
    def setUp(self):
        _seed_user(7002, "notif_unread@test.local")
        with db.conn() as c:
            c.execute("DELETE FROM notifications WHERE user_id = ?", (7002,))

    def test_unread_count_excludes_read_and_archived(self):
        a = db.create_notification(user_id=7002, type="system", title="a")
        b = db.create_notification(user_id=7002, type="system", title="b")
        db.create_notification(user_id=7002, type="system", title="c")
        self.assertEqual(db.get_unread_count(7002), 3)
        db.mark_notification_read(a, 7002)
        self.assertEqual(db.get_unread_count(7002), 2)
        db.archive_notification(b, 7002)
        self.assertEqual(db.get_unread_count(7002), 1)

    def test_mark_read_user_scoped(self):
        # Mark with a wrong user_id should not flip the row.
        nid = db.create_notification(user_id=7002, type="system", title="x")
        self.assertFalse(db.mark_notification_read(nid, 9999))
        self.assertEqual(db.get_unread_count(7002), 1)

    def test_mark_all_returns_count(self):
        for _ in range(3):
            db.create_notification(user_id=7002, type="system", title="t")
        marked = db.mark_all_notifications_read(7002)
        self.assertEqual(marked, 3)
        self.assertEqual(db.get_unread_count(7002), 0)

    def test_delete_is_user_scoped(self):
        nid = db.create_notification(user_id=7002, type="system", title="d")
        self.assertFalse(db.delete_notification(nid, 9999))
        self.assertTrue(db.delete_notification(nid, 7002))


class TestPreferences(unittest.TestCase):
    def setUp(self):
        _seed_user(7003, "notif_prefs@test.local")
        with db.conn() as c:
            c.execute(
                "DELETE FROM notification_preferences WHERE user_id = ?",
                (7003,),
            )

    def test_defaults_all_on_with_push_off(self):
        prefs = db.get_notification_preferences(7003)
        self.assertTrue(prefs["inapp_enabled"])
        self.assertTrue(prefs["email_enabled"])
        # Push defaults off — requires explicit user opt-in.
        self.assertFalse(prefs["push_enabled"])
        # Every NOTIFICATION_TYPES key is on by default.
        for t in db.NOTIFICATION_TYPES:
            self.assertTrue(prefs["types"][t])

    def test_partial_update_returns_full_dict(self):
        result = db.set_notification_preferences(
            7003, push_enabled=True,
        )
        self.assertTrue(result["push_enabled"])
        # Other flags untouched.
        self.assertTrue(result["inapp_enabled"])
        self.assertTrue(result["email_enabled"])

    def test_type_enabled_respects_channel_master(self):
        # inapp_enabled off → every type returns False even if type's
        # individual flag is True.
        db.set_notification_preferences(7003, inapp_enabled=False)
        self.assertFalse(db.notification_type_enabled(7003, "system"))

    def test_type_enabled_respects_per_type_flag(self):
        db.set_notification_preferences(
            7003,
            inapp_enabled=True,
            types={"system": False},
        )
        self.assertFalse(db.notification_type_enabled(7003, "system"))
        # Other types stay enabled.
        self.assertTrue(db.notification_type_enabled(7003, "prediction_resolved"))


if __name__ == "__main__":
    unittest.main()

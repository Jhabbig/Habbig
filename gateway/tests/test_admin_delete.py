"""Tests for admin user deletion — single (`/admin/users/{id}/delete`) and
bulk (`/admin/users/bulk` with `bulk_action=delete`).

Regression cover for the GDPR Art. 17 audit finding: both handlers used to
hand-roll a 3-table DELETE (sessions/subscriptions/users) and leaked rows
in every other user-scoped table (analytics_events, gifts, etc.). The fix
routes both paths through ``db.cascade_delete_user`` which walks
``sqlite_master`` and deletes every row in every table with a ``user_id``
column.
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from urllib.parse import urlencode

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import server  # noqa: E402


def _suffix() -> str:
    return f"{os.getpid()}_{int(time.time() * 1000) & 0xFFFFFF}"


def _create_super_admin() -> tuple[int, str]:
    email = f"admdelete_{_suffix()}@test.local"
    uid = db.create_user(
        email, "Password1!verylong", username=f"admdelete_{_suffix()}"
    )
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
    return uid, token


def _create_regular_user() -> int:
    email = f"victim_{_suffix()}@test.local"
    return db.create_user(
        email, "Password1!verylong", username=f"victim_{_suffix()}"
    )


def _seed_user_rows(user_id: int) -> dict:
    """Seed at least one row per user-scoped table we want to witness."""
    from queries import admin as admin_q
    admin_q.record_analytics_event(
        event_type="test_event",
        user_id=user_id,
        session_id=f"sess_{user_id}",
        page="/test",
        referrer=None,
        ip_hash=f"iphash_{user_id}",
        user_agent_category="test",
        properties={"k": "v"},
    )
    return _count_user_rows(user_id)


def _count_user_rows(user_id: int) -> dict:
    """{table: count} for every table with a ``user_id`` column that
    currently holds rows for ``user_id``."""
    counts: dict = {}
    with db.conn() as c:
        tables = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for t in tables:
            name = t["name"]
            if name == "users":
                continue
            try:
                cols = [r["name"] for r in c.execute(
                    f"PRAGMA table_info({name})"
                ).fetchall()]
            except Exception:
                continue
            if "user_id" not in cols:
                continue
            try:
                row = c.execute(
                    f"SELECT COUNT(*) AS n FROM {name} WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
            except Exception:
                continue
            if row and row["n"]:
                counts[name] = row["n"]
    return counts


def _prime_csrf(client: TestClient, session_token: str) -> str:
    client.get(
        "/admin/users",
        cookies={server.COOKIE_NAME: session_token},
        follow_redirects=False,
    )
    return client.cookies.get(server.CSRF_COOKIE_NAME) or ""


class AdminSingleDeleteCascadeTestCase(unittest.TestCase):
    """`/admin/users/{user_id}/delete` must wipe every user-scoped row."""

    @classmethod
    def setUpClass(cls):
        try:
            import migrations as _migrations
            _migrations.upgrade_to_head()
        except Exception:
            pass
        cls.client = TestClient(server.app)
        cls.admin_id, cls.admin_token = _create_super_admin()

    def test_delete_user_wipes_user_scoped_tables(self):
        victim_id = _create_regular_user()
        pre = _seed_user_rows(victim_id)
        self.assertIn("analytics_events", pre)

        # Seed an analytics row for the admin as a control — it must
        # survive the cascade.
        from queries import admin as admin_q
        admin_q.record_analytics_event(
            event_type="admin_event",
            user_id=self.admin_id,
            session_id="adm_sess",
            page="/admin",
            referrer=None,
            ip_hash="adm_iphash",
            user_agent_category="test",
            properties={},
        )
        admin_pre = _count_user_rows(self.admin_id)

        csrf = _prime_csrf(self.client, self.admin_token)
        self.assertTrue(csrf)
        body = urlencode([(server.CSRF_FORM_FIELD, csrf)])
        r = self.client.post(
            f"/admin/users/{victim_id}/delete",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            cookies={
                server.COOKIE_NAME: self.admin_token,
                server.CSRF_COOKIE_NAME: csrf,
            },
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303), r.text[:200])

        post = _count_user_rows(victim_id)
        self.assertEqual(post, {}, f"orphan rows: {post}")
        self.assertIsNone(db.get_user_by_id(victim_id))

        admin_post = _count_user_rows(self.admin_id)
        for table, n in admin_pre.items():
            self.assertGreaterEqual(admin_post.get(table, 0), n)


class AdminBulkDeleteCascadeTestCase(unittest.TestCase):
    """`/admin/users/bulk` with bulk_action=delete cascades for each user."""

    @classmethod
    def setUpClass(cls):
        try:
            import migrations as _migrations
            _migrations.upgrade_to_head()
        except Exception:
            pass
        cls.client = TestClient(server.app)
        cls.admin_id, cls.admin_token = _create_super_admin()

    def test_bulk_delete_wipes_user_scoped_tables_for_each(self):
        victim_ids = [_create_regular_user() for _ in range(3)]
        for vid in victim_ids:
            pre = _seed_user_rows(vid)
            self.assertIn("analytics_events", pre)

        csrf = _prime_csrf(self.client, self.admin_token)
        fields = [
            (server.CSRF_FORM_FIELD, csrf),
            ("bulk_action", "delete"),
        ]
        for vid in victim_ids:
            fields.append(("user_ids", str(vid)))
        body = urlencode(fields)

        r = self.client.post(
            "/admin/users/bulk",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            cookies={
                server.COOKIE_NAME: self.admin_token,
                server.CSRF_COOKIE_NAME: csrf,
            },
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303), r.text[:200])

        for vid in victim_ids:
            post = _count_user_rows(vid)
            self.assertEqual(post, {}, f"victim {vid} orphans: {post}")
            self.assertIsNone(db.get_user_by_id(vid))


if __name__ == "__main__":
    unittest.main()

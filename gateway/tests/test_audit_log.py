"""Tests for security/audit.py — explicit action logging with graceful failure.

Uses the same in-memory DB pattern as test_http_auth.py.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import unittest

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402

_conn = sqlite3.connect(":memory:", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA foreign_keys = ON")


@contextlib.contextmanager
def _fake_conn():
    try:
        yield _conn
        _conn.commit()
    except Exception:
        _conn.rollback()
        raise


db.conn = _fake_conn
db.init_db()
import migrations  # noqa: E402
migrations.upgrade_to_head()

from security import audit  # noqa: E402


class _FakeRequest:
    """Minimal Request stand-in with the fields audit._get_* helpers read."""

    def __init__(self, ip="5.6.7.8", ua="pytest/1.0", req_id="req-xyz"):
        class _C:
            host = ip
        self.client = _C()
        self.headers = {
            "user-agent": ua,
            "x-request-id": req_id,
            "x-forwarded-for": ip,
        }


class TestLogAction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_id = db.create_user(
            "logaction_admin@test.com", "TestPass123!",
            username="logadmin", is_admin=True,
        )

    def test_persists_request_metadata(self):
        req = _FakeRequest(ip="1.1.1.1", ua="curl/8", req_id="req-1")
        audit.log_action(
            admin_user_id=self.admin_id,
            admin_email="logaction_admin@test.com",
            action=audit.AuditAction.USER_SUSPEND,
            target_type="user",
            target_id=99,
            target_description="victim@example.com",
            before={"suspended": 0},
            after={"suspended": 1},
            request=req,
            notes="manual",
        )
        rows, total = db.query_audit_log(action=audit.AuditAction.USER_SUSPEND)
        self.assertGreaterEqual(total, 1)
        row = rows[0]
        self.assertEqual(row["admin_email"], "logaction_admin@test.com")
        self.assertEqual(row["target_description"], "victim@example.com")
        self.assertEqual(row["ip_address"], "1.1.1.1")
        self.assertEqual(row["user_agent"], "curl/8")
        self.assertEqual(row["request_id"], "req-1")
        self.assertIn("suspended", row["before_state"])
        self.assertIn("suspended", row["after_state"])

    def test_never_raises_on_db_failure(self):
        """A broken db.insert_audit_log must not propagate exceptions."""
        original = db.insert_audit_log
        def boom(*args, **kwargs):
            raise RuntimeError("simulated DB failure")
        db.insert_audit_log = boom
        try:
            # Should return silently, not raise
            audit.log_action(
                admin_user_id=self.admin_id,
                admin_email="x@y.com",
                action=audit.AuditAction.TOKEN_GENERATE,
            )
        finally:
            db.insert_audit_log = original

    def test_snapshot_user_returns_dict(self):
        snap = audit.snapshot_user(self.admin_id)
        self.assertIsNotNone(snap)
        self.assertEqual(snap["id"], self.admin_id)
        self.assertEqual(snap["email"], "logaction_admin@test.com")
        self.assertTrue(snap["is_admin"] > 0)

    def test_snapshot_user_missing(self):
        self.assertIsNone(audit.snapshot_user(999999))


class TestFilterKwargs(unittest.TestCase):
    def test_parse_filters(self):
        class QP:
            def __init__(self, d): self._d = d
            def get(self, k, default=None): return self._d.get(k, default)
            def items(self): return self._d.items()
        qp = QP({
            "action": "user.suspend",
            "admin_id": "5",
            "target_type": "user",
            "from": "2026-01-01",
            "to": "2026-12-31",
        })
        kwargs = audit.filter_to_query_kwargs(qp)
        self.assertEqual(kwargs["action"], "user.suspend")
        self.assertEqual(kwargs["admin_user_id"], 5)
        self.assertEqual(kwargs["target_type"], "user")
        self.assertIn("from_ts", kwargs)
        self.assertIn("to_ts", kwargs)
        # to_ts should be end-of-day
        self.assertGreater(kwargs["to_ts"], kwargs["from_ts"])

    def test_empty_query_yields_empty_kwargs(self):
        class QP:
            def get(self, k, default=None): return None
            def items(self): return []
        kwargs = audit.filter_to_query_kwargs(QP())
        self.assertEqual(kwargs, {})


if __name__ == "__main__":
    unittest.main()

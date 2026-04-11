"""DB-layer tests for migration 006 and 2FA helpers.

Follows the same in-memory sqlite pattern as test_http_auth.py: monkey-patch
db.conn BEFORE any import of server so nothing touches auth.db on disk.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import unittest

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


# Apply our patch at module load so init_db / migrations populate _test_conn,
# then save our context manager so each TestCase can re-apply it. Other test
# modules in the same pytest session monkey-patch db.conn for themselves;
# without the per-class re-bind below, whichever module loads LAST wins and
# every other module's helpers start talking to the wrong in-memory DB.
db.conn = _fake_conn
db.init_db()

import migrations  # noqa: E402
migrations.upgrade_to_head()


def _rebind_db_conn_for_class(cls):
    """Use as a base class or via subclassing to keep db.conn pinned at this file's fake."""
    return cls


class _RebindMixin:
    """Re-applies this file's db.conn patch in setUp so cross-test pollution can't bite."""
    @classmethod
    def setUpClass(cls):
        cls._previous_db_conn = db.conn
        db.conn = _fake_conn

    @classmethod
    def tearDownClass(cls):
        db.conn = cls._previous_db_conn

    def setUp(self):
        # Per-test re-pin so individual tests' DB ops always hit our conn,
        # even if a fixture in a different test file flipped db.conn between
        # methods. Subclasses that override setUp must call super().setUp().
        db.conn = _fake_conn


class TestMigration006Schema(_RebindMixin, unittest.TestCase):
    """Migration 006 adds columns to users/sessions and creates 3 new tables."""

    def test_users_has_2fa_columns(self):
        cols = {r["name"] for r in _test_conn.execute("PRAGMA table_info(users)")}
        for col in (
            "totp_enabled",
            "totp_secret",
            "totp_setup_at",
            "email_otp_enabled",
            "two_fa_method",
            "two_fa_verified_at",
            "backup_codes",
            "backup_codes_generated_at",
        ):
            self.assertIn(col, cols, f"users missing column {col}")

    def test_sessions_has_2fa_columns(self):
        cols = {r["name"] for r in _test_conn.execute("PRAGMA table_info(sessions)")}
        for col in (
            "two_fa_verified",
            "two_fa_verified_at",
            "pending_totp_secret",
            "pending_totp_secret_at",
        ):
            self.assertIn(col, cols, f"sessions missing column {col}")

    def test_new_tables_exist(self):
        tables = {
            r[0]
            for r in _test_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("two_fa_attempts", tables)
        self.assertIn("email_otps", tables)
        self.assertIn("audit_log", tables)

    def test_schema_version_006_applied(self):
        revs = [
            r["revision"]
            for r in _test_conn.execute("SELECT revision FROM schema_version ORDER BY revision")
        ]
        self.assertIn("006", revs)


class TestBackupCodeHelpers(_RebindMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import secrets
        email = f"backup_{secrets.token_hex(4)}@test.com"
        self.uid = db.create_user(email, "TestPass123!", username=f"backup{secrets.token_hex(3)}")

    def test_store_and_consume_roundtrip(self):
        from security import two_factor as tf
        plaintexts = tf.generate_backup_codes()
        hashed = tf.hash_backup_codes(plaintexts)
        db.store_backup_codes(self.uid, hashed)

        self.assertEqual(db.count_remaining_backup_codes(self.uid), 8)
        self.assertTrue(db.consume_backup_code(self.uid, plaintexts[0]))
        self.assertEqual(db.count_remaining_backup_codes(self.uid), 7)

    def test_used_code_cannot_be_reused(self):
        from security import two_factor as tf
        plaintexts = tf.generate_backup_codes()
        hashed = tf.hash_backup_codes(plaintexts)
        db.store_backup_codes(self.uid, hashed)

        self.assertTrue(db.consume_backup_code(self.uid, plaintexts[0]))
        self.assertFalse(db.consume_backup_code(self.uid, plaintexts[0]))

    def test_wrong_code_rejected(self):
        from security import two_factor as tf
        plaintexts = tf.generate_backup_codes()
        hashed = tf.hash_backup_codes(plaintexts)
        db.store_backup_codes(self.uid, hashed)
        self.assertFalse(db.consume_backup_code(self.uid, "ZZZZ-ZZZZ"))
        self.assertEqual(db.count_remaining_backup_codes(self.uid), 8)


class TestSession2FAFlag(_RebindMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import secrets
        email = f"sess_{secrets.token_hex(4)}@test.com"
        self.uid = db.create_user(email, "TestPass123!", username=f"sess{secrets.token_hex(3)}")

    def test_default_unverified_then_marked(self):
        token = db.create_session(self.uid)
        self.assertFalse(db.session_two_fa_verified(token))
        db.mark_session_two_fa_verified(token)
        self.assertTrue(db.session_two_fa_verified(token))

    def test_pending_totp_secret_roundtrip(self):
        token = db.create_session(self.uid)
        self.assertIsNone(db.get_pending_totp_secret(token))
        db.set_pending_totp_secret(token, "encrypted_fake_value")
        self.assertEqual(db.get_pending_totp_secret(token), "encrypted_fake_value")
        db.clear_pending_totp_secret(token)
        self.assertIsNone(db.get_pending_totp_secret(token))


class TestAuditLogDB(_RebindMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import secrets
        email = f"audit_{secrets.token_hex(4)}@test.com"
        self.uid = db.create_user(email, "TestPass123!", username=f"audit{secrets.token_hex(3)}")
        # Audit_log can be polluted by other test files in the same process
        # (e.g. test_audit_log.py rebinds db.conn at module load and inserts
        # rows that survive into our conn). Filter every assertion below by
        # admin_user_id == self.uid so the assertions stay deterministic.

    def test_insert_and_query(self):
        row_id = db.insert_audit_log(
            admin_user_id=self.uid,
            admin_email="admin@test.com",
            action="user.suspend",
            target_type="user",
            target_id=42,
            target_description="victim@test.com",
            before_state=json.dumps({"suspended": 0}),
            after_state=json.dumps({"suspended": 1}),
            ip_address="1.2.3.4",
            user_agent="pytest",
            request_id="abc",
        )
        self.assertGreater(row_id, 0)
        # Scope to our admin id so cross-file pollution can't bump the count.
        rows, total = db.query_audit_log(
            action="user.suspend", admin_user_id=self.uid
        )
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["action"], "user.suspend")
        self.assertEqual(rows[0]["before_state"], '{"suspended": 0}')
        self.assertEqual(rows[0]["after_state"], '{"suspended": 1}')

    def test_filters(self):
        db.insert_audit_log(
            admin_user_id=self.uid, admin_email="a@t.com",
            action="token.generate",
        )
        db.insert_audit_log(
            admin_user_id=self.uid, admin_email="a@t.com",
            action="token.revoke",
        )
        rows, total = db.query_audit_log(
            action="token.generate", admin_user_id=self.uid
        )
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["action"], "token.generate")
        rows, total = db.query_audit_log(admin_user_id=self.uid)
        self.assertGreaterEqual(total, 2)

    def test_csv_export(self):
        db.insert_audit_log(
            admin_user_id=self.uid, admin_email="csv@test.com",
            action="user.unsuspend", target_id=99,
        )
        csv_text = db.export_audit_log_csv(
            action="user.unsuspend", admin_user_id=self.uid
        )
        self.assertIn("timestamp_iso", csv_text)
        self.assertIn("user.unsuspend", csv_text)
        self.assertIn("csv@test.com", csv_text)


if __name__ == "__main__":
    unittest.main()

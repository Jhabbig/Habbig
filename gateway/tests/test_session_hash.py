"""Migration 191 — legacy `sessions` token hashed at rest.

Covers the audit fix for the HIGH-severity finding where the legacy
`sessions` table stored raw cookie tokens as the primary key (plaintext).
After the migration:
  * the `token` column is gone
  * a new `token_hash` column holds SHA-256(raw)
  * read paths in queries/auth.py re-hash before SELECT
  * pre-migration cookie shapes no longer validate (they would need
    a raw-token lookup, which the schema no longer supports)
"""

from __future__ import annotations

import hashlib
import os
import unittest

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("EMAIL_DRY_RUN", "true")

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations
import db  # noqa: E402


def _sha(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class TestSessionsStoredAsHashAtRest(unittest.TestCase):
    """Post-migration: only the SHA-256 hash is in the legacy table."""

    def test_create_session_stores_hash_not_raw(self):
        uid = db.create_user(
            "sess-hash-raw@test.com", "InitialPass123!", username="sesshashraw1"
        )
        raw = db.create_session(uid)
        with db.conn() as c:
            rows = c.execute(
                "SELECT token_hash FROM sessions WHERE user_id = ?", (uid,)
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["token_hash"], raw)
        self.assertEqual(rows[0]["token_hash"], _sha(raw))

    def test_sessions_table_has_no_raw_token_column(self):
        """The `token` column was dropped by migration 191."""
        with db.conn() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(sessions)")}
        self.assertNotIn(
            "token", cols,
            "sessions.token must not exist after migration 191",
        )
        self.assertIn("token_hash", cols)


class TestPostMigrationCookieValidates(unittest.TestCase):
    """The happy path — a freshly issued cookie round-trips correctly."""

    def test_new_cookie_validates(self):
        uid = db.create_user(
            "sess-hash-new@test.com", "InitialPass123!", username="sesshashnew1"
        )
        raw = db.create_session(uid)
        row = db.get_session(raw)
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], uid)

    def test_delete_session_invalidates_cookie(self):
        uid = db.create_user(
            "sess-hash-del@test.com", "InitialPass123!", username="sesshashdel1"
        )
        raw = db.create_session(uid)
        self.assertIsNotNone(db.get_session(raw))
        db.delete_session(raw)
        self.assertIsNone(db.get_session(raw))


class TestCsrfTokensReadableByRawCookie(unittest.TestCase):
    """The CSRF helpers also key on the raw cookie; verify they still
    behave correctly post-migration."""

    def test_set_then_get_csrf_through_raw_cookie(self):
        uid = db.create_user(
            "sess-hash-csrf@test.com", "InitialPass123!", username="sesshashcsrf1"
        )
        raw = db.create_session(uid)
        db.set_session_csrf(raw, "csrf-abc-123")
        got = db.get_session_csrf(raw)
        self.assertIsNotNone(got)
        self.assertEqual(got["csrf_token"], "csrf-abc-123")

        db.clear_session_csrf(raw)
        self.assertIsNone(db.get_session_csrf(raw))


if __name__ == "__main__":
    unittest.main()

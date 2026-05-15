"""Regression test for migration 193 — subscriptions.expires_at backfill.

AUDIT (MED-1, queries/billing) — see gateway/queries/subscriptions.py
and gateway/migrations/193_subscriptions_expires_at_backfill.py for
the underlying fix.

Asserted behaviour:

  * An active Stripe-sourced subscription row written with
    ``expires_at = NULL`` (the pre-fix bug state) is read as expired
    by ``has_active_subscription`` once the migration's backfill has
    run, because the row's ``expires_at`` lands at NOW and the
    closed-fail rule treats anything <= now as expired.
  * Manual gift rows (``source != 'stripe'``) keep their NULL
    ``expires_at`` — those are legitimate permanent grants and the
    backfill must NOT silently expire them.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

USES_TESTDB = True
from tests import _testdb  # noqa: E402,F401

import db  # noqa: E402
import migrations  # noqa: E402

_SHARED_DB_CONN = _testdb._fake_conn


def _pin_shared_db() -> None:
    db.conn = _SHARED_DB_CONN


def _force_revert_193(c) -> None:
    """Pretend migration 193 has not yet run on the in-memory DB so we
    can re-apply it inside the test and assert its effect.

    The shared ``_testdb`` bootstrap calls ``upgrade_to_head`` on every
    import, which means by the time this test runs, 193's backfill has
    already swept the table once. We delete the version row + null the
    columns we care about, then re-trigger the migration.
    """
    c.execute("DELETE FROM schema_version WHERE revision = '193'")


class TestExpiresAtBackfill(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _pin_shared_db()

    def setUp(self):
        _pin_shared_db()
        with db.conn() as c:
            c.execute("DELETE FROM subscriptions")

    def test_backfill_expires_legacy_null_stripe_row(self):
        """Stripe-sourced active row with NULL expires_at → after the
        backfill runs, has_active_subscription returns False because
        expires_at is stamped to NOW (<= now → expired)."""
        uid = db.create_user("legacy_null@t.com", "TestPass123!", username="legacy_null")
        now = int(time.time())
        with db.conn() as c:
            c.execute(
                "INSERT INTO subscriptions "
                "(user_id, dashboard_key, plan, status, started_at, "
                " expires_at, source) "
                "VALUES (?, ?, ?, 'active', ?, NULL, 'stripe')",
                (uid, "climate", "pro", now - 86400),
            )
            _force_revert_193(c)

        # Re-run the migration. 193's upgrade is idempotent and the
        # discovery loop will replay any revision missing from
        # schema_version.
        result = migrations.upgrade_to_head()
        self.assertGreaterEqual(result["applied"], 1)

        with db.conn() as c:
            row = c.execute(
                "SELECT expires_at FROM subscriptions "
                "WHERE user_id = ? AND dashboard_key = ?",
                (uid, "climate"),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["expires_at"], "backfill should populate NULL")
        self.assertLessEqual(int(row["expires_at"]), int(time.time()))

        # And the closed-fail rule now reads it as expired.
        self.assertFalse(db.has_active_subscription(uid, "climate"))

    def test_backfill_leaves_non_stripe_null_alone(self):
        """A gift / placeholder row with source != 'stripe' and NULL
        expires_at must keep its NULL — those represent permanent
        grants and the backfill should not silently expire them. The
        access check still fails closed (NULL → not active) but that's
        a property of the read path, not the migration."""
        uid = db.create_user("gift_null@t.com", "TestPass123!", username="gift_null")
        now = int(time.time())
        with db.conn() as c:
            c.execute(
                "INSERT INTO subscriptions "
                "(user_id, dashboard_key, plan, status, started_at, "
                " expires_at, source) "
                "VALUES (?, ?, ?, 'active', ?, NULL, 'placeholder')",
                (uid, "love", "pro", now),
            )
            _force_revert_193(c)

        migrations.upgrade_to_head()

        with db.conn() as c:
            row = c.execute(
                "SELECT expires_at FROM subscriptions "
                "WHERE user_id = ? AND dashboard_key = ?",
                (uid, "love"),
            ).fetchone()
        self.assertIsNone(
            row["expires_at"],
            "non-stripe rows must keep their NULL expires_at",
        )


if __name__ == "__main__":
    unittest.main()

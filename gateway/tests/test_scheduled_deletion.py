"""Tests for the 30-day soft-delete sweep in ``jobs.pipeline_jobs``.

Regression cover for the GDPR Art. 17 audit finding: the original
``process_scheduled_deletions`` hand-rolled a 7-table DELETE and missed
~50 other user-keyed tables (analytics_events, take_reports,
user_follows, newsletter audiences, watchlists, alerts, share_metrics,
every ``*_user_id`` variant column, etc.).

The fix routes the sweep through ``db.cascade_delete_user`` which walks
``sqlite_master`` and deletes every row in every (table, column) pair
matching ``user_id`` or ``*_user_id``. We also unlink data-export ZIPs
on disk so the deletion is GDPR-clean (the user's personal data extract
is itself personal data).

Tests pin:
  1. A user seeded across many tables (including variant-column tables)
     has zero rows in every user-keyed table after the sweep.
  2. ZIP files referenced from ``data_export_requests.file_path`` are
     unlinked from disk and the row is gone.
  3. Cancelled deletions are still skipped.
  4. Future-dated deletions are still skipped.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations
import db  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _suffix() -> str:
    return f"{time.time_ns() & 0xFFFFFF:x}"


def _create_user(prefix: str = "sched") -> int:
    s = _suffix()
    return db.create_user(
        f"{prefix}_{s}@test.local",
        "InitialPass123!verylong",
        username=f"{prefix}_{s}",
    )


def _schedule_for_past(user_id: int) -> None:
    past = int(time.time()) - 10
    with db.conn() as c:
        c.execute(
            "UPDATE users SET deletion_requested_at = ?, "
            "deletion_scheduled_for = ?, deletion_cancelled_at = NULL "
            "WHERE id = ?",
            (past, past, user_id),
        )


def _count_user_keyed_rows(user_id: int) -> dict[str, int]:
    """``{table.column: count}`` for every user-keyed row referencing
    ``user_id`` across the schema."""
    out: dict[str, int] = {}
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
                cols = c.execute(f"PRAGMA table_info({name})").fetchall()
            except Exception:
                continue
            for col in cols:
                col_name = col["name"]
                col_type = (col["type"] or "").upper()
                if "INT" not in col_type:
                    continue
                if col_name != "user_id" and not col_name.endswith("_user_id"):
                    continue
                try:
                    n = c.execute(
                        f"SELECT COUNT(*) AS n FROM {name} "
                        f"WHERE {col_name} = ?",
                        (user_id,),
                    ).fetchone()["n"]
                except Exception:
                    continue
                if n:
                    key = name if col_name == "user_id" else f"{name}.{col_name}"
                    out[key] = n
    return out


class ProcessScheduledDeletionsCascadeTestCase(unittest.TestCase):
    """The 30-day sweep cascades across every user-keyed table."""

    def setUp(self):
        try:
            import migrations as _migrations
            _migrations.upgrade_to_head()
        except Exception:
            pass

    def test_sweep_clears_canonical_and_variant_columns(self):
        """Seed rows across canonical and variant column tables, schedule
        the user for past-due deletion, run the sweep, assert no
        user-keyed rows survive."""
        actor = _create_user("victim")
        target = _create_user("witness")

        from queries import admin as admin_q
        admin_q.record_analytics_event(
            event_type="sweep_test",
            user_id=actor,
            session_id=f"sess_{actor}",
            page="/test",
            referrer=None,
            ip_hash=f"iphash_{actor}",
            user_agent_category="test",
            properties={"k": "v"},
        )

        now = int(time.time())
        with db.conn() as c:
            try:
                c.execute(
                    "INSERT INTO audit_log "
                    "(timestamp, admin_user_id, admin_email, action, "
                    "target_type, target_id, target_description, "
                    "ip_address, request_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (now, actor, "x@test.local", "test_sweep",
                     "user", str(target), "ut", "127.0.0.1", "rid"),
                )
            except Exception:
                pass
            try:
                c.execute(
                    "INSERT INTO collections "
                    "(owner_user_id, slug, title, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (actor, f"slug_{actor}", "T", now, now),
                )
            except Exception:
                pass
            try:
                c.execute(
                    "INSERT INTO user_follows "
                    "(follower_user_id, followed_user_id) "
                    "VALUES (?, ?)",
                    (actor, target),
                )
            except Exception:
                pass
            try:
                c.execute(
                    "INSERT INTO referrals "
                    "(referrer_user_id, referred_user_id, created_at) "
                    "VALUES (?, ?, ?)",
                    (actor, target, now),
                )
            except Exception:
                pass
            try:
                c.execute(
                    "INSERT INTO impersonation_sessions "
                    "(admin_user_id, target_user_id, cookie_token, "
                    "reason, started_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (actor, target, f"tok_{actor}", "test", now),
                )
            except Exception:
                pass

        pre = _count_user_keyed_rows(actor)
        self.assertGreater(
            len(pre), 1,
            f"Test setup didn't seed enough rows to exercise the cascade; "
            f"got {pre}"
        )
        self.assertIn("analytics_events", pre)

        _schedule_for_past(actor)
        from jobs.pipeline_jobs import process_scheduled_deletions
        result = _run(process_scheduled_deletions())
        self.assertGreaterEqual(result["deleted"], 1)
        self.assertIn("files_removed", result)

        post = _count_user_keyed_rows(actor)
        self.assertEqual(
            post, {},
            f"User-keyed rows survived the sweep for actor={actor}: {post}"
        )

    def test_sweep_unlinks_data_export_zips(self):
        """Data export ZIPs on disk must be unlinked. Missing files are
        tolerated."""
        uid = _create_user("zip")
        f1 = tempfile.NamedTemporaryFile(
            delete=False, suffix=".zip", prefix=f"export_{uid}_"
        )
        f1.write(b"PK\x03\x04 fake zip body")
        f1.close()
        f2 = tempfile.NamedTemporaryFile(
            delete=False, suffix=".zip", prefix=f"export_{uid}_"
        )
        f2.write(b"PK\x03\x04 fake zip body 2")
        f2.close()
        missing = os.path.join(
            tempfile.gettempdir(),
            f"export_{uid}_does_not_exist.zip",
        )

        now = int(time.time())
        with db.conn() as c:
            c.execute(
                "INSERT INTO data_export_requests "
                "(user_id, requested_at, status, file_path, "
                "completed_at, expires_at) "
                "VALUES (?, ?, 'ready', ?, ?, ?)",
                (uid, now, f1.name, now, now + 86400),
            )
            c.execute(
                "INSERT INTO data_export_requests "
                "(user_id, requested_at, status, file_path, "
                "completed_at, expires_at) "
                "VALUES (?, ?, 'ready', ?, ?, ?)",
                (uid, now, f2.name, now, now + 86400),
            )
            c.execute(
                "INSERT INTO data_export_requests "
                "(user_id, requested_at, status, file_path, "
                "completed_at, expires_at) "
                "VALUES (?, ?, 'ready', ?, ?, ?)",
                (uid, now, missing, now, now + 86400),
            )

        self.assertTrue(os.path.exists(f1.name))
        self.assertTrue(os.path.exists(f2.name))
        self.assertFalse(os.path.exists(missing))

        _schedule_for_past(uid)
        from jobs.pipeline_jobs import process_scheduled_deletions
        result = _run(process_scheduled_deletions())
        self.assertGreaterEqual(result["deleted"], 1)
        self.assertGreaterEqual(result["files_removed"], 2)

        self.assertFalse(
            os.path.exists(f1.name),
            f"Export ZIP {f1.name} should have been unlinked"
        )
        self.assertFalse(
            os.path.exists(f2.name),
            f"Export ZIP {f2.name} should have been unlinked"
        )

        with db.conn() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM data_export_requests "
                "WHERE user_id = ?", (uid,),
            ).fetchone()["n"]
        self.assertEqual(n, 0)

    def test_sweep_skips_cancelled_deletions(self):
        """``deletion_cancelled_at`` set → not swept."""
        uid = _create_user("cancelled")
        past = int(time.time()) - 10
        now = int(time.time())
        with db.conn() as c:
            c.execute(
                "UPDATE users SET deletion_requested_at = ?, "
                "deletion_scheduled_for = ?, "
                "deletion_cancelled_at = ? WHERE id = ?",
                (past, past, now, uid),
            )
        from jobs.pipeline_jobs import process_scheduled_deletions
        _run(process_scheduled_deletions())
        with db.conn() as c:
            row = c.execute(
                "SELECT id FROM users WHERE id = ?", (uid,),
            ).fetchone()
        self.assertIsNotNone(
            row,
            "Cancelled-deletion user was swept despite "
            "deletion_cancelled_at being set."
        )

    def test_sweep_skips_future_scheduled(self):
        """Future ``deletion_scheduled_for`` → not swept."""
        uid = _create_user("future")
        future = int(time.time()) + 30 * 86400
        now = int(time.time())
        with db.conn() as c:
            c.execute(
                "UPDATE users SET deletion_requested_at = ?, "
                "deletion_scheduled_for = ?, "
                "deletion_cancelled_at = NULL WHERE id = ?",
                (now, future, uid),
            )
        from jobs.pipeline_jobs import process_scheduled_deletions
        _run(process_scheduled_deletions())
        with db.conn() as c:
            row = c.execute(
                "SELECT id FROM users WHERE id = ?", (uid,),
            ).fetchone()
        self.assertIsNotNone(
            row,
            "Future-scheduled user was swept early."
        )

    def test_sweep_returns_zero_when_no_users_due(self):
        """Empty result returns ``{deleted: 0, checked: 0, files_removed: 0}``."""
        from jobs.pipeline_jobs import process_scheduled_deletions
        with db.conn() as c:
            c.execute(
                "UPDATE users SET deletion_scheduled_for = NULL "
                "WHERE deletion_scheduled_for IS NOT NULL"
            )
        result = _run(process_scheduled_deletions())
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["files_removed"], 0)

    def test_cascade_failure_rolls_back_cleanly(self):
        """If ``cascade_delete_user`` raises mid-flight, the user row and
        every user-keyed row stays intact (cascade's own txn rolls back)
        and the export ZIPs are NOT unlinked from disk — the next sweep
        retries the user end-to-end.

        Audit #14 HIGH #5: explicit rollback contract. We mock
        ``db.cascade_delete_user`` to delete a few user-keyed rows and
        then raise, simulating a partial-cascade crash. The test pins:

          (a) The user row still exists (no orphaned half-anonymisation).
          (b) Any rows the mock managed to write are reverted (the mock
              uses ``with db.conn() as c:`` so its writes commit only if
              the function returns normally — raising mid-block triggers
              ROLLBACK).
          (c) Export ZIPs on disk were NOT unlinked (the loop is after
              the cascade call, so it never runs).
          (d) The sweep does not crash the whole job — other users in
              the batch still process.
        """
        from unittest import mock as _mock

        # Three users:
        #   - ``victim`` is the one whose cascade we'll crash.
        #   - ``other`` is queued behind to verify the loop continues.
        #   - ``bystander`` is NOT scheduled for deletion; we seed the
        #     victim's follow row pointing at the bystander so that
        #     ``other``'s successful cascade can't accidentally
        #     mask-clean the victim's row through the ``followed_user_id``
        #     column.
        victim = _create_user("rb_victim")
        other = _create_user("rb_other")
        bystander = _create_user("rb_bystand")

        # Seed a user-keyed row for the victim so we can assert it
        # survives the rollback. follower=victim, followed=bystander
        # keeps the row alive even after ``other``'s cascade.
        now = int(time.time())
        with db.conn() as c:
            try:
                c.execute(
                    "INSERT INTO user_follows "
                    "(follower_user_id, followed_user_id) "
                    "VALUES (?, ?)",
                    (victim, bystander),
                )
            except Exception:
                pass

        # Stage an export ZIP on disk for the victim — must NOT be
        # unlinked when cascade fails.
        f = tempfile.NamedTemporaryFile(
            delete=False, suffix=".zip", prefix=f"rb_export_{victim}_"
        )
        f.write(b"PK\x03\x04 should survive")
        f.close()
        with db.conn() as c:
            c.execute(
                "INSERT INTO data_export_requests "
                "(user_id, requested_at, status, file_path, "
                "completed_at, expires_at) "
                "VALUES (?, ?, 'ready', ?, ?, ?)",
                (victim, now, f.name, now, now + 86400),
            )

        # Schedule both users for past-due deletion.
        _schedule_for_past(victim)
        _schedule_for_past(other)

        # Sanity: pre-state has rows + file on disk.
        pre = _count_user_keyed_rows(victim)
        self.assertGreater(
            len(pre), 0,
            "Setup didn't seed any user-keyed rows for the victim."
        )
        self.assertTrue(os.path.exists(f.name))

        # Patch cascade to write 3 rows (mimicking a partial cascade)
        # then raise. Because the mock uses ``with db.conn() as c:``,
        # its writes are inside a transaction that rolls back when the
        # block exits with an exception — so post-condition: zero rows
        # changed. This pins the contract: callers can rely on cascade
        # being atomic even on partial failure.
        call_count = {"n": 0}
        real_cascade = db.cascade_delete_user

        def _fake_cascade(user_id):
            call_count["n"] += 1
            if user_id == victim:
                # Simulate a partial cascade: open the connection, do
                # some DELETEs, then raise. The ``with`` block must
                # roll back the partial DELETEs.
                with db.conn() as c:
                    # Delete from 3 user-keyed tables (the "after 3
                    # tables" in the audit test spec). Each one is a
                    # real DELETE that would normally commit at block
                    # exit.
                    for tbl in ("user_follows", "user_topics", "sessions"):
                        try:
                            c.execute(
                                f"DELETE FROM {tbl} WHERE user_id = ? "
                                "OR follower_user_id = ?",
                                (user_id, user_id),
                            )
                        except Exception:
                            # Try the canonical user_id column alone
                            try:
                                c.execute(
                                    f"DELETE FROM {tbl} WHERE user_id = ?",
                                    (user_id,),
                                )
                            except Exception:
                                pass
                    raise RuntimeError(
                        "simulated cascade crash after 3 tables"
                    )
            # ``other`` cascades normally so we can verify the sweep
            # loop didn't bail on the first failure.
            return real_cascade(user_id)

        from jobs.pipeline_jobs import process_scheduled_deletions
        with _mock.patch.object(db, "cascade_delete_user", _fake_cascade):
            result = _run(process_scheduled_deletions())

        # (a) The victim user row still exists — cascade rolled back.
        with db.conn() as c:
            row = c.execute(
                "SELECT id FROM users WHERE id = ?", (victim,),
            ).fetchone()
        self.assertIsNotNone(
            row,
            "Victim user was deleted despite cascade raising "
            "mid-flight — rollback contract violated."
        )

        # (b) Any rows the partial cascade managed to DELETE are back.
        post = _count_user_keyed_rows(victim)
        self.assertEqual(
            post, pre,
            f"Partial-cascade DELETEs were not rolled back. "
            f"Pre={pre} Post={post}"
        )

        # (c) The export ZIP is still on disk. Unlink only runs after
        # a successful cascade.
        self.assertTrue(
            os.path.exists(f.name),
            f"Export ZIP {f.name} was unlinked despite cascade raising "
            "— unlink must be gated on cascade success."
        )

        # (d) The OTHER user processed normally — sweep didn't abort.
        with db.conn() as c:
            other_row = c.execute(
                "SELECT id FROM users WHERE id = ?", (other,),
            ).fetchone()
        self.assertIsNone(
            other_row,
            "Cascade failure on victim aborted processing of subsequent "
            "users — per-user try/except contract broken."
        )

        # The job's accounting reflects exactly 1 success (the other),
        # the victim was checked but not deleted.
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["deleted"], 1)

        # Cleanup — unlink the leftover ZIP ourselves.
        try:
            os.unlink(f.name)
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()

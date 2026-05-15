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


if __name__ == "__main__":
    unittest.main()

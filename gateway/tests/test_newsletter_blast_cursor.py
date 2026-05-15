"""Tests for Fix D (2026-05-15): newsletter blast cursor + atomic claim.

Covers:
  * Migration 194 adds last_recipient_id + claim_token columns
  * claim_blast_job is atomic — concurrent calls with different tokens
    can NOT both win the same row
  * Cursor pagination: get_blast_recipients_after uses id > last_id and
    is stable across mutations of newsletter_subscribers between ticks
  * advance_blast_job_progress_with_cursor bumps cursor + processed +
    releases the claim atomically
  * advance_blast_job_progress_with_cursor with stale claim no-ops
  * Reclaim after CLAIM_TTL_SECONDS works for a crashed worker
"""

from __future__ import annotations

import os
import sys
import time
import unittest

from tests import _testdb  # noqa: F401

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
from queries import newsletter as nq  # noqa: E402


def _suffix() -> str:
    return f"{os.getpid()}_cur"


def _seed_confirmed(email: str) -> int:
    """Insert a confirmed, never-unsubscribed subscriber row."""
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO newsletter_subscribers "
            "(email, subscribed_at, source, segment, frequency, "
            " confirmed_at, unsubscribed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (email.lower(), now, "test", "all", "weekly", now),
        )
        return int(cur.lastrowid)


def _make_campaign_and_job(total: int) -> tuple[int, int]:
    """Create a fake campaign + a pending blast job for the test to claim."""
    now = int(time.time())
    with db.conn() as c:
        cur_camp = c.execute(
            "INSERT INTO newsletter_campaigns "
            "(admin_user_id, subject, body_md, segment, frequency_filter, "
            " scheduled_at, sent_at, recipient_count, created_at) "
            "VALUES (1, 'Test', 'Hello', 'all', NULL, ?, NULL, ?, ?)",
            (now, total, now),
        )
        campaign_id = int(cur_camp.lastrowid)
    job_id = db.create_blast_job(campaign_id=campaign_id, total_recipients=total)
    return campaign_id, job_id


class TestMigration194(unittest.TestCase):
    """The migration adds the new columns + index."""

    def test_columns_exist(self):
        with db.conn() as c:
            cols = {row["name"] for row in c.execute(
                "PRAGMA table_info(newsletter_blast_jobs)",
            )}
        self.assertIn("last_recipient_id", cols)
        self.assertIn("claim_token", cols)

    def test_default_cursor_is_zero(self):
        # Existing rows + new inserts default to last_recipient_id=0,
        # so the very first tick reads from id > 0 = every row.
        _campaign_id, job_id = _make_campaign_and_job(total=3)
        try:
            job = db.get_blast_job(job_id)
            with db.conn() as c:
                row = c.execute(
                    "SELECT COALESCE(last_recipient_id, 0) AS cur "
                    "FROM newsletter_blast_jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
            self.assertEqual(int(row["cur"]), 0)
        finally:
            with db.conn() as c:
                c.execute(
                    "DELETE FROM newsletter_blast_jobs WHERE id = ?",
                    (job_id,),
                )


class TestAtomicClaim(unittest.TestCase):
    """claim_blast_job is single-winner under concurrent calls."""

    def setUp(self):
        # Fresh state per test.
        with db.conn() as c:
            c.execute("DELETE FROM newsletter_blast_jobs")
            c.execute("DELETE FROM newsletter_campaigns")

    def test_concurrent_claims_only_one_wins(self):
        _campaign_id, _job_id = _make_campaign_and_job(total=5)

        first = db.claim_blast_job(claim_token="worker-A")
        second = db.claim_blast_job(claim_token="worker-B")
        self.assertIsNotNone(first)
        # Worker A holds the only pending job; worker B sees an empty
        # claim (the row is now status=running with worker A's token).
        self.assertIsNone(second)

    def test_same_worker_can_reclaim_its_own_row(self):
        _campaign_id, _job_id = _make_campaign_and_job(total=5)
        first = db.claim_blast_job(claim_token="worker-A")
        self.assertIsNotNone(first)
        # A second call from the SAME token re-fetches the same row —
        # idempotent so a tick that crashes mid-cycle can resume.
        same = db.claim_blast_job(claim_token="worker-A")
        self.assertIsNotNone(same)
        self.assertEqual(first["id"], same["id"])

    def test_reclaim_after_ttl_grace(self):
        # Set started_at to long ago so the row counts as abandoned.
        _campaign_id, job_id = _make_campaign_and_job(total=5)
        long_ago = int(time.time()) - nq.CLAIM_TTL_SECONDS - 10
        with db.conn() as c:
            c.execute(
                "UPDATE newsletter_blast_jobs "
                "SET status = 'running', started_at = ?, claim_token = ? "
                "WHERE id = ?",
                (long_ago, "crashed-worker", job_id),
            )
        # New worker with a different token reclaims after TTL.
        claimed = db.claim_blast_job(claim_token="fresh-worker")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], job_id)


class TestCursorPagination(unittest.TestCase):
    """get_blast_recipients_after is stable across mutations."""

    def setUp(self):
        with db.conn() as c:
            c.execute(
                "DELETE FROM newsletter_subscribers WHERE email LIKE ?",
                (f"%_{_suffix()}_cur@example.com",),
            )

    def test_pages_by_id_not_offset(self):
        ids = []
        for i in range(5):
            ids.append(
                _seed_confirmed(f"a{i:02d}_{_suffix()}_cur@example.com")
            )

        # First page: every row above id 0.
        page1 = db.get_blast_recipients_after(
            segment="all", frequency_filter=None,
            last_id=0, limit=3,
        )
        self.assertEqual(len(page1), 3)
        self.assertEqual(
            [r["id"] for r in page1],
            sorted(ids)[:3],
        )

        # Second page: only above the last id from page 1.
        page2 = db.get_blast_recipients_after(
            segment="all", frequency_filter=None,
            last_id=page1[-1]["id"], limit=3,
        )
        self.assertEqual(len(page2), 2)
        self.assertEqual(
            [r["id"] for r in page2],
            sorted(ids)[3:],
        )

    def test_unsubscribe_between_pages_does_not_shift_cursor(self):
        # Seed 4 rows, page through, unsubscribe between pages — the
        # cursor (last_id) is stable so we don't re-send or skip.
        ids = [
            _seed_confirmed(f"u{i:02d}_{_suffix()}_cur@example.com")
            for i in range(4)
        ]
        # First page returns ids[0..1].
        page1 = db.get_blast_recipients_after(
            segment="all", frequency_filter=None,
            last_id=0, limit=2,
        )
        self.assertEqual(len(page1), 2)
        last_id = page1[-1]["id"]

        # Mass-unsubscribe ids[0] AFTER it was already enqueued.
        with db.conn() as c:
            c.execute(
                "UPDATE newsletter_subscribers SET unsubscribed_at = ? "
                "WHERE id = ?",
                (int(time.time()), ids[0]),
            )

        # Second page reads strictly above last_id — never re-sends ids[1].
        page2 = db.get_blast_recipients_after(
            segment="all", frequency_filter=None,
            last_id=last_id, limit=2,
        )
        page2_ids = [r["id"] for r in page2]
        self.assertNotIn(page1[0]["id"], page2_ids)
        self.assertNotIn(page1[-1]["id"], page2_ids)


class TestCursorAdvance(unittest.TestCase):
    """advance_blast_job_progress_with_cursor bumps + releases claim atomically."""

    def setUp(self):
        with db.conn() as c:
            c.execute("DELETE FROM newsletter_blast_jobs")
            c.execute("DELETE FROM newsletter_campaigns")

    def test_advance_bumps_processed_and_cursor(self):
        _campaign_id, job_id = _make_campaign_and_job(total=10)
        claimed = db.claim_blast_job(claim_token="worker-X")
        self.assertIsNotNone(claimed)

        # Bump processed + cursor.
        result = db.advance_blast_job_progress_with_cursor(
            job_id,
            batch_size=3,
            last_recipient_id=42,
            claim_token="worker-X",
        )
        self.assertEqual(int(result["processed_recipients"]), 3)
        self.assertEqual(int(result["last_recipient_id"]), 42)

        # Status is still 'running' (3 < 10).
        self.assertEqual(result["status"], "running")

    def test_advance_flips_to_done_at_total(self):
        _campaign_id, job_id = _make_campaign_and_job(total=5)
        db.claim_blast_job(claim_token="worker-Y")

        result = db.advance_blast_job_progress_with_cursor(
            job_id,
            batch_size=5,
            last_recipient_id=999,
            claim_token="worker-Y",
        )
        self.assertEqual(result["status"], "done")
        self.assertIsNotNone(result["finished_at"])

    def test_stale_claim_no_ops(self):
        # A second worker with the WRONG token can't bump the cursor
        # — protects the legitimate worker's progress if a stale tick
        # ever wakes up.
        _campaign_id, job_id = _make_campaign_and_job(total=10)
        db.claim_blast_job(claim_token="real-worker")

        # Bogus claim → no change.
        result = db.advance_blast_job_progress_with_cursor(
            job_id,
            batch_size=99,
            last_recipient_id=9999,
            claim_token="ghost-worker",
        )
        # Row stays at 0/10 because the UPDATE was filtered out.
        self.assertEqual(int(result["processed_recipients"]), 0)
        self.assertEqual(int(result["last_recipient_id"]), 0)

    def test_advance_releases_claim_for_next_tick(self):
        _campaign_id, job_id = _make_campaign_and_job(total=10)
        db.claim_blast_job(claim_token="worker-1")
        # First batch.
        db.advance_blast_job_progress_with_cursor(
            job_id,
            batch_size=3,
            last_recipient_id=10,
            claim_token="worker-1",
        )
        # Next tick can re-claim with a different token (claim_token
        # was nulled out by the advance call).
        next_claim = db.claim_blast_job(claim_token="worker-2")
        self.assertIsNotNone(next_claim)
        self.assertEqual(next_claim["id"], job_id)


if __name__ == "__main__":
    unittest.main()

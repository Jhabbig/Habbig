"""Regression tests for /admin/newsletter/send recipient bounding.

Audit #12 MED #1: the original handler walked every confirmed subscriber
inside the request and awaited an ``enqueue_email`` per row. A 100k
blast = 100k DB writes on the admin POST path, easily stalling the
worker.

The fix:
  1. The synchronous portion is capped at MAX_INLINE_RECIPIENTS (500).
  2. Anything past the cap is recorded as a row in
     ``newsletter_blast_jobs`` (migration 187).
  3. A scheduled worker (``newsletter_blast_tick``) drains the row in
     batches of MAX_BATCH_PER_TICK per cron pulse.

These tests verify:
  * <=cap blasts stay inline (back-compat with the existing flow).
  * >cap blasts return 200 with ``queued_count`` set and a
    ``newsletter_blast_jobs`` row recorded.
  * The synchronous enqueue count is bounded — even with 600 recipients
    in the table the handler runs at most ``cap`` enqueue calls.
  * The tick worker drains the deferred tail into ``enqueue_email``
    calls and flips the row to ``status='done'`` once
    ``processed_recipients == total_recipients``.

Auth + DB setup mirror ``test_admin_newsletter.py`` so the two suites
can run back-to-back without polluting each other.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import server  # noqa: E402


def _suffix() -> str:
    # Each test class gets a fresh suffix so subscriber seeding never
    # collides with rows from sibling classes in the shared in-memory DB.
    return f"{os.getpid()}_bb"


def _create_admin_session() -> tuple[int, str]:
    email = f"newsletter_bb_admin_{_suffix()}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        uid = int(existing["id"])
    else:
        uid = db.create_user(
            email, "Password1!verylong",
            username=f"newsletter_bb_admin_{_suffix()}",
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


def _prime_csrf(client: TestClient, session_token: str) -> str:
    client.get(
        "/admin/newsletter",
        cookies={server.COOKIE_NAME: session_token},
        follow_redirects=False,
    )
    return client.cookies.get(server.CSRF_COOKIE_NAME) or ""


def _post_blast(
    client: TestClient,
    *,
    admin_token: str,
    csrf: str,
) -> object:
    body = urlencode([
        (server.CSRF_FORM_FIELD, csrf),
        ("subject", "Bounded blast"),
        ("body_md", "Hello **friends**."),
        ("segment", "all"),
        ("frequency_filter", ""),
        ("schedule", "now"),
    ])
    return client.post(
        "/admin/newsletter/send",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            # JSON callers (tests, future admin tooling) get the bounded
            # counts back so the assertions don't have to introspect the
            # DB to confirm the deferred-tail flip.
            "Accept": "application/json",
        },
        cookies={
            server.COOKIE_NAME: admin_token,
            server.CSRF_COOKIE_NAME: csrf,
        },
        follow_redirects=False,
    )


class BlastBoundingTestCase(unittest.TestCase):
    """Cover the inline cap + deferred-tail recording."""

    @classmethod
    def setUpClass(cls):
        try:
            import migrations as _migrations
            _migrations.upgrade_to_head()
        except Exception:
            pass
        cls.client = TestClient(server.app)
        cls.admin_id, cls.admin_token = _create_admin_session()

    def setUp(self):
        # Each test gets a clean campaigns + jobs table and only its own
        # seeded subscriber rows.
        with db.conn() as c:
            c.execute("DELETE FROM newsletter_campaigns")
            c.execute("DELETE FROM newsletter_blast_jobs")
            c.execute(
                "DELETE FROM newsletter_subscribers WHERE email LIKE ?",
                (f"%_{_suffix()}_blast@example.com",),
            )

    # ── Happy path: small blast stays fully inline ──────────────────

    def test_under_cap_blast_runs_fully_inline(self):
        # Cap is well above the 5 rows we seed here, so every recipient
        # MUST be enqueued inline and no jobs row created.
        emails = [
            f"u{i}_{_suffix()}_blast@example.com" for i in range(5)
        ]
        for em in emails:
            _seed_confirmed(em)

        csrf = _prime_csrf(self.client, self.admin_token)
        self.assertTrue(csrf)

        async def _fake_enqueue(*args, **kwargs):
            _fake_enqueue.calls.append(kwargs.get("to"))
            return 1
        _fake_enqueue.calls = []  # type: ignore[attr-defined]

        with patch(
            "jobs.email_jobs.enqueue_email", new=_fake_enqueue,
        ):
            r = _post_blast(
                self.client,
                admin_token=self.admin_token, csrf=csrf,
            )

        self.assertEqual(r.status_code, 200)
        payload = r.json()
        self.assertEqual(payload["status"], "sent")
        self.assertEqual(payload["queued_count"], 0)
        self.assertGreaterEqual(payload["immediate_enqueued"], 5)
        # No deferred job row recorded.
        self.assertIsNone(payload["blast_job_id"])
        with db.conn() as c:
            count = c.execute(
                "SELECT COUNT(*) AS n FROM newsletter_blast_jobs"
            ).fetchone()["n"]
        self.assertEqual(int(count), 0)
        # Every confirmed row got an enqueue call.
        for em in emails:
            self.assertIn(em.lower(), _fake_enqueue.calls)

    # ── Bounded: >cap blast defers the tail ─────────────────────────

    def test_over_cap_blast_bounds_inline_and_defers_tail(self):
        """A blast with >cap recipients enqueues exactly ``cap`` inline
        and records the remainder as a pending blast_jobs row.

        We monkey-patch ``MAX_INLINE_RECIPIENTS`` down to 5 so the test
        can stay fast (seeding 501 rows in CI is wasteful). The handler
        reads the constant via ``db.NEWSLETTER_MAX_INLINE_RECIPIENTS``,
        so patching that symbol on the db re-export controls both the
        admin route and the worker.
        """
        # Seed 8 recipients with a cap of 5 → 5 inline, 3 deferred.
        total = 8
        for i in range(total):
            _seed_confirmed(
                f"o{i:03d}_{_suffix()}_blast@example.com",
            )

        csrf = _prime_csrf(self.client, self.admin_token)
        self.assertTrue(csrf)

        async def _fake_enqueue(*args, **kwargs):
            _fake_enqueue.calls.append(kwargs.get("to"))
            return 1
        _fake_enqueue.calls = []  # type: ignore[attr-defined]

        with patch.object(db, "NEWSLETTER_MAX_INLINE_RECIPIENTS", 5):
            with patch(
                "jobs.email_jobs.enqueue_email", new=_fake_enqueue,
            ):
                r = _post_blast(
                    self.client,
                    admin_token=self.admin_token, csrf=csrf,
                )

        self.assertEqual(r.status_code, 200)
        payload = r.json()
        # The synchronous portion MUST be bounded at exactly the cap.
        self.assertEqual(payload["immediate_enqueued"], 5)
        # And every additional recipient lives in the deferred tail.
        self.assertEqual(payload["queued_count"], total - 5)
        self.assertEqual(payload["status"], "queued")
        self.assertIsNotNone(payload["blast_job_id"])
        self.assertEqual(
            payload["recipient_count"], total,
            "recipient_count includes BOTH the inline portion and the "
            "deferred tail",
        )

        # The deferred tail is persisted to newsletter_blast_jobs with
        # status='pending', total=3, processed=0.
        with db.conn() as c:
            jobs_row = c.execute(
                "SELECT id, campaign_id, status, total_recipients, "
                " processed_recipients FROM newsletter_blast_jobs "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(jobs_row)
        self.assertEqual(jobs_row["status"], "pending")
        self.assertEqual(int(jobs_row["total_recipients"]), total - 5)
        self.assertEqual(int(jobs_row["processed_recipients"]), 0)

        # The synchronous enqueue side-effects MUST also match the cap.
        self.assertEqual(len(_fake_enqueue.calls), 5)

        # The campaign row records the FULL recipient_count but leaves
        # sent_at NULL until the tail drains (the tick worker
        # backfills it).
        with db.conn() as c:
            camp_row = c.execute(
                "SELECT recipient_count, sent_at FROM newsletter_campaigns "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(int(camp_row["recipient_count"]), total)
        self.assertIsNone(camp_row["sent_at"])

    # ── Worker drains the deferred tail ─────────────────────────────

    def test_tick_drains_deferred_tail_and_marks_done(self):
        """Run the tick worker against a freshly-deferred job and verify
        it advances ``processed_recipients`` then closes the row.
        """
        total = 8
        for i in range(total):
            _seed_confirmed(
                f"t{i:03d}_{_suffix()}_blast@example.com",
            )

        csrf = _prime_csrf(self.client, self.admin_token)
        async def _fake_enqueue(*args, **kwargs):
            _fake_enqueue.calls.append(kwargs.get("to"))
            return 1
        _fake_enqueue.calls = []  # type: ignore[attr-defined]

        with patch.object(db, "NEWSLETTER_MAX_INLINE_RECIPIENTS", 5):
            with patch(
                "jobs.email_jobs.enqueue_email", new=_fake_enqueue,
            ):
                r = _post_blast(
                    self.client,
                    admin_token=self.admin_token, csrf=csrf,
                )
                payload = r.json()
                self.assertEqual(payload["queued_count"], 3)

                # Reset the mock so we measure only the tick's enqueues.
                _fake_enqueue.calls = []

                # Tick: drain the tail in a single pulse (3 recipients).
                from jobs.newsletter_blast_jobs import (
                    newsletter_blast_tick,
                )
                result = asyncio.run(newsletter_blast_tick())

        # The tick MUST have enqueued exactly the deferred tail.
        self.assertEqual(len(_fake_enqueue.calls), 3)
        # And it MUST have closed the row.
        self.assertEqual(result["status_after"], "done")
        self.assertEqual(int(result["processed_after"]), 3)
        self.assertEqual(int(result["total"]), 3)

        with db.conn() as c:
            jobs_row = c.execute(
                "SELECT status, processed_recipients, total_recipients, "
                " finished_at, started_at FROM newsletter_blast_jobs "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(jobs_row["status"], "done")
        self.assertEqual(
            int(jobs_row["processed_recipients"]),
            int(jobs_row["total_recipients"]),
        )
        self.assertIsNotNone(jobs_row["started_at"])
        self.assertIsNotNone(jobs_row["finished_at"])

        # And the campaign's sent_at is now backfilled.
        with db.conn() as c:
            camp_row = c.execute(
                "SELECT sent_at FROM newsletter_campaigns "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(camp_row["sent_at"])


if __name__ == "__main__":
    unittest.main()

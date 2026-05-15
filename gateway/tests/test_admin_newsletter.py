"""Tests for /admin/newsletter — compose + schedule blasts.

Covers:
  * Anon callers get 403/redirect on the page + JSON endpoints.
  * Regular logged-in users get 403.
  * Admin can render the compose page with the past-campaigns history.
  * POST /admin/newsletter/send requires CSRF (middleware-enforced).
  * Admin send-now fans out one ``enqueue_email`` per matching confirmed
    subscriber and records a campaign row with the correct
    ``recipient_count``.
  * Segment + frequency filters narrow the recipient set.
  * Scheduling for the future records ``sent_at=NULL`` and does NOT
    enqueue any emails synchronously.

Auth setup mirrors test_admin_users.py: a 2FA-verified super-admin
session + a vanilla logged-in user. The newsletter_subscribers table is
populated directly via SQL so we can control segments/frequencies/
confirmation state precisely.
"""

from __future__ import annotations

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
    return f"{os.getpid()}"


def _create_admin_session() -> tuple[int, str]:
    email = f"newsletter_admin_{_suffix()}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        uid = int(existing["id"])
    else:
        uid = db.create_user(
            email, "Password1!verylong",
            username=f"newsletter_admin_{_suffix()}",
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


def _create_regular_session() -> tuple[int, str]:
    email = f"newsletter_user_{_suffix()}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        uid = int(existing["id"])
        db.set_user_role(uid, 0)
    else:
        uid = db.create_user(
            email, "Password1!verylong",
            username=f"newsletter_user_{_suffix()}",
        )
        db.set_user_role(uid, 0)
    return uid, db.create_session(uid)


def _seed_subscriber(
    email: str,
    *,
    segment: str = "all",
    frequency: str = "weekly",
    confirmed: bool = True,
    unsubscribed: bool = False,
) -> int:
    """Insert a newsletter_subscribers row at a known state.

    ``confirmed=True`` puts a unix-seconds timestamp on confirmed_at.
    ``unsubscribed=True`` flips unsubscribed_at — even confirmed rows
    are excluded from blasts when this is set.
    """
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO newsletter_subscribers "
            "(email, subscribed_at, source, segment, frequency, confirmed_at, "
            " unsubscribed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                email.lower(),
                now,
                "test",
                segment,
                frequency,
                now if confirmed else None,
                now if unsubscribed else None,
            ),
        )
        return int(cur.lastrowid)


def _prime_csrf(client: TestClient, session_token: str) -> str:
    """GET the newsletter page so the CSRF cookie is minted, then return
    its value for the next POST.
    """
    client.get(
        "/admin/newsletter",
        cookies={server.COOKIE_NAME: session_token},
        follow_redirects=False,
    )
    return client.cookies.get(server.CSRF_COOKIE_NAME) or ""


class AdminNewsletterTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import migrations as _migrations
            _migrations.upgrade_to_head()
        except Exception:
            pass
        cls.client = TestClient(server.app)

    def setUp(self):
        # Each test gets a clean campaigns table and a fresh slice of
        # subscribers so recipient-count assertions are deterministic.
        with db.conn() as c:
            c.execute("DELETE FROM newsletter_campaigns")
            c.execute(
                "DELETE FROM newsletter_subscribers WHERE email LIKE ?",
                (f"%_{_suffix()}_blast@example.com",),
            )
        # Fixture-pollution guard: the conftest ``_reset_global_test_state``
        # autouse fixture wipes ``sessions`` *and* ``users`` after every
        # function-scoped test on the shared in-memory DB. That means any
        # session token minted in ``setUpClass`` is dead by the time the
        # second test in this module runs — admin/user requests then 302
        # to /gate or 403 on CSRF because the cookie no longer resolves
        # to a row. Mint a fresh admin + regular session per test instead.
        # Same story for the TestClient cookie jar: httpx 0.27 merges
        # per-request ``cookies={...}`` kwargs into the persistent jar,
        # so stale tokens from a previous test would clobber the new one.
        self.client.cookies.clear()
        self.admin_id, self.admin_token = _create_admin_session()
        self.user_id, self.user_token = _create_regular_session()
        self.admin_cookies = {server.COOKIE_NAME: self.admin_token}
        self.user_cookies = {server.COOKIE_NAME: self.user_token}

    # ── Auth gates ──────────────────────────────────────────────────

    def test_page_rejects_anon(self):
        r = self.client.get(
            "/admin/newsletter", cookies={}, follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303, 403))

    def test_page_rejects_regular_user(self):
        r = self.client.get(
            "/admin/newsletter",
            cookies=self.user_cookies,
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403)

    def test_send_rejects_anon(self):
        r = self.client.post(
            "/admin/newsletter/send",
            cookies={},
            follow_redirects=False,
        )
        # CSRF middleware bites before the admin check, so 403 is the
        # baseline rejection. Either way the body MUST NOT be enqueued.
        self.assertIn(r.status_code, (302, 303, 403))

    def test_send_rejects_regular_user(self):
        # Even with a logged-in session, a non-admin can't post here.
        csrf = _prime_csrf(self.client, self.user_token)
        body = urlencode([
            (server.CSRF_FORM_FIELD, csrf or ""),
            ("subject", "x"), ("body_md", "x"),
            ("segment", "all"), ("schedule", "now"),
        ])
        r = self.client.post(
            "/admin/newsletter/send",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            cookies={
                server.COOKIE_NAME: self.user_token,
                **({server.CSRF_COOKIE_NAME: csrf} if csrf else {}),
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403)

    # ── Page render ─────────────────────────────────────────────────

    def test_page_admin_200(self):
        r = self.client.get(
            "/admin/newsletter",
            cookies=self.admin_cookies,
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        body = r.text
        self.assertIn("Newsletter blasts", body)
        self.assertIn('id="newsletter-form"', body)
        self.assertIn('name="subject"', body)
        self.assertIn('name="body_md"', body)
        self.assertIn('name="segment"', body)
        self.assertIn('name="frequency_filter"', body)
        self.assertIn('name="schedule"', body)
        # The audience card carries the live recipient count in Geist
        # Mono — we don't assert the exact number (other tests in the
        # suite may have seeded subscribers) but the wrapper must exist.
        self.assertIn("newsletter-recipient-count__value", body)

    # ── Recipient query math ────────────────────────────────────────

    def test_recipient_count_filters_unconfirmed_and_unsubbed(self):
        _seed_subscriber(
            f"a_{_suffix()}_blast@example.com",
            segment="all", frequency="weekly",
            confirmed=True,
        )
        _seed_subscriber(
            f"b_{_suffix()}_blast@example.com",
            segment="all", frequency="weekly",
            confirmed=False,   # pending double-opt-in — excluded.
        )
        _seed_subscriber(
            f"c_{_suffix()}_blast@example.com",
            segment="all", frequency="weekly",
            confirmed=True, unsubscribed=True,  # excluded.
        )

        # Only the first row matches.
        n = db.count_blast_recipients(segment="all", frequency_filter=None)
        # Other tests in the suite may have seeded confirmed rows in the
        # shared in-memory DB; assert the floor instead of equality.
        self.assertGreaterEqual(n, 1)

        # Walk the recipients list and prove our two excluded rows aren't
        # in there.
        rows = db.get_blast_recipients(
            segment="all", frequency_filter=None,
        )
        emails = {r["email"] for r in rows}
        self.assertIn(
            f"a_{_suffix()}_blast@example.com".lower(), emails,
        )
        self.assertNotIn(
            f"b_{_suffix()}_blast@example.com".lower(), emails,
        )
        self.assertNotIn(
            f"c_{_suffix()}_blast@example.com".lower(), emails,
        )

    def test_recipient_count_segment_includes_all_bucket(self):
        # 'all'-bucket subscribers should match a targeted segment send
        # because they explicitly opted into every segment.
        _seed_subscriber(
            f"mkts_{_suffix()}_blast@example.com",
            segment="markets", frequency="weekly", confirmed=True,
        )
        _seed_subscriber(
            f"all_{_suffix()}_blast@example.com",
            segment="all", frequency="weekly", confirmed=True,
        )
        _seed_subscriber(
            f"clim_{_suffix()}_blast@example.com",
            segment="climate", frequency="weekly", confirmed=True,
        )

        rows = db.get_blast_recipients(
            segment="markets", frequency_filter=None,
        )
        emails = {r["email"] for r in rows}
        self.assertIn(
            f"mkts_{_suffix()}_blast@example.com".lower(), emails,
        )
        # 'all' bucket gets the markets blast too.
        self.assertIn(
            f"all_{_suffix()}_blast@example.com".lower(), emails,
        )
        # Climate-only subscribers DO NOT receive a markets blast.
        self.assertNotIn(
            f"clim_{_suffix()}_blast@example.com".lower(), emails,
        )

    def test_recipient_count_frequency_filter(self):
        _seed_subscriber(
            f"w_{_suffix()}_blast@example.com",
            segment="all", frequency="weekly", confirmed=True,
        )
        _seed_subscriber(
            f"m_{_suffix()}_blast@example.com",
            segment="all", frequency="monthly", confirmed=True,
        )
        rows = db.get_blast_recipients(
            segment="all", frequency_filter="monthly",
        )
        emails = {r["email"] for r in rows}
        self.assertNotIn(
            f"w_{_suffix()}_blast@example.com".lower(), emails,
        )
        self.assertIn(
            f"m_{_suffix()}_blast@example.com".lower(), emails,
        )

    # ── Send-now end-to-end ─────────────────────────────────────────

    def _send_blast_now(self, csrf: str, *, segment: str = "all"):
        body = urlencode([
            (server.CSRF_FORM_FIELD, csrf),
            ("subject", "Launch is here"),
            ("body_md", "Hello **friends**.\n\nWe just shipped."),
            ("segment", segment),
            ("frequency_filter", ""),
            ("schedule", "now"),
        ])
        return self.client.post(
            "/admin/newsletter/send",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            cookies={
                server.COOKIE_NAME: self.admin_token,
                server.CSRF_COOKIE_NAME: csrf,
            },
            follow_redirects=False,
        )

    def test_send_now_enqueues_one_per_recipient_and_records_campaign(self):
        # Three confirmed, two excluded.
        confirmed_emails = [
            f"r1_{_suffix()}_blast@example.com",
            f"r2_{_suffix()}_blast@example.com",
            f"r3_{_suffix()}_blast@example.com",
        ]
        for em in confirmed_emails:
            _seed_subscriber(em, segment="all", frequency="weekly",
                             confirmed=True)
        _seed_subscriber(
            f"x_{_suffix()}_blast@example.com",
            segment="all", frequency="weekly", confirmed=False,
        )

        csrf = _prime_csrf(self.client, self.admin_token)
        self.assertTrue(csrf)

        # Pre-count what the handler should see so the recipient_count
        # assertion is exact even when other tests left rows behind.
        expected_recipients = db.count_blast_recipients(
            segment="all", frequency_filter=None,
        )

        async def _fake_enqueue(*args, **kwargs):
            _fake_enqueue.calls.append(kwargs.get("to"))
            return 1
        _fake_enqueue.calls = []  # type: ignore[attr-defined]

        with patch(
            "jobs.email_jobs.enqueue_email", new=_fake_enqueue,
        ):
            r = self._send_blast_now(csrf)

        self.assertIn(r.status_code, (302, 303))
        # Every confirmed row got an enqueue call.
        self.assertEqual(
            len(_fake_enqueue.calls), expected_recipients,
        )
        for em in confirmed_emails:
            self.assertIn(em.lower(), _fake_enqueue.calls)

        # Campaign row recorded with matching recipient_count + sent_at.
        with db.conn() as c:
            row = c.execute(
                "SELECT * FROM newsletter_campaigns ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["subject"], "Launch is here")
        self.assertEqual(row["segment"], "all")
        self.assertEqual(int(row["recipient_count"]), expected_recipients)
        self.assertIsNotNone(row["sent_at"])

    # ── Scheduling ──────────────────────────────────────────────────

    def test_send_later_records_pending_campaign_no_enqueue(self):
        _seed_subscriber(
            f"s1_{_suffix()}_blast@example.com",
            segment="all", frequency="weekly", confirmed=True,
        )
        csrf = _prime_csrf(self.client, self.admin_token)
        self.assertTrue(csrf)

        future_dt = time.strftime(
            "%Y-%m-%dT%H:%M",
            time.gmtime(int(time.time()) + 3 * 86400),
        )

        async def _fake_enqueue(*args, **kwargs):  # pragma: no cover
            _fake_enqueue.calls.append(kwargs.get("to"))
            return 1
        _fake_enqueue.calls = []  # type: ignore[attr-defined]

        body = urlencode([
            (server.CSRF_FORM_FIELD, csrf),
            ("subject", "Future blast"),
            ("body_md", "## Heads up\n\nThis fires later."),
            ("segment", "all"),
            ("frequency_filter", ""),
            ("schedule", "later"),
            ("scheduled_at", future_dt),
        ])

        with patch(
            "jobs.email_jobs.enqueue_email", new=_fake_enqueue,
        ):
            r = self.client.post(
                "/admin/newsletter/send",
                data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                cookies={
                    server.COOKIE_NAME: self.admin_token,
                    server.CSRF_COOKIE_NAME: csrf,
                },
                follow_redirects=False,
            )

        self.assertIn(r.status_code, (302, 303))
        # Scheduled — no synchronous enqueue.
        self.assertEqual(_fake_enqueue.calls, [])

        with db.conn() as c:
            row = c.execute(
                "SELECT * FROM newsletter_campaigns ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["subject"], "Future blast")
        self.assertIsNone(row["sent_at"])
        self.assertGreater(
            int(row["scheduled_at"]), int(time.time()),
        )

    def test_send_later_rejects_past_timestamp(self):
        csrf = _prime_csrf(self.client, self.admin_token)
        past_dt = time.strftime(
            "%Y-%m-%dT%H:%M",
            time.gmtime(int(time.time()) - 86400),
        )
        body = urlencode([
            (server.CSRF_FORM_FIELD, csrf),
            ("subject", "Backdated"),
            ("body_md", "x"),
            ("segment", "all"),
            ("schedule", "later"),
            ("scheduled_at", past_dt),
        ])
        r = self.client.post(
            "/admin/newsletter/send",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            cookies={
                server.COOKIE_NAME: self.admin_token,
                server.CSRF_COOKIE_NAME: csrf,
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 400)

    # ── Recipient JSON endpoint ─────────────────────────────────────

    def test_recipient_count_json_admin_ok(self):
        _seed_subscriber(
            f"j1_{_suffix()}_blast@example.com",
            segment="all", frequency="weekly", confirmed=True,
        )
        r = self.client.get(
            "/admin/newsletter/recipients?segment=all",
            cookies=self.admin_cookies,
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("count", data)
        self.assertGreaterEqual(int(data["count"]), 1)

    def test_recipient_count_json_rejects_anon(self):
        r = self.client.get(
            "/admin/newsletter/recipients?segment=all",
            cookies={},
        )
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()

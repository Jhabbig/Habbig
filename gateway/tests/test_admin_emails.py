"""Tests for /admin/emails — outbound queue + delivery review.

Covers:
  - anon and non-admin callers are blocked (302/403)
  - admin GET returns 200 and renders the diagnostic surface
  - recipient redaction in the list view (only `abc***@domain` is shown,
    never the full local-part)
  - the JSON list endpoint mirrors the redaction
  - POST /admin/emails/{id}/resend requires CSRF (the double-submit
    cookie + form field). Missing or mismatched token -> 403.
  - admin with a valid CSRF token can resend a failed delivery, and a
    new background_jobs row is created.

Auth setup mirrors ``test_admin_jobs.py``: seed an admin user + session
in SQLite and mark it 2FA-verified so ``_require_admin_user`` lets it
through.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import server  # noqa: E402


def _create_admin_session() -> str:
    email = f"emails_admin_{os.getpid()}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        user_id = existing["id"]
    else:
        user_id = db.create_user(email, "Password1!verylong",
                                 username=f"emails_admin_{os.getpid()}")
    db.set_user_role(user_id, 2)  # super admin
    try:
        db.set_user_2fa_method(user_id, "email_otp")
    except Exception:
        pass
    token = db.create_session(user_id)
    try:
        db.mark_session_two_fa_verified(token)
    except Exception:
        pass
    return token


def _create_regular_session() -> str:
    email = f"emails_user_{os.getpid()}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        uid = existing["id"]
        db.set_user_role(uid, 0)
    else:
        uid = db.create_user(email, "Password1!verylong",
                             username=f"emails_user_{os.getpid()}")
        db.set_user_role(uid, 0)
    return db.create_session(uid)


def _insert_email_job(
    *,
    template: str,
    to: str,
    status: str = "complete",
    error: str | None = None,
    enqueued_offset: int = 0,
    context: dict | None = None,
) -> int:
    """Insert a synthetic background_jobs row for a send_email job."""
    # Make sure the table exists (the in-process backend creates it lazily).
    try:
        from jobs.backend import _ensure_jobs_table
        _ensure_jobs_table()
    except Exception:
        pass

    payload = json.dumps({
        "to": to,
        "template": template,
        "context": context or {},
    })
    now = int(time.time())
    started = now + enqueued_offset
    finished = None if status in ("queued", "running") else started + 1
    duration_ms = None if finished is None else (finished - started) * 1000
    attempts = 0 if status == "queued" else 1
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO background_jobs "
            "(name, payload, status, attempts, max_attempts, error, "
            " enqueued_at, started_at, finished_at, duration_ms) "
            "VALUES ('send_email', ?, ?, ?, 3, ?, ?, ?, ?, ?)",
            (payload, status, attempts, error, started, started, finished,
             duration_ms),
        )
        return cur.lastrowid


class AdminEmailsAuthTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import migrations as _migrations
            _migrations.upgrade_to_head()
        except Exception:
            pass
        cls.client = TestClient(server.app)
        cls.admin_cookies = {server.COOKIE_NAME: _create_admin_session()}
        cls.user_cookies = {server.COOKIE_NAME: _create_regular_session()}

    def setUp(self):
        # Clean send_email rows between tests so the page assertions
        # are deterministic.
        try:
            from jobs.backend import _ensure_jobs_table
            _ensure_jobs_table()
        except Exception:
            pass
        with db.conn() as c:
            c.execute("DELETE FROM background_jobs WHERE name = 'send_email'")

    # ── Auth ───────────────────────────────────────────────────────────

    def test_page_rejects_anon(self):
        r = self.client.get("/admin/emails", cookies={}, follow_redirects=False)
        # Anonymous -> redirected to /gate by _denied_response, OR 403.
        self.assertIn(r.status_code, (302, 303, 403))

    def test_page_rejects_non_admin(self):
        r = self.client.get(
            "/admin/emails",
            cookies=self.user_cookies,
            follow_redirects=False,
        )
        # Non-admin logged-in users hit the 403 page from _denied_response.
        self.assertEqual(r.status_code, 403)

    def test_api_list_rejects_anon(self):
        r = self.client.get("/admin/api/emails", cookies={})
        self.assertEqual(r.status_code, 403)

    def test_api_list_rejects_non_admin(self):
        r = self.client.get("/admin/api/emails", cookies=self.user_cookies)
        self.assertEqual(r.status_code, 403)

    # ── Page renders ───────────────────────────────────────────────────

    def test_page_admin_200(self):
        _insert_email_job(template="welcome", to="someone@example.com",
                          status="complete")
        r = self.client.get(
            "/admin/emails",
            cookies=self.admin_cookies,
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        body = r.text
        # Hero + stats + section titles render.
        self.assertIn(">Emails<", body)
        self.assertIn("Sent", body)
        self.assertIn("Failed", body)
        self.assertIn("Recent deliveries", body)
        self.assertIn("welcome", body)

    def test_failed_email_surfaces_error(self):
        _insert_email_job(
            template="payment_failed",
            to="bouncer@example.com",
            status="failed",
            error="SMTP 550 mailbox not found",
        )
        r = self.client.get("/admin/emails", cookies=self.admin_cookies,
                            follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        body = r.text
        self.assertIn("payment_failed", body)
        self.assertIn("SMTP 550 mailbox not found", body)
        self.assertIn("emails-status--failed", body)

    # ── Recipient redaction ────────────────────────────────────────────

    def test_recipient_redaction_in_list_view(self):
        full_local = "alice.bobson"
        domain = "example.com"
        addr = f"{full_local}@{domain}"
        _insert_email_job(template="welcome", to=addr, status="complete")

        r = self.client.get("/admin/emails", cookies=self.admin_cookies,
                            follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        body = r.text
        # The full local part must NOT be in the page body.
        self.assertNotIn("alice.bobson@", body)
        # The redacted form (first chars + ***@domain) MUST be.
        self.assertIn("ali***@example.com", body)

    def test_redaction_short_local_part(self):
        # Local-parts <= 3 chars use only the first char to avoid leaking
        # the whole thing.
        _insert_email_job(template="welcome", to="ab@example.com",
                          status="complete")
        r = self.client.get("/admin/emails", cookies=self.admin_cookies,
                            follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        body = r.text
        self.assertNotIn("ab@example.com", body)
        self.assertIn("a***@example.com", body)

    def test_api_list_also_redacts(self):
        _insert_email_job(template="welcome", to="charlie@example.com",
                          status="complete")
        r = self.client.get("/admin/api/emails",
                            cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertGreaterEqual(len(data["emails"]), 1)
        row = data["emails"][0]
        # The redacted form is present.
        self.assertIn("***@example.com", row["recipient_redacted"])
        # The raw recipient must NOT be present in the list-view JSON.
        self.assertNotIn("recipient", row)

    # ── Filters ────────────────────────────────────────────────────────

    def test_filter_by_status(self):
        _insert_email_job(template="welcome", to="a@example.com",
                          status="complete")
        _insert_email_job(template="welcome", to="b@example.com",
                          status="failed", error="boom")
        r = self.client.get("/admin/api/emails?status=failed",
                            cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        statuses = {row["status"] for row in r.json()["emails"]}
        self.assertEqual(statuses, {"failed"})

    def test_filter_by_template(self):
        _insert_email_job(template="welcome", to="a@example.com",
                          status="complete")
        _insert_email_job(template="payment_failed", to="b@example.com",
                          status="complete")
        r = self.client.get("/admin/api/emails?template=welcome",
                            cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        templates = {row["template"] for row in r.json()["emails"]}
        self.assertEqual(templates, {"welcome"})


class AdminEmailsResendTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import migrations as _migrations
            _migrations.upgrade_to_head()
        except Exception:
            pass
        cls.client = TestClient(server.app)
        cls.admin_cookies = {server.COOKIE_NAME: _create_admin_session()}

    def setUp(self):
        try:
            from jobs.backend import _ensure_jobs_table
            _ensure_jobs_table()
        except Exception:
            pass
        with db.conn() as c:
            c.execute("DELETE FROM background_jobs WHERE name = 'send_email'")

    def _seed_failed(self) -> int:
        return _insert_email_job(
            template="welcome",
            to="resender@example.com",
            status="failed",
            error="initial failure",
            context={"username": "resender"},
        )

    def test_resend_requires_csrf_form_field(self):
        """POST without the _csrf token in body OR header -> 403."""
        job_id = self._seed_failed()

        # Pre-flight GET to put a _csrf cookie on the client. We
        # intentionally do NOT echo the cookie back in the form body
        # below, so the double-submit check must fail.
        self.client.get("/admin/emails", cookies=self.admin_cookies)

        r = self.client.post(
            f"/admin/emails/{job_id}/resend",
            cookies=self.admin_cookies,
            data={},  # no _csrf
            follow_redirects=False,
        )
        # CSRF middleware -> 403 JSON error.
        self.assertEqual(r.status_code, 403)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        self.assertEqual(body.get("error"), "CSRF validation failed")

    def test_resend_succeeds_with_csrf(self):
        job_id = self._seed_failed()

        # Prime the CSRF cookie, then send the same token back as the
        # form field (the double-submit pattern).
        pre = self.client.get("/admin/emails", cookies=self.admin_cookies)
        csrf = pre.cookies.get(server.CSRF_COOKIE_NAME)
        self.assertIsNotNone(csrf, "CSRF cookie not set on GET")

        cookies = dict(self.admin_cookies)
        cookies[server.CSRF_COOKIE_NAME] = csrf

        r = self.client.post(
            f"/admin/emails/{job_id}/resend",
            cookies=cookies,
            data={server.CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200, msg=f"body={r.text!r}")
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["original_id"], job_id)
        self.assertIsInstance(body["new_job_id"], int)
        self.assertNotEqual(body["new_job_id"], job_id)

        # New row was inserted in background_jobs for the same payload.
        with db.conn() as c:
            row = c.execute(
                "SELECT name, payload FROM background_jobs WHERE id = ?",
                (body["new_job_id"],),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "send_email")
        payload = json.loads(row["payload"])
        self.assertEqual(payload.get("template"), "welcome")
        self.assertEqual(payload.get("to"), "resender@example.com")

    def test_resend_rejects_anon(self):
        job_id = self._seed_failed()
        r = self.client.post(
            f"/admin/emails/{job_id}/resend",
            cookies={},  # no session
            follow_redirects=False,
        )
        # CSRF middleware fires before auth; either is acceptable as
        # long as the resend is rejected.
        self.assertIn(r.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()

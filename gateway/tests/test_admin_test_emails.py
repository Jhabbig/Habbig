"""Tests for /admin/test-emails — preview + send-to-self for email templates.

Covers the five guarantees the prompt asked for:

  - Anonymous callers get 403 (or a redirect to /gate).
  - Admin callers get 200 with the hero + templates list rendered.
  - A test-send enqueues an email via ``jobs.email_jobs.enqueue_email``.
  - The preview endpoint returns valid HTML with the security headers set.
  - The 21st send inside an hour gets 429'd by the per-admin rate limit.

Auth setup mirrors :mod:`test_admin_cost_alerts` — seed a real admin user +
session in SQLite, mark it 2FA-verified, then drive the FastAPI app via
``TestClient``. The rate-limit and enqueue tests stub the email job queue
so we don't depend on a worker or SMTP transport.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import server  # noqa: E402


_CSRF_TOKEN = "test-csrf-token-test-emails-suite"


def _create_admin_session() -> tuple[str, str]:
    """Create an admin session. Returns (session_token, email)."""
    email = f"test_emails_admin_{os.getpid()}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        user_id = existing["id"]
    else:
        user_id = db.create_user(
            email, "Password1!verylong",
            username=f"test_emails_admin_{os.getpid()}",
        )
    db.set_user_role(user_id, 2)
    try:
        db.set_user_2fa_method(user_id, "email_otp")
    except Exception:
        pass
    token = db.create_session(user_id)
    try:
        db.mark_session_two_fa_verified(token)
    except Exception:
        pass
    return token, email


def _create_regular_session() -> str:
    email = f"test_emails_user_{os.getpid()}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        uid = existing["id"]
        db.set_user_role(uid, 0)
    else:
        uid = db.create_user(
            email, "Password1!verylong",
            username=f"test_emails_user_{os.getpid()}",
        )
        db.set_user_role(uid, 0)
    return db.create_session(uid)


def _admin_cookies(session_token: str) -> dict:
    return {
        server.COOKIE_NAME: session_token,
        server.CSRF_COOKIE_NAME: _CSRF_TOKEN,
    }


def _csrf_headers() -> dict:
    return {server.CSRF_HEADER_NAME: _CSRF_TOKEN}


class AdminTestEmailsTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import migrations as _migrations
            _migrations.upgrade_to_head()
        except Exception:
            pass
        cls.client = TestClient(server.app)
        cls.admin_session, cls.admin_email = _create_admin_session()
        cls.user_session = _create_regular_session()

    def setUp(self):
        # Patch enqueue_email so the send path doesn't require a running
        # job backend. Each test resets the capture list so assertions
        # can pin down both the call count and the kwargs.
        import jobs.email_jobs as email_jobs

        self._enqueue_calls = []

        async def _fake_enqueue_email(**kw):
            self._enqueue_calls.append(kw)
            return 1

        self._orig_enqueue = email_jobs.enqueue_email
        email_jobs.enqueue_email = _fake_enqueue_email  # type: ignore[assignment]

        # The route module imports lazily inside the POST handler, so we
        # also have to patch the module's reference if it was already imported.
        import sys as _sys
        if "admin_test_emails_routes" in _sys.modules:
            mod = _sys.modules["admin_test_emails_routes"]
            self._orig_mod_enqueue = getattr(mod, "enqueue_email", None)
            # Nothing to patch on the module — it does `from jobs.email_jobs
            # import enqueue_email` inside the handler, so patching the
            # source module is sufficient.

        # Reset the rate limiter so cross-test state doesn't bleed into
        # the 429 test.
        try:
            from security.rate_limiter import limiter
            with limiter._lock:
                limiter._windows.clear()
        except Exception:
            pass

    def tearDown(self):
        import jobs.email_jobs as email_jobs
        email_jobs.enqueue_email = self._orig_enqueue  # type: ignore[assignment]

    # ── Auth ─────────────────────────────────────────────────────────

    def test_page_anonymous_denied(self):
        """Anonymous callers must not see the test-emails page."""
        r = self.client.get(
            "/admin/test-emails",
            cookies={},
            follow_redirects=False,
        )
        # _denied_response redirects unauth users to /gate (302/303) and
        # returns the 403 page for authed-but-not-admin. Both are valid
        # "not authorised" outcomes for an anonymous caller.
        self.assertIn(r.status_code, (302, 303, 403))

    def test_page_non_admin_403(self):
        r = self.client.get(
            "/admin/test-emails",
            cookies={server.COOKIE_NAME: self.user_session},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403)

    def test_send_anonymous_403(self):
        """An anonymous POST should be rejected (CSRF middleware + admin gate)."""
        r = self.client.post(
            "/admin/test-emails/send",
            cookies={},
            json={"template": "welcome"},
        )
        self.assertIn(r.status_code, (302, 303, 403))

    def test_send_non_admin_403(self):
        r = self.client.post(
            "/admin/test-emails/send",
            cookies={server.COOKIE_NAME: self.user_session,
                     server.CSRF_COOKIE_NAME: _CSRF_TOKEN},
            headers=_csrf_headers(),
            json={"template": "welcome"},
        )
        self.assertEqual(r.status_code, 403)

    def test_preview_non_admin_403(self):
        r = self.client.get(
            "/admin/test-emails/preview/welcome",
            cookies={server.COOKIE_NAME: self.user_session},
        )
        self.assertEqual(r.status_code, 403)

    # ── Page renders ─────────────────────────────────────────────────

    def test_page_admin_200_renders_template_list(self):
        r = self.client.get(
            "/admin/test-emails",
            cookies=_admin_cookies(self.admin_session),
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200, msg=r.text)
        body = r.text
        # Hero in Instrument Serif Italic.
        self.assertIn("Test email templates", body)
        self.assertIn("test-emails__display", body)
        # At least one well-known template surfaces.
        self.assertIn(">welcome<", body)
        self.assertIn("password_reset", body)
        # The context-override textarea ships with default JSON pre-loaded.
        self.assertIn("test-emails-context", body)
        # Send + preview action affordances are wired in.
        self.assertIn("/admin/test-emails/preview/welcome", body)
        self.assertIn('data-action="send"', body)
        # Admin's own email is shown as the destination.
        self.assertIn(self.admin_email, body)
        # base.html is layout-only and must NOT show up in the grid.
        # We look for `>base<` (tag content) so we don't false-match on
        # "base.html" inside copy text.
        self.assertNotIn(">base<", body)

    # ── Test-send enqueues an email ──────────────────────────────────

    def test_send_enqueues_email(self):
        r = self.client.post(
            "/admin/test-emails/send",
            cookies=_admin_cookies(self.admin_session),
            headers=_csrf_headers(),
            json={"template": "welcome"},
        )
        self.assertEqual(r.status_code, 200, msg=r.text)
        body = r.json()
        self.assertTrue(body.get("queued"))
        self.assertEqual(body.get("template"), "welcome")
        self.assertEqual(body.get("recipient"), self.admin_email)

        # The patched enqueue_email captured exactly one call.
        self.assertEqual(len(self._enqueue_calls), 1)
        call = self._enqueue_calls[0]
        self.assertEqual(call["to"], self.admin_email)
        self.assertEqual(call["template"], "welcome")
        self.assertIsInstance(call["context"], dict)
        # The handler force-overwrites the recipient context so override
        # values can't redirect the test send elsewhere.
        self.assertEqual(call["context"]["email"], self.admin_email)
        # And the tag is set so a worker can audit-trail it as a test send.
        self.assertIn("admin_test", call.get("tags") or [])

    def test_send_merges_context_override(self):
        """Override values merge on top of the per-template defaults."""
        r = self.client.post(
            "/admin/test-emails/send",
            cookies=_admin_cookies(self.admin_session),
            headers=_csrf_headers(),
            json={
                "template": "welcome",
                "context": {"display_name": "Override Name", "tier": "Custom"},
            },
        )
        self.assertEqual(r.status_code, 200, msg=r.text)
        self.assertEqual(len(self._enqueue_calls), 1)
        ctx = self._enqueue_calls[0]["context"]
        self.assertEqual(ctx["display_name"], "Override Name")
        self.assertEqual(ctx["tier"], "Custom")
        # Default keys not in the override remain.
        self.assertIn("app_url", ctx)
        # Recipient override is ignored — the handler resets it.
        self.assertEqual(ctx["email"], self.admin_email)

    def test_send_rejects_unknown_template(self):
        r = self.client.post(
            "/admin/test-emails/send",
            cookies=_admin_cookies(self.admin_session),
            headers=_csrf_headers(),
            json={"template": "../etc/passwd"},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(len(self._enqueue_calls), 0)

    def test_send_rejects_missing_template(self):
        r = self.client.post(
            "/admin/test-emails/send",
            cookies=_admin_cookies(self.admin_session),
            headers=_csrf_headers(),
            json={},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(self._enqueue_calls), 0)

    def test_send_requires_csrf(self):
        """A POST without an x-csrf-token header must 403 from the middleware."""
        r = self.client.post(
            "/admin/test-emails/send",
            cookies={server.COOKIE_NAME: self.admin_session},
            json={"template": "welcome"},
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(len(self._enqueue_calls), 0)

    # ── Preview endpoint ─────────────────────────────────────────────

    def test_preview_returns_valid_html(self):
        r = self.client.get(
            "/admin/test-emails/preview/welcome",
            cookies=_admin_cookies(self.admin_session),
        )
        self.assertEqual(r.status_code, 200, msg=r.text)
        # Content-Type is text/html so the browser renders rather than downloads.
        ctype = r.headers.get("content-type", "")
        self.assertTrue(ctype.startswith("text/html"), msg=f"content-type={ctype!r}")
        # X-Frame-Options: DENY blocks clickjacking via iframe.
        self.assertEqual(r.headers.get("x-frame-options"), "DENY")
        # CSP frame-ancestors mirrors XFO for modern browsers.
        self.assertIn("frame-ancestors 'none'", r.headers.get("content-security-policy", ""))
        # The body actually contains the rendered template — base.html
        # wraps every child, so its <html> root must be present.
        body = r.text
        self.assertIn("<html", body.lower())
        self.assertIn("</html>", body.lower())
        # The default display_name (Test Admin) makes it into the welcome
        # template's body, proving the renderer ran with our defaults.
        self.assertIn("Test Admin", body)

    def test_preview_unknown_template_404(self):
        r = self.client.get(
            "/admin/test-emails/preview/does_not_exist",
            cookies=_admin_cookies(self.admin_session),
        )
        self.assertEqual(r.status_code, 404)

    # ── Rate limit ───────────────────────────────────────────────────

    def test_send_rate_limit_trips_on_21st_request(self):
        """20 sends/h are allowed; the 21st should 429."""
        for i in range(20):
            r = self.client.post(
                "/admin/test-emails/send",
                cookies=_admin_cookies(self.admin_session),
                headers=_csrf_headers(),
                json={"template": "welcome"},
            )
            self.assertEqual(r.status_code, 200, msg=f"send #{i + 1} failed: {r.text}")

        # The 21st request hits the per-admin cap and gets a 429 from the
        # rate-limit decorator before the route runs.
        r21 = self.client.post(
            "/admin/test-emails/send",
            cookies=_admin_cookies(self.admin_session),
            headers=_csrf_headers(),
            json={"template": "welcome"},
        )
        self.assertEqual(r21.status_code, 429, msg=r21.text)
        # And the 21st didn't enqueue anything — the decorator short-circuits.
        self.assertEqual(len(self._enqueue_calls), 20)


if __name__ == "__main__":
    unittest.main()

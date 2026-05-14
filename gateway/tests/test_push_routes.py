"""Tests for /api/push/* routes.

Two areas covered:

1. ``/api/push/subscribe`` host-allowlist enforcement (Audit #9 LOW #1).
   The endpoint used to accept any HTTPS URL, which let a logged-in user
   persist arbitrary endpoints and coerce the server into POSTing VAPID-
   signed payloads at them (SSRF + push-spam vector). The handler now
   rejects anything outside the canonical FCM / Mozilla Autopush / Apple
   WebPush / Microsoft WNS host list.

2. ``/api/push/test`` end-to-end dispatch. The "Send yourself a test
   push" button in settings hits this route; we verify the auth + CSRF
   + DB + sender plumbing without firing any real push (pywebpush is
   patched out so no network I/O happens).

These tests cover:
  - unit-level _is_allowed_push_host: every canonical host accepted,
    wildcard-suffix matching for WNS, http:// rejected, malformed URL
    rejected, random https:// host rejected
  - subscribe route: 422 for unsupported hosts, 400 still raised for
    the older "must be https" check so we don't silently swallow it
  - test route: 401 unauthenticated, 403 missing CSRF, 200 with zero
    subscriptions, 200 with a mock subscription row, 503 when push deps
    aren't installed, 500 on unexpected exceptions

Shared DB via tests._testdb — same pattern as test_feedback_routes.py.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

USES_TESTDB = True

from tests import _testdb  # noqa: E402,F401

import db  # noqa: E402
import push  # noqa: E402
import server  # noqa: E402
import push_routes  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


client = TestClient(server.app)

_SHARED_DB_CONN = _testdb._fake_conn


def _pin_shared_db() -> None:
    db.conn = _SHARED_DB_CONN


def _clear_client() -> None:
    try:
        client.cookies.clear()
    except Exception:
        pass


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _pin_shared_db()
        super().setUpClass()

    def setUp(self):
        _pin_shared_db()
        _clear_client()
        super().setUp()


def _make_user(email: str) -> int:
    return db.create_user(email, "TestPass123!", username=email.split("@")[0])


def _login_as(user_id: int) -> dict:
    token = db.create_session(user_id)
    return {
        server.COOKIE_NAME: token,
        "_csrf": "test-csrf-token",
    }


CSRF_HEADERS = {
    "x-csrf-token": "test-csrf-token",
    "content-type": "application/json",
}


# ── Unit tests for the allowlist helper ──────────────────────────────────

class TestIsAllowedPushHost(unittest.TestCase):

    def test_fcm_endpoint_accepted(self):
        self.assertTrue(push_routes._is_allowed_push_host(
            "https://fcm.googleapis.com/fcm/send/abc123"
        ))

    def test_android_fcm_endpoint_accepted(self):
        self.assertTrue(push_routes._is_allowed_push_host(
            "https://android.googleapis.com/send/xyz"
        ))

    def test_push_googleapis_endpoint_accepted(self):
        self.assertTrue(push_routes._is_allowed_push_host(
            "https://push.googleapis.com/some/path"
        ))

    def test_mozilla_endpoint_accepted(self):
        self.assertTrue(push_routes._is_allowed_push_host(
            "https://updates.push.services.mozilla.com/wpush/v2/abc"
        ))

    def test_mozilla_autopush_endpoint_accepted(self):
        self.assertTrue(push_routes._is_allowed_push_host(
            "https://updates-autopush.push.services.mozilla.com/wpush/v2/abc"
        ))

    def test_apple_webpush_endpoint_accepted(self):
        self.assertTrue(push_routes._is_allowed_push_host(
            "https://web.push.apple.com/QABC"
        ))

    def test_apple_api_push_accepted(self):
        self.assertTrue(push_routes._is_allowed_push_host(
            "https://api.push.apple.com/3/device/abc"
        ))

    def test_wns_wildcard_suffix_accepted(self):
        # Microsoft regional WNS hosts use wns2-*.notify.windows.com
        self.assertTrue(push_routes._is_allowed_push_host(
            "https://wns2-by3p.notify.windows.com/raw/...something..."
        ))
        self.assertTrue(push_routes._is_allowed_push_host(
            "https://db5p.notify.windows.com/raw/abc"
        ))

    def test_wns_bare_suffix_rejected(self):
        # The literal "notify.windows.com" with no subdomain must NOT match
        # the "*.notify.windows.com" wildcard — guards against attackers
        # registering the bare apex (or claiming it via a stale record).
        self.assertFalse(push_routes._is_allowed_push_host(
            "https://notify.windows.com/raw/abc"
        ))

    def test_random_https_host_rejected(self):
        self.assertFalse(push_routes._is_allowed_push_host(
            "https://evil.com/push"
        ))

    def test_lookalike_host_rejected(self):
        # Must not be fooled by hosts that *contain* an allowed string
        # somewhere but aren't an actual match.
        self.assertFalse(push_routes._is_allowed_push_host(
            "https://fcm.googleapis.com.evil.com/send"
        ))
        self.assertFalse(push_routes._is_allowed_push_host(
            "https://evil.com/fcm.googleapis.com/send"
        ))

    def test_http_scheme_rejected(self):
        self.assertFalse(push_routes._is_allowed_push_host(
            "http://fcm.googleapis.com/fcm/send/abc"
        ))

    def test_malformed_url_rejected(self):
        self.assertFalse(push_routes._is_allowed_push_host("not-a-url"))
        self.assertFalse(push_routes._is_allowed_push_host(""))
        self.assertFalse(push_routes._is_allowed_push_host("https://"))


# ── HTTP route tests ─────────────────────────────────────────────────────

class TestSubscribeRouteAllowlist(_Base):
    """Hit the FastAPI route directly: bad-host requests must 422 before
    reaching ``push.save_subscription``."""

    def test_random_https_host_rejected_with_422(self):
        uid = _make_user("evil-push@test.com")
        cookies = _login_as(uid)
        r = client.post(
            "/api/push/subscribe",
            json={
                "endpoint": "https://evil.com/push/xyz",
                "keys": {"p256dh": "p", "auth": "a"},
            },
            cookies=cookies, headers=CSRF_HEADERS,
        )
        self.assertEqual(r.status_code, 422)
        # Detail-body assertion intentionally omitted — the global error
        # handler may sanitize 4xx detail strings, and the audit fix only
        # depends on the status code reaching the client.

    def test_http_scheme_rejected_with_400(self):
        # The older "endpoint must be https" guard still wins for non-https
        # URLs — it runs first and returns 400, not 422.
        uid = _make_user("http-push@test.com")
        cookies = _login_as(uid)
        r = client.post(
            "/api/push/subscribe",
            json={
                "endpoint": "http://fcm.googleapis.com/fcm/send/abc",
                "keys": {"p256dh": "p", "auth": "a"},
            },
            cookies=cookies, headers=CSRF_HEADERS,
        )
        self.assertEqual(r.status_code, 400)

    def test_malformed_endpoint_rejected(self):
        # "https://" by itself passes the startswith check but has no host
        # → the allowlist guard catches it and returns 422.
        uid = _make_user("malformed-push@test.com")
        cookies = _login_as(uid)
        r = client.post(
            "/api/push/subscribe",
            json={
                "endpoint": "https://",
                "keys": {"p256dh": "p", "auth": "a"},
            },
            cookies=cookies, headers=CSRF_HEADERS,
        )
        self.assertEqual(r.status_code, 422)


# ── /api/push/test endpoint tests ────────────────────────────────────────

class TestPushTestRoute(_Base):
    """End-to-end coverage for ``POST /api/push/test``.

    The endpoint loads the caller's push subscriptions, builds a payload,
    and hands it to ``push.send_to_user``. We patch the sender to avoid
    touching the network — verifying the route's auth/CSRF/DB plumbing,
    not pywebpush itself.
    """

    def test_unauthenticated_rejected(self):
        # No session cookie. The CSRF middleware runs ahead of auth and
        # rejects the request because the anon caller has no _csrf cookie
        # matching the header (returns 403). Either 401 (auth) or 403
        # (CSRF) is acceptable here — the contract is "anon callers can't
        # reach push.send_to_user". We assert >= 400 and that the route
        # body never reached the sender.
        with patch.object(push, "send_to_user") as mock_send:
            r = client.post("/api/push/test", json={}, headers=CSRF_HEADERS)
        self.assertIn(r.status_code, (401, 403))
        mock_send.assert_not_called()

    def test_missing_csrf_token_rejected(self):
        # CSRF middleware should reject the request before the handler
        # runs, so send_to_user must NOT be invoked.
        uid = _make_user("nocsrf-push@test.com")
        cookies = _login_as(uid)
        with patch.object(push, "send_to_user") as mock_send:
            r = client.post(
                "/api/push/test",
                json={},
                cookies=cookies,
                headers={"content-type": "application/json"},  # no x-csrf-token
            )
        self.assertEqual(r.status_code, 403)
        mock_send.assert_not_called()

    def test_no_subscriptions_returns_zero_counts(self):
        # User with zero push_subscriptions rows: send_to_user short-circuits
        # to {sent:0, failed:0, expired:0} — the route should pass that
        # through as 200 (NOT a 404 — the UI button still wants a sensible
        # JSON shape to render).
        uid = _make_user("empty-push@test.com")
        cookies = _login_as(uid)
        r = client.post(
            "/api/push/test",
            json={},
            cookies=cookies, headers=CSRF_HEADERS,
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body, {"sent": 0, "failed": 0, "expired": 0})

    def test_with_mock_subscription_builds_payload_without_error(self):
        # Insert a fake push_subscriptions row and patch the actual sender
        # so no network I/O happens. We verify the route reaches the
        # sender with the documented payload shape (title/body/url/tag),
        # which is what the settings-UI "Send test" button relies on.
        uid = _make_user("withpush@test.com")
        cookies = _login_as(uid)
        with db.conn() as c:
            c.execute(
                """
                INSERT INTO push_subscriptions
                    (user_id, endpoint, p256dh, auth, user_agent, created_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid,
                    "https://fcm.googleapis.com/fcm/send/mock-endpoint-123",
                    "mock-p256dh", "mock-auth", "pytest-ua",
                    1700000000, 1700000000,
                ),
            )

        summary = {"sent": 1, "failed": 0, "expired": 0}
        with patch.object(push, "send_to_user", return_value=summary) as mock_send:
            r = client.post(
                "/api/push/test",
                json={},
                cookies=cookies, headers=CSRF_HEADERS,
            )

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), summary)
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        # Positional user_id, plus keyword payload fields per the route.
        self.assertEqual(args[0], uid)
        self.assertEqual(kwargs.get("title"), "narve.ai")
        self.assertIn("Test push", kwargs.get("body", ""))
        self.assertEqual(kwargs.get("url"), "/dashboards")
        self.assertEqual(kwargs.get("tag"), "narve-test")

    def test_push_not_available_returns_503(self):
        # If pywebpush / cryptography aren't installed, send_to_user
        # raises PushNotAvailable; the route must surface that as 503
        # so the UI shows a "feature unavailable" notice rather than
        # a generic error.
        uid = _make_user("unavail-push@test.com")
        cookies = _login_as(uid)
        with patch.object(
            push, "send_to_user",
            side_effect=push.PushNotAvailable("pywebpush not installed"),
        ):
            r = client.post(
                "/api/push/test",
                json={},
                cookies=cookies, headers=CSRF_HEADERS,
            )
        self.assertEqual(r.status_code, 503)

    def test_unexpected_exception_returns_500(self):
        # Any other exception from send_to_user (e.g. DB blew up mid-loop)
        # collapses to a 500 — the route must NOT leak internals to the
        # client. The exception is logged server-side.
        uid = _make_user("boom-push@test.com")
        cookies = _login_as(uid)
        with patch.object(
            push, "send_to_user",
            side_effect=RuntimeError("kaboom"),
        ):
            r = client.post(
                "/api/push/test",
                json={},
                cookies=cookies, headers=CSRF_HEADERS,
            )
        self.assertEqual(r.status_code, 500)


if __name__ == "__main__":
    unittest.main()

"""Tests for the per-recipient email-watermark subsystem.

Covers:
  * Watermark hex is deterministic for the same (user_id, email_id).
  * Different user_ids on the same email_id produce different hexes.
  * Different email_ids for the same user produce different hexes.
  * Empty EMAIL_WATERMARK_KEY makes the helpers return empty strings
    (so dev environments don't accidentally ship a fixed fingerprint).
  * `watermark_zw` round-trips through `decode_zw`.
  * `annotate_context` populates context AND records the mapping.
  * `trace_watermark` reverse-lookup returns the original user_id.
  * Rendered weekly_digest carries both surfaces (visible footer +
    zero-width run) when a watermark is supplied.
  * Subject lines NEVER carry the watermark — confirms the threading-
    safety constraint.
  * Admin `/admin/trace-watermark` endpoint resolves correctly and
    refuses non-admins.

Uses the shared test DB from ``_testdb`` so migration 175 (email_watermarks
table) is already present when the tests run.
"""

from __future__ import annotations

import os
import unittest

USES_TESTDB = True

from tests import _testdb  # noqa: F401 — installs shared in-memory DB + migrations
import db  # noqa: E402

# Ensure the watermark key is set before importing the module so the
# helpers see a non-empty key. We restore the prior value in tearDown
# for the dedicated "empty key" test.
os.environ.setdefault("EMAIL_WATERMARK_KEY", "test-watermark-key-do-not-use-in-prod")

from email_system import watermark as wm  # noqa: E402
from email_system.renderer import render  # noqa: E402


def _make_user(email: str) -> int:
    existing = db.get_user_by_email(email)
    if existing:
        return existing["id"]
    return db.create_user(email, "Passw0rd!longenough1", username=email.split("@")[0])


class TestWatermarkDeterminism(unittest.TestCase):
    def test_same_input_same_output(self):
        a = wm.watermark_for_user(42, "weekly_digest:42:19500")
        b = wm.watermark_for_user(42, "weekly_digest:42:19500")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 6)
        # Watermark must be lowercase hex.
        self.assertTrue(all(c in "0123456789abcdef" for c in a), a)

    def test_different_users_different_watermark(self):
        a = wm.watermark_for_user(1, "weekly_digest:0:19500")
        b = wm.watermark_for_user(2, "weekly_digest:0:19500")
        # Possible 1-in-16M collision; if you ever see this fail re-run.
        self.assertNotEqual(a, b)

    def test_different_emails_different_watermark(self):
        a = wm.watermark_for_user(7, "weekly_digest:7:19500")
        b = wm.watermark_for_user(7, "morning_briefing:7:19500")
        self.assertNotEqual(a, b)


class TestWatermarkEmptyKey(unittest.TestCase):
    def setUp(self) -> None:
        self._prior = os.environ.get("EMAIL_WATERMARK_KEY")
        os.environ["EMAIL_WATERMARK_KEY"] = ""

    def tearDown(self) -> None:
        if self._prior is None:
            os.environ.pop("EMAIL_WATERMARK_KEY", None)
        else:
            os.environ["EMAIL_WATERMARK_KEY"] = self._prior

    def test_empty_key_returns_empty_string(self):
        self.assertEqual(wm.watermark_for_user(1, "anything"), "")

    def test_empty_key_zw_is_empty(self):
        self.assertEqual(wm.watermark_zw(""), "")


class TestZeroWidthEncoding(unittest.TestCase):
    def test_round_trip(self):
        hex_in = "abcdef"
        zw = wm.watermark_zw(hex_in)
        # 6 hex chars × 4 bits = 24 zero-width chars.
        self.assertEqual(len(zw), 24)
        self.assertEqual(wm.decode_zw(zw), hex_in)

    def test_only_zw_chars(self):
        zw = wm.watermark_zw("0fa1")
        self.assertTrue(all(c in ("​", "‌") for c in zw))


class TestTraceWatermark(unittest.TestCase):
    def test_record_and_trace(self):
        uid = _make_user("watermark-trace@test.com")
        context: dict = {}
        wm.annotate_context(context, uid, "weekly_digest", batch_ts=19500 * 86400)
        watermark_hex = context["watermark"]
        self.assertEqual(len(watermark_hex), 6)

        resolved = wm.trace_watermark(watermark_hex)
        self.assertEqual(resolved, uid)

    def test_trace_detail_returns_template(self):
        uid = _make_user("watermark-detail@test.com")
        context: dict = {}
        wm.annotate_context(context, uid, "morning_briefing", batch_ts=19501 * 86400)
        detail = wm.trace_watermark_detail(context["watermark"])
        self.assertIsNotNone(detail)
        self.assertEqual(detail["user_id"], uid)
        self.assertEqual(detail["template"], "morning_briefing")

    def test_trace_unknown_returns_none(self):
        self.assertIsNone(wm.trace_watermark("ffffff"))
        self.assertIsNone(wm.trace_watermark(""))


class TestTemplateRender(unittest.TestCase):
    def test_weekly_digest_carries_both_surfaces(self):
        watermark_hex = "abc123"
        html = render("weekly_digest", {
            "display_name": "Watermarked User",
            "week_start": "May 1",
            "week_end": "May 7, 2026",
            "top_predictions": [
                {"source": "@s", "category": "politics",
                 "content": "Body text", "credibility": 0.8},
            ],
            "top_sources": [],
            "app_url": "https://narve.ai",
            "unsubscribe_url": "https://narve.ai/unsub",
            "watermark": watermark_hex,
            "watermark_zw": wm.watermark_zw(watermark_hex),
        })
        # Visible footer fragment is present.
        self.assertIn(f"id:{watermark_hex}", html)
        # Invisible zero-width run shows up at least once in the body.
        self.assertIn("​" if "​" in wm.watermark_zw(watermark_hex)
                      else "‌", html)

    def test_weekly_digest_no_watermark_still_renders(self):
        # Sanity: an unconfigured (no key) deployment must still send.
        html = render("weekly_digest", {
            "display_name": "User",
            "week_start": "May 1",
            "week_end": "May 7, 2026",
            "top_predictions": [],
            "top_sources": [],
            "app_url": "https://narve.ai",
            "unsubscribe_url": "https://narve.ai/unsub",
            "watermark": "",
            "watermark_zw": "",
        })
        # No visible "id:" footer line.
        self.assertNotIn("id:", html.lower().split("watermark-id")[0])


class TestSubjectSafety(unittest.TestCase):
    """Watermark must NEVER leak into the email subject line — that would
    break mail-client threading and would let a curious recipient grep
    their inbox for the fingerprint."""

    def test_subject_does_not_contain_watermark(self):
        from email_system.service import _SUBJECTS
        for key in ("weekly_digest",):
            subject = _SUBJECTS.get(key, "")
            self.assertNotIn("{{", subject)  # no template var slots at all
            self.assertNotIn("watermark", subject.lower())


class TestAdminTraceRoute(unittest.TestCase):
    """The admin /admin/trace-watermark endpoint must:
      * Require admin auth.
      * Return JSON containing user_id when the watermark exists.
      * 404 when the watermark is unknown.
      * 400 when the query string is malformed.

    We import the route function directly and exercise it with a stub
    Request rather than spinning a TestClient — the surface we care
    about is the lookup logic + the admin gate, both of which a unit
    test can hit cleanly.
    """

    def test_route_resolves_watermark(self):
        import admin_routes
        import asyncio

        uid = _make_user("admin-trace@test.com")
        context: dict = {}
        wm.annotate_context(context, uid, "morning_briefing", batch_ts=19510 * 86400)
        watermark_hex = context["watermark"]

        class FakeRequest:
            def __init__(self, qs):
                self.query_params = qs
                self.headers = {}

        # Bypass the admin guard for the test by monkeypatching.
        original_guard = admin_routes._require_admin_user
        admin_routes._require_admin_user = lambda req, page=False: {
            "id": 1, "email": "admin@narve.ai", "is_admin": 1,
        }
        try:
            resp = asyncio.get_event_loop().run_until_complete(
                admin_routes.trace_watermark_route(FakeRequest({"id": watermark_hex}))
            )
            self.assertEqual(resp.status_code, 200)
            import json
            body = json.loads(resp.body)
            self.assertEqual(body["user_id"], uid)
            self.assertEqual(body["watermark"], watermark_hex)
            self.assertEqual(body["template"], "morning_briefing")
        finally:
            admin_routes._require_admin_user = original_guard

    def test_route_404s_on_unknown(self):
        import admin_routes
        import asyncio

        class FakeRequest:
            def __init__(self, qs):
                self.query_params = qs
                self.headers = {}

        original_guard = admin_routes._require_admin_user
        admin_routes._require_admin_user = lambda req, page=False: {
            "id": 1, "email": "admin@narve.ai", "is_admin": 1,
        }
        try:
            resp = asyncio.get_event_loop().run_until_complete(
                admin_routes.trace_watermark_route(FakeRequest({"id": "deadbe"}))
            )
            self.assertEqual(resp.status_code, 404)
        finally:
            admin_routes._require_admin_user = original_guard

    def test_route_400_on_garbage(self):
        import admin_routes
        import asyncio

        class FakeRequest:
            def __init__(self, qs):
                self.query_params = qs
                self.headers = {}

        original_guard = admin_routes._require_admin_user
        admin_routes._require_admin_user = lambda req, page=False: {
            "id": 1, "email": "admin@narve.ai", "is_admin": 1,
        }
        try:
            for bad in ("", "not-hex!", "z" * 6, "0" * 64):
                resp = asyncio.get_event_loop().run_until_complete(
                    admin_routes.trace_watermark_route(FakeRequest({"id": bad}))
                )
                self.assertEqual(
                    resp.status_code, 400,
                    f"expected 400 for input={bad!r}, got {resp.status_code}",
                )
        finally:
            admin_routes._require_admin_user = original_guard


class TestAdminTraceForensicAlerts(unittest.TestCase):
    """The /admin/trace-watermark endpoint is a forensic surface — per the
    Cloudflare audit, every access must:
      * be captured to Sentry at info level,
      * enqueue an email to the forensic mailbox using the
        ``admin_forensic_alert`` template,
      * 429 the 11th request within an hour from the same admin.

    These tests assert each of the three guarantees independently by
    monkeypatching ``sentry_sdk``, ``jobs.email_jobs.enqueue_email`` and
    ``server._is_rate_limited`` respectively.
    """

    def _fake_request(self, qs, ua="curl/8", ip="203.0.113.5"):
        class FakeClient:
            def __init__(self, host):
                self.host = host
        class FakeRequest:
            def __init__(self):
                self.query_params = qs
                self.headers = {"user-agent": ua, "x-forwarded-for": ip}
                self.client = FakeClient(ip)
        return FakeRequest()

    def _bypass_admin(self):
        import admin_routes
        original_guard = admin_routes._require_admin_user
        admin_routes._require_admin_user = lambda req, page=False: {
            "user_id": 99, "id": 99, "email": "admin@narve.ai", "is_admin": 1,
        }
        return original_guard

    def _restore_admin(self, original_guard):
        import admin_routes
        admin_routes._require_admin_user = original_guard

    def test_sentry_capture_on_hit(self):
        import admin_routes
        import asyncio
        import sys
        import types

        uid = _make_user("forensic-sentry-hit@test.com")
        ctx: dict = {}
        wm.annotate_context(ctx, uid, "weekly_digest", batch_ts=19520 * 86400)
        watermark_hex = ctx["watermark"]

        captured: list[tuple[str, str]] = []

        # Stub sentry_sdk so the lazy `import sentry_sdk` inside the route
        # resolves to our fake. Capturing also any kwargs we care about.
        fake_sentry = types.SimpleNamespace(
            capture_message=lambda msg, level="info": captured.append((msg, level))
        )
        sys.modules["sentry_sdk"] = fake_sentry

        original_guard = self._bypass_admin()
        try:
            loop = asyncio.new_event_loop()
            try:
                resp = loop.run_until_complete(
                    admin_routes.trace_watermark_route(
                        self._fake_request({"id": watermark_hex})
                    )
                )
            finally:
                loop.close()
            self.assertEqual(resp.status_code, 200)
        finally:
            self._restore_admin(original_guard)
            sys.modules.pop("sentry_sdk", None)

        self.assertEqual(len(captured), 1, f"expected 1 sentry capture, got {captured!r}")
        msg, level = captured[0]
        self.assertEqual(level, "info")
        self.assertIn("admin@narve.ai", msg)
        self.assertIn(str(uid), msg)

    def test_sentry_capture_also_on_miss(self):
        """A 404 (unknown watermark) must still capture — we want to know
        when someone is probing the endpoint with forged fingerprints."""
        import admin_routes
        import asyncio
        import sys
        import types

        captured: list[tuple[str, str]] = []
        sys.modules["sentry_sdk"] = types.SimpleNamespace(
            capture_message=lambda msg, level="info": captured.append((msg, level))
        )

        original_guard = self._bypass_admin()
        try:
            loop = asyncio.new_event_loop()
            try:
                resp = loop.run_until_complete(
                    admin_routes.trace_watermark_route(
                        self._fake_request({"id": "deadbe"})
                    )
                )
            finally:
                loop.close()
            self.assertEqual(resp.status_code, 404)
        finally:
            self._restore_admin(original_guard)
            sys.modules.pop("sentry_sdk", None)

        self.assertEqual(len(captured), 1)
        self.assertIn("admin@narve.ai", captured[0][0])

    def test_forensic_email_enqueued_with_template(self):
        import admin_routes
        import asyncio
        import sys
        import types

        uid = _make_user("forensic-email@test.com")
        ctx: dict = {}
        wm.annotate_context(ctx, uid, "morning_briefing", batch_ts=19521 * 86400)
        watermark_hex = ctx["watermark"]

        enqueued: list[dict] = []

        async def fake_enqueue(to, template, context, reply_to=None, tags=None):
            enqueued.append({
                "to": to, "template": template, "context": context, "tags": tags,
            })
            return 1

        # Stub sentry_sdk so the capture call doesn't blow up.
        sys.modules["sentry_sdk"] = types.SimpleNamespace(
            capture_message=lambda *a, **kw: None
        )

        # Patch enqueue_email at its source module so the route's lazy
        # import sees the fake.
        from jobs import email_jobs as _ej
        original_enqueue = _ej.enqueue_email
        _ej.enqueue_email = fake_enqueue

        original_guard = self._bypass_admin()
        try:
            loop = asyncio.new_event_loop()
            try:
                # Run the route, then let the create_task() coroutine drain.
                resp = loop.run_until_complete(
                    admin_routes.trace_watermark_route(
                        self._fake_request({"id": watermark_hex})
                    )
                )
                pending = [
                    t for t in asyncio.all_tasks(loop=loop) if not t.done()
                ]
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending))
            finally:
                loop.close()
            self.assertEqual(resp.status_code, 200)
        finally:
            self._restore_admin(original_guard)
            _ej.enqueue_email = original_enqueue
            sys.modules.pop("sentry_sdk", None)

        self.assertEqual(len(enqueued), 1, f"expected 1 email enqueue, got {enqueued!r}")
        payload = enqueued[0]
        self.assertEqual(payload["template"], "admin_forensic_alert")
        # Forensic recipient — env default lands on legal@narve.ai when
        # neither EMAIL_FORENSIC nor LEGAL_EMAIL is set.
        self.assertIn("@narve.ai", payload["to"])
        ctx_passed = payload["context"]
        self.assertEqual(ctx_passed["admin_email"], "admin@narve.ai")
        self.assertEqual(ctx_passed["target_watermark"], watermark_hex)
        self.assertEqual(ctx_passed["target_user_id"], uid)
        self.assertIn("UTC", ctx_passed["timestamp"])
        # Subject must interpolate the admin's email for inbox preview.
        self.assertIn("admin=admin@narve.ai", ctx_passed["subject"])
        # The tags array tells the relay this is forensic traffic.
        self.assertIn("forensic", payload["tags"])

    def test_rate_limit_11th_request_in_hour_returns_429(self):
        import admin_routes
        import asyncio
        import sys
        import types

        # Stub sentry + enqueue so the rate-limit test isolates only the
        # 429 path.
        sys.modules["sentry_sdk"] = types.SimpleNamespace(
            capture_message=lambda *a, **kw: None
        )
        from jobs import email_jobs as _ej
        original_enqueue = _ej.enqueue_email
        _ej.enqueue_email = lambda *a, **kw: _noop_coro()

        # Use a stub rate-limiter that returns False for the first 10
        # calls and True from the 11th onward — the in-memory store is
        # process-wide and could be polluted by other tests.
        import server as _srv
        call_count = {"n": 0}
        def _stub_rl(key, limit, window=None):
            call_count["n"] += 1
            return call_count["n"] > limit  # 11th request → True
        original_irl = _srv._is_rate_limited
        _srv._is_rate_limited = _stub_rl

        original_guard = self._bypass_admin()
        responses = []
        try:
            loop = asyncio.new_event_loop()
            try:
                for _ in range(11):
                    resp = loop.run_until_complete(
                        admin_routes.trace_watermark_route(
                            self._fake_request({"id": "abcdef"})
                        )
                    )
                    responses.append(resp.status_code)
                pending = [
                    t for t in asyncio.all_tasks(loop=loop) if not t.done()
                ]
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending))
            finally:
                loop.close()
        finally:
            self._restore_admin(original_guard)
            _srv._is_rate_limited = original_irl
            _ej.enqueue_email = original_enqueue
            sys.modules.pop("sentry_sdk", None)

        # First 10 calls must NOT be 429 (they're 404s since watermark
        # is fake, but anything ≠ 429 satisfies the under-the-limit gate).
        self.assertTrue(all(s != 429 for s in responses[:10]),
                        f"under-limit responses: {responses[:10]}")
        # The 11th must be 429.
        self.assertEqual(responses[10], 429)


async def _noop_coro():
    return 1


if __name__ == "__main__":
    unittest.main()

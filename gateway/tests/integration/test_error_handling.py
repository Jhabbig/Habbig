"""Tests for gateway.error_handlers + common/circuit_breaker + common/retry.

The handlers are tested by invoking them directly against synthetic
Request + exception objects. We can't mount probe routes on the global
`server.app` because server.py registers a greedy catch-all before
this module imports — that route wins over anything we add here.
Invoking the handlers by hand is actually tighter: we exercise the
exact input shape they receive at runtime without middleware drift.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

os.environ.setdefault("SITE_ACCESS_TOKEN", "test_token_48_chars_aaaaaaaaaaaaaaaaaaaaaaaaaaaa")
os.environ.setdefault("EMAIL_DRY_RUN", "true")

from tests import _testdb  # noqa: F401,E402 — shared in-memory DB + migrations

from fastapi import Request  # noqa: E402
from starlette.exceptions import HTTPException  # noqa: E402

import error_handlers as eh  # noqa: E402
from common.circuit_breaker import CircuitBreaker, CircuitOpen  # noqa: E402
from common.retry import retry, RetryAfter, raise_for_retry_after  # noqa: E402


def _mk_request(path: str = "/", *, accept: str = "text/html", headers: dict | None = None) -> Request:
    """Build a synthetic Request for the handlers to operate on."""
    raw_headers = [
        (b"host", b"testserver"),
        (b"accept", accept.encode()),
    ]
    for k, v in (headers or {}).items():
        raw_headers.append((k.lower().encode(), v.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "headers": raw_headers,
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "state": {},
    }
    req = Request(scope)
    # Mimic what RequestIDMiddleware normally does.
    req.state.request_id = "testrid1"
    return req


async def _await(coro):
    return await coro


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── JSON envelope tests ──────────────────────────────────────────────


class TestJsonHttpExceptionEnvelope(unittest.TestCase):
    def test_404_envelope_shape(self):
        req = _mk_request("/api/widgets/42", accept="application/json")
        exc = HTTPException(status_code=404, detail="widget not found")
        resp = _run(eh.http_exception_handler(req, exc))
        self.assertEqual(resp.status_code, 404)
        import json
        body = json.loads(resp.body.decode())
        self.assertEqual(body["error"], "resource_not_found")
        self.assertEqual(body["message"], "widget not found")
        self.assertEqual(body["request_id"], "testrid1")

    def test_trace_looking_detail_is_scrubbed(self):
        req = _mk_request("/api/users", accept="application/json")
        exc = HTTPException(
            status_code=403,
            detail="sqlite3.IntegrityError: UNIQUE constraint failed on users.email",
        )
        resp = _run(eh.http_exception_handler(req, exc))
        import json
        body = json.loads(resp.body.decode())
        self.assertEqual(body["error"], "authorization_required")
        self.assertNotIn("IntegrityError", body["message"])
        self.assertNotIn("sqlite3", body["message"])
        self.assertNotIn("UNIQUE", body["message"])

    def test_429_retry_after_passes_through(self):
        req = _mk_request("/api/do", accept="application/json")
        exc = HTTPException(status_code=429, detail="slow down", headers={"Retry-After": "17"})
        resp = _run(eh.http_exception_handler(req, exc))
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.headers.get("Retry-After"), "17")
        import json
        body = json.loads(resp.body.decode())
        self.assertEqual(body["error"], "rate_limit_exceeded")


class TestJsonAppExceptionEnvelope(unittest.TestCase):
    def test_unhandled_exception_scrubbed_to_generic_500(self):
        req = _mk_request("/api/thing", accept="application/json")
        exc = RuntimeError("internal boom detail that must not leak")
        resp = _run(eh.app_exception_handler(req, exc))
        self.assertEqual(resp.status_code, 500)
        import json
        body = json.loads(resp.body.decode())
        self.assertEqual(body["error"], "internal_error")
        self.assertNotIn("boom", body["message"].lower())
        self.assertNotIn("RuntimeError", body["message"])
        self.assertEqual(body["request_id"], "testrid1")


class TestValidationEnvelope(unittest.TestCase):
    def test_pydantic_errors_yield_per_field_details(self):
        from fastapi.exceptions import RequestValidationError
        from pydantic import ValidationError, BaseModel

        class _Body(BaseModel):
            email: str
            age: int

        try:
            _Body(email="x", age="abc")  # type: ignore[arg-type]
            self.fail("should have raised")
        except ValidationError as exc:
            # Wrap as RequestValidationError so the handler sees the
            # shape it would receive from FastAPI.
            req_exc = RequestValidationError(errors=exc.errors())

        req = _mk_request("/api/submit", accept="application/json")
        resp = _run(eh.validation_exception_handler(req, req_exc))
        self.assertEqual(resp.status_code, 422)
        import json
        body = json.loads(resp.body.decode())
        self.assertEqual(body["error"], "validation_failed")
        self.assertIn("errors", body["details"])
        fields = {e["field"] for e in body["details"]["errors"]}
        # `age` field must surface.
        self.assertIn("age", fields)


# ── HTML error pages ─────────────────────────────────────────────────


class TestHtmlErrorPage(unittest.TestCase):
    def test_render_404_injects_request_id_and_title(self):
        req = _mk_request("/gone")
        req.state.request_id = "feedface"
        resp = eh.render_error_page(req, status=404)
        self.assertEqual(resp.status_code, 404)
        body = resp.body.decode()
        # The 404 page intentionally omits the request id (no
        # ops-actionable support channel for a "page not found").
        self.assertIn("Not found", body)
        # Context-appropriate action.
        self.assertIn('href="/dashboards"', body)
        # No stack trace or module name.
        self.assertNotIn("Traceback", body)
        self.assertNotIn("RuntimeError", body)

    def test_render_500_is_generic(self):
        req = _mk_request("/explode")
        resp = eh.render_error_page(req, status=500)
        body = resp.body.decode()
        self.assertIn("Something broke", body)
        self.assertIn("Retry", body)

    def test_render_429_includes_retry_line(self):
        req = _mk_request("/api/x")
        resp = eh.render_error_page(req, status=429, retry_after=17)
        self.assertEqual(resp.headers.get("Retry-After"), "17")
        self.assertIn("17 seconds", resp.body.decode())

    def test_render_402_includes_pricing_link(self):
        req = _mk_request("/paywall")
        resp = eh.render_error_page(req, status=402)
        body = resp.body.decode()
        self.assertIn("Subscription required", body)
        # The CTA points at /pricing (where the user actually purchases)
        # rather than /billing (which 404s for unauth visitors).
        self.assertIn("/pricing", body)

    def test_render_503_links_to_status_page(self):
        req = _mk_request("/overload")
        resp = eh.render_error_page(req, status=503)
        body = resp.body.decode()
        self.assertIn("Temporarily down", body)
        self.assertIn("/status", body)

    def test_html_escapes_potentially_hostile_title(self):
        req = _mk_request("/inj")
        resp = eh.render_error_page(req, status=404, title="<script>x</script>")
        body = resp.body.decode()
        self.assertNotIn("<script>x</script>", body)
        self.assertIn("&lt;script&gt;", body)


# ── Content negotiation ──────────────────────────────────────────────


class TestContentNegotiation(unittest.TestCase):
    def test_is_api_request_api_prefix(self):
        req = _mk_request("/api/widgets")
        self.assertTrue(eh.is_api_request(req))

    def test_is_api_request_json_accept(self):
        req = _mk_request("/foo", accept="application/json")
        self.assertTrue(eh.is_api_request(req))

    def test_is_api_request_html_accept(self):
        req = _mk_request("/foo", accept="text/html,application/xhtml+xml")
        self.assertFalse(eh.is_api_request(req))


# ── Status → slug + message surface area ────────────────────────────


class TestStatusMappings(unittest.TestCase):
    def test_every_common_status_maps_to_a_slug(self):
        for status in (400, 401, 402, 403, 404, 409, 422, 429, 500, 502, 503, 504):
            self.assertIn(status, eh._STATUS_TO_SLUG)
            self.assertIn(status, eh._STATUS_TO_TITLE)
            self.assertIn(status, eh._STATUS_TO_MESSAGE)

    def test_slug_for_unknown_status_is_generic(self):
        self.assertEqual(eh.slug_for_status(418), "error")

    def test_generic_safe_messages_never_reference_internals(self):
        for _, msg in eh._STATUS_TO_MESSAGE.items():
            for banned in ("Traceback", "IntegrityError", "sqlite3", "psycopg", "OperationalError"):
                self.assertNotIn(banned, msg)

    def test_looks_like_trace_catches_sql_chatter(self):
        self.assertTrue(eh._looks_like_trace("sqlite3.IntegrityError"))
        self.assertTrue(eh._looks_like_trace("UNIQUE constraint failed"))
        self.assertTrue(eh._looks_like_trace("a" * 300))
        self.assertFalse(eh._looks_like_trace("Item not found."))


# ── Request-ID helper ────────────────────────────────────────────────


class TestRequestIDSurface(unittest.TestCase):
    def test_get_request_id_reuses_existing(self):
        req = _mk_request("/x")
        req.state.request_id = "already-set"
        self.assertEqual(eh.get_request_id(req), "already-set")

    def test_get_request_id_mints_when_absent(self):
        req = _mk_request("/x")
        # Clear state.
        try:
            delattr(req.state, "request_id")
        except Exception:
            pass
        rid = eh.get_request_id(req)
        self.assertEqual(len(rid), 8)
        # Subsequent call returns the same id.
        self.assertEqual(eh.get_request_id(req), rid)

    def test_generate_request_id_format(self):
        rid = eh.generate_request_id()
        self.assertEqual(len(rid), 8)
        # hex-only
        int(rid, 16)


# ── Circuit breaker ──────────────────────────────────────────────────


class TestCircuitBreaker(unittest.TestCase):
    def test_breaker_opens_after_threshold_failures_and_recovers(self):
        cb = CircuitBreaker(name="t1", failure_threshold=3, recovery_timeout=0.1)
        for _ in range(3):
            self.assertTrue(cb.can_call())
            cb.record_failure()
        self.assertEqual(cb.state, "open")
        self.assertFalse(cb.can_call())
        self.assertGreaterEqual(cb.rejected_count, 1)
        time.sleep(0.12)
        self.assertTrue(cb.can_call())  # probe lane
        self.assertFalse(cb.can_call())
        cb.record_success()
        self.assertEqual(cb.state, "closed")
        self.assertTrue(cb.can_call())

    def test_breaker_half_open_probe_failure_reopens(self):
        cb = CircuitBreaker(name="t2", failure_threshold=2, recovery_timeout=0.1)
        cb.record_failure(); cb.record_failure()
        self.assertEqual(cb.state, "open")
        time.sleep(0.12)
        self.assertTrue(cb.can_call())  # probe
        cb.record_failure()
        self.assertEqual(cb.state, "open")

    def test_wrap_decorator_raises_circuitopen_when_open(self):
        cb = CircuitBreaker(name="t3", failure_threshold=1, recovery_timeout=5)
        calls = {"n": 0}

        @cb.wrap()
        def flaky():
            calls["n"] += 1
            raise RuntimeError("upstream down")

        with self.assertRaises(RuntimeError):
            flaky()
        with self.assertRaises(CircuitOpen):
            flaky()
        self.assertEqual(calls["n"], 1)

    def test_wrap_decorator_async(self):
        cb = CircuitBreaker(name="t4", failure_threshold=2, recovery_timeout=5)

        @cb.wrap()
        async def flaky_async():
            raise TimeoutError("slow")

        async def drive():
            for _ in range(2):
                try:
                    await flaky_async()
                except TimeoutError:
                    pass
            with self.assertRaises(CircuitOpen):
                await flaky_async()

        _run(drive())

    def test_reset_clears_state(self):
        cb = CircuitBreaker(name="t5", failure_threshold=1, recovery_timeout=5)
        cb.record_failure()
        self.assertEqual(cb.state, "open")
        cb.reset()
        self.assertEqual(cb.state, "closed")
        self.assertTrue(cb.can_call())


class TestNamedBreakers(unittest.TestCase):
    def test_all_five_upstreams_have_breakers(self):
        from common.circuit_breaker import (
            claude_breaker, stripe_breaker, polymarket_breaker,
            kalshi_breaker, sec_edgar_breaker, all_breakers,
        )
        names = {b.name for b in all_breakers()}
        self.assertEqual(
            names,
            {"claude", "stripe", "polymarket", "kalshi", "sec_edgar"},
        )


# ── Retry helper ─────────────────────────────────────────────────────


class TestRetryHelper(unittest.TestCase):
    def test_sync_retry_succeeds_on_second_attempt(self):
        attempts = {"n": 0}

        @retry(stop_after_attempt=3, wait_exponential_min=0.01,
               wait_exponential_max=0.01, retry_on=(ConnectionError,))
        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise ConnectionError("transient")
            return "ok"

        self.assertEqual(flaky(), "ok")
        self.assertEqual(attempts["n"], 2)

    def test_sync_retry_reraises_after_exhaustion(self):
        @retry(stop_after_attempt=2, wait_exponential_min=0.01,
               wait_exponential_max=0.01, retry_on=(ConnectionError,))
        def always_broken():
            raise ConnectionError("persistent")

        with self.assertRaises(ConnectionError):
            always_broken()

    def test_non_retryable_exception_is_raised_immediately(self):
        attempts = {"n": 0}

        @retry(stop_after_attempt=5, wait_exponential_min=0.01,
               wait_exponential_max=0.01, retry_on=(ConnectionError,))
        def client_bug():
            attempts["n"] += 1
            raise ValueError("not retryable")

        with self.assertRaises(ValueError):
            client_bug()
        self.assertEqual(attempts["n"], 1)

    def test_retry_after_exception_waits_for_specified_seconds(self):
        attempts = {"n": 0}

        @retry(stop_after_attempt=3, wait_exponential_min=0.5,
               wait_exponential_max=1.0, retry_on=(ConnectionError,))
        def rate_limited():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RetryAfter(seconds=0.05, reason="rate limited")
            return "ok"

        start = time.time()
        self.assertEqual(rate_limited(), "ok")
        elapsed = time.time() - start
        self.assertLess(elapsed, 0.4, f"waited too long: {elapsed:.2f}s")

    def test_raise_for_retry_after_helper(self):
        class _R:
            status_code = 429
            headers = {"Retry-After": "3.5"}

        with self.assertRaises(RetryAfter) as ctx:
            raise_for_retry_after(_R())
        self.assertAlmostEqual(ctx.exception.seconds, 3.5, places=2)

    def test_raise_for_retry_after_noop_on_2xx(self):
        class _R:
            status_code = 200
            headers = {}

        raise_for_retry_after(_R())  # must not raise

    def test_async_retry_succeeds_on_second_attempt(self):
        attempts = {"n": 0}

        @retry(stop_after_attempt=3, wait_exponential_min=0.01,
               wait_exponential_max=0.01, retry_on=(ConnectionError,))
        async def flaky_a():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise ConnectionError("transient")
            return "ok"

        out = _run(flaky_a())
        self.assertEqual(out, "ok")
        self.assertEqual(attempts["n"], 2)


if __name__ == "__main__":
    unittest.main()

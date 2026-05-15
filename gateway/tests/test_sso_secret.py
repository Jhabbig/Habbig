"""Tests for the GATEWAY_SSO_SECRET fail-closed guard.

Audit finding (HIGH): when ``GATEWAY_SSO_SECRET`` was unset, the dashboard
proxy at ``server.proxy_request`` silently dropped ``X-Gateway-Secret`` from
forwarded requests. If the downstream subproduct service was also
mis-configured with an empty secret, ``hmac.compare_digest("", "")`` returns
True and accepts unauthenticated traffic — full SSO bypass.

These tests assert that:

1. ``server.GATEWAY_SSO_SECRET`` is exposed as a module constant so the
   startup checks can reference it (instead of re-reading os.environ each
   time and risking drift between paths).
2. The proxy refuses to forward when the secret is empty — it returns 401
   *before* any ``hmac.compare_digest`` ever runs downstream, so the empty
   token can never be the input on either side of the compare.
3. With a valid secret, the proxy actually stamps ``X-Gateway-Secret`` on
   forwarded requests (regression guard for the silent-drop pattern).
"""

from __future__ import annotations

import asyncio
import hmac
import os
import sys
import unittest
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import server  # noqa: E402 — sys.path tweak above is intentional


# ── Helpers ────────────────────────────────────────────────────────────────


def _run(coro):
    """Run an awaitable in a fresh event loop. Kept synchronous so unittest
    discovery treats it like every other test in the suite (avoids the
    asyncio_mode=strict requirement that pytest-asyncio would impose on
    async def test_… functions)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeURL:
    def __init__(self, path: str = "/", query: str = "", scheme: str = "https"):
        self.path = path
        self.query = query
        self.scheme = scheme


class _FakeRequest:
    """Minimal stand-in for starlette.Request. Only exposes the surface
    ``proxy_request`` reads — headers, url, method, body, state, client."""

    def __init__(self, *, host: str, path: str = "/", query: str = ""):
        self.headers = {"host": host}
        self.url = _FakeURL(path=path, query=query)
        self.method = "GET"
        self.state = type("S", (), {})()

        class _Client:
            def __init__(self):
                self.host = "127.0.0.1"

        self.client = _Client()

    async def body(self):
        return b""


def _first_dashboard_key_and_subdomain() -> tuple[str, str]:
    """Return any (dashboard_key, subdomain) pair the server knows about.
    The proxy uses ``SUBDOMAIN_TO_KEY`` to resolve the dashboard; the test
    is agnostic to which one — we just need a valid value."""
    sub, key = next(iter(server.SUBDOMAIN_TO_KEY.items()))
    return key, sub


# ── Tests ──────────────────────────────────────────────────────────────────


class TestSSOSecretConstant(unittest.TestCase):
    """The module-level constant must exist so startup checks can use it."""

    def test_constant_is_a_string(self):
        self.assertIsInstance(server.GATEWAY_SSO_SECRET, str)

    def test_constant_reads_from_env_at_import(self):
        # The constant is computed once from os.environ at import time.
        # If GATEWAY_SSO_SECRET was set in the environment when server was
        # imported, the constant must match it. Otherwise it must be "".
        env_value = os.environ.get("GATEWAY_SSO_SECRET", "")
        self.assertEqual(server.GATEWAY_SSO_SECRET, env_value)


class TestProxyRefusesEmptySecret(unittest.TestCase):
    """The fail-closed guard must reject the request *before* any compare
    runs downstream — i.e. ``hmac.compare_digest("", "")`` is unreachable."""

    def test_proxy_returns_401_when_secret_empty(self):
        key, sub = _first_dashboard_key_and_subdomain()
        host = f"{sub}.narve.ai"
        req = _FakeRequest(host=host)

        fake_user = {"user_id": 1, "email": "u@test.example", "is_admin": True}

        with patch.object(server, "GATEWAY_SSO_SECRET", ""), \
                patch.dict(os.environ, {"GATEWAY_SSO_SECRET": ""}, clear=False), \
                patch.object(server, "current_user", return_value=fake_user), \
                patch.object(server, "get_subdomain", return_value=sub), \
                patch.object(server, "_request_apex", return_value="narve.ai"):
            response = _run(server.proxy_request(req))

        self.assertEqual(response.status_code, 401)
        # Sanity: the misconfig message must surface so ops can diagnose
        # the failure without attaching a debugger.
        body = response.body if isinstance(response.body, bytes) else b""
        self.assertIn(b"SSO secret", body)

    def test_proxy_returns_401_even_when_production_off(self):
        """Fail-closed applies regardless of IS_PRODUCTION — the
        compare-digest bypass exists in dev too, so dev must reject."""
        key, sub = _first_dashboard_key_and_subdomain()
        host = f"{sub}.narve.ai"
        req = _FakeRequest(host=host)

        fake_user = {"user_id": 1, "email": "u@test.example", "is_admin": True}

        with patch.object(server, "GATEWAY_SSO_SECRET", ""), \
                patch.dict(os.environ, {"GATEWAY_SSO_SECRET": ""}, clear=False), \
                patch.object(server, "IS_PRODUCTION", False), \
                patch.object(server, "current_user", return_value=fake_user), \
                patch.object(server, "get_subdomain", return_value=sub), \
                patch.object(server, "_request_apex", return_value="narve.ai"):
            response = _run(server.proxy_request(req))

        self.assertEqual(response.status_code, 401)


class TestEmptyEmptyCompareIsUnreachable(unittest.TestCase):
    """End-to-end intent of the fix: the unauthenticated empty-empty path
    is no longer reachable. Two complementary assertions:

    1. ``hmac.compare_digest("", "")`` itself is True — this is the cpython
       behaviour we're defending against, not changing.
    2. With our guard in place, the proxy never reaches the point where
       its outbound ``X-Gateway-Secret`` could be empty. Even if the
       downstream service is mis-configured, the gateway will not forward
       a request that downstream's ``compare_digest("", "")`` would accept.
    """

    def test_empty_empty_compare_returns_true(self):
        # Defensive sanity check — if Python ever changes this we want to
        # know, because the entire premise of the fix changes.
        self.assertTrue(hmac.compare_digest("", ""))

    def test_proxy_never_sends_empty_x_gateway_secret(self):
        """Capture the outbound HTTP call. The guard must short-circuit
        before HTTP_CLIENT.request is invoked, so the empty-secret request
        never travels to the downstream side at all."""
        key, sub = _first_dashboard_key_and_subdomain()
        host = f"{sub}.narve.ai"
        req = _FakeRequest(host=host)

        fake_user = {"user_id": 1, "email": "u@test.example", "is_admin": True}
        captured: dict[str, Any] = {}

        async def _record_request(*args, **kwargs):  # pragma: no cover
            captured["called"] = True
            captured["headers"] = kwargs.get("headers", {})
            raise AssertionError(
                "HTTP_CLIENT.request must NOT be called when "
                "GATEWAY_SSO_SECRET is empty — guard short-circuit failed"
            )

        # ``HTTP_CLIENT`` is None until lifespan runs, so swap in a stand-in
        # whose ``request`` method records calls (and would AssertionError if
        # the guard ever lets one through).
        class _StubClient:
            async def request(self, *args, **kwargs):
                return await _record_request(*args, **kwargs)

        with patch.object(server, "GATEWAY_SSO_SECRET", ""), \
                patch.dict(os.environ, {"GATEWAY_SSO_SECRET": ""}, clear=False), \
                patch.object(server, "current_user", return_value=fake_user), \
                patch.object(server, "get_subdomain", return_value=sub), \
                patch.object(server, "_request_apex", return_value="narve.ai"), \
                patch.object(server, "HTTP_CLIENT", _StubClient()):
            response = _run(server.proxy_request(req))

        self.assertEqual(response.status_code, 401)
        self.assertNotIn(
            "called", captured,
            msg="proxy_request reached the upstream HTTP call despite an "
                "empty GATEWAY_SSO_SECRET — fail-closed guard is broken.",
        )


class TestProxyStampsSecretWhenSet(unittest.TestCase):
    """Regression guard: with a real secret, ``X-Gateway-Secret`` is
    stamped on the forwarded headers. Catches a future refactor that
    accidentally drops the assignment along with the if-guard."""

    def test_secret_is_forwarded_when_set(self):
        key, sub = _first_dashboard_key_and_subdomain()
        host = f"{sub}.narve.ai"
        req = _FakeRequest(host=host)

        fake_user = {"user_id": 1, "email": "u@test.example", "is_admin": True}
        captured: dict[str, Any] = {}

        class _FakeResp:
            status_code = 200
            headers = {"content-type": "text/plain"}
            content = b"ok"

        async def _record_request(*args, **kwargs):
            captured["headers"] = kwargs.get("headers", {})
            return _FakeResp()

        class _StubClient:
            async def request(self, *args, **kwargs):
                return await _record_request(*args, **kwargs)

        secret = "x" * 64

        with patch.object(server, "GATEWAY_SSO_SECRET", secret), \
                patch.dict(os.environ, {"GATEWAY_SSO_SECRET": secret}, clear=False), \
                patch.object(server, "current_user", return_value=fake_user), \
                patch.object(server, "get_subdomain", return_value=sub), \
                patch.object(server, "_request_apex", return_value="narve.ai"), \
                patch.object(server, "HTTP_CLIENT", _StubClient()):
            response = _run(server.proxy_request(req))

        self.assertEqual(response.status_code, 200)
        self.assertIn("headers", captured,
                      msg="upstream HTTP call was never made")
        self.assertEqual(captured["headers"].get("X-Gateway-Secret"), secret)


if __name__ == "__main__":
    unittest.main()

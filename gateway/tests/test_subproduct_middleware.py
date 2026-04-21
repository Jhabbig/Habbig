"""Unit tests for middleware.subproduct.

Covers the three things the middleware does in one pass:
  1. Host allowlist (unknown hosts → 400).
  2. Production CF-Connecting-IP enforcement (missing → 403).
  3. Subproduct attachment on request.state.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from middleware.subproduct import (  # noqa: E402
    SubproductMiddleware,
    allowed_hosts,
    subproduct_for_host,
)


class TestAllowlist(unittest.TestCase):
    def test_apex_hosts(self):
        for h in ("narve.ai", "www.narve.ai", "api.narve.ai", "admin.narve.ai"):
            self.assertIn(h, allowed_hosts())

    def test_every_subproduct_included(self):
        hosts = allowed_hosts()
        for slug in ("sports", "weather", "world", "crypto", "midterm", "traders"):
            self.assertIn(f"{slug}.narve.ai", hosts)


class TestSubproductForHost(unittest.TestCase):
    def test_none_for_apex(self):
        self.assertIsNone(subproduct_for_host("narve.ai"))
        self.assertIsNone(subproduct_for_host("www.narve.ai"))
        self.assertIsNone(subproduct_for_host("api.narve.ai"))

    def test_none_for_dev(self):
        self.assertIsNone(subproduct_for_host("localhost"))
        self.assertIsNone(subproduct_for_host("localhost:7000"))

    def test_subproduct_hosts(self):
        self.assertEqual(subproduct_for_host("sports.narve.ai"), "sports")
        self.assertEqual(subproduct_for_host("crypto.narve.ai"), "crypto")

    def test_unknown_host(self):
        self.assertIsNone(subproduct_for_host("randomsubdomain.narve.ai"))
        self.assertIsNone(subproduct_for_host("narve.evil.com"))


class TestMiddlewareHttp(unittest.TestCase):
    """Drive the middleware through a Starlette TestClient so we see
    the same behaviour the gateway does."""

    def setUp(self):
        from fastapi import FastAPI
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        from fastapi.testclient import TestClient

        async def _ping(request: Request):
            sub = getattr(request.state, "subproduct", None)
            return JSONResponse({"sub": sub})

        app = FastAPI(routes=[Route("/ping", _ping, methods=["GET"])])
        app.add_middleware(SubproductMiddleware)
        self._client = TestClient(app)

    def tearDown(self):
        os.environ.pop("PRODUCTION", None)

    def test_unknown_host_rejected(self):
        r = self._client.get(
            "/ping", headers={"Host": "evil.example.com"},
        )
        self.assertEqual(r.status_code, 400)

    def test_apex_host_passes(self):
        r = self._client.get("/ping", headers={"Host": "narve.ai"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["sub"])

    def test_subproduct_attached(self):
        r = self._client.get("/ping", headers={"Host": "sports.narve.ai"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["sub"], "sports")

    def test_production_requires_cf_header(self):
        os.environ["PRODUCTION"] = "1"
        r = self._client.get("/ping", headers={"Host": "narve.ai"})
        # No CF-Connecting-IP → 403.
        self.assertEqual(r.status_code, 403)

    def test_production_passes_with_cf_header(self):
        os.environ["PRODUCTION"] = "1"
        r = self._client.get(
            "/ping", headers={"Host": "narve.ai", "CF-Connecting-IP": "1.2.3.4"},
        )
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for subproduct_access.has_subproduct_access and
require_subproduct_access."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI, Depends, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import subproduct_access as sa  # noqa: E402


def _u(**overrides) -> dict:
    base = {
        "id": 1,
        "is_admin": 0,
        "subscription_tier": "",
        "subproduct_subscriptions": "{}",
    }
    base.update(overrides)
    return base


class TestHasSubproductAccess(unittest.TestCase):
    def test_super_admin(self):
        self.assertTrue(sa.has_subproduct_access(_u(is_admin=2), "sports"))

    def test_admin(self):
        self.assertTrue(sa.has_subproduct_access(_u(is_admin=1), "crypto"))

    def test_pro_tier(self):
        self.assertTrue(sa.has_subproduct_access(
            _u(subscription_tier="pro"), "weather",
        ))

    def test_enterprise_tier(self):
        self.assertTrue(sa.has_subproduct_access(
            _u(subscription_tier="enterprise_team"), "world",
        ))

    def test_active_subproduct_sub(self):
        blob = {"sports": {"status": "active",
                           "period_end": int(time.time()) + 3600,
                           "stripe_sub_id": "sub_1"}}
        self.assertTrue(sa.has_subproduct_access(
            _u(subproduct_subscriptions=json.dumps(blob)), "sports",
        ))

    def test_expired_subproduct_sub(self):
        blob = {"sports": {"status": "active",
                           "period_end": int(time.time()) - 10,
                           "stripe_sub_id": "sub_1"}}
        self.assertFalse(sa.has_subproduct_access(
            _u(subproduct_subscriptions=json.dumps(blob)), "sports",
        ))

    def test_cancelled_subproduct_sub(self):
        blob = {"sports": {"status": "canceled",
                           "period_end": int(time.time()) + 3600,
                           "stripe_sub_id": "sub_1"}}
        self.assertFalse(sa.has_subproduct_access(
            _u(subproduct_subscriptions=json.dumps(blob)), "sports",
        ))

    def test_wrong_slug(self):
        blob = {"sports": {"status": "active",
                           "period_end": int(time.time()) + 3600}}
        self.assertFalse(sa.has_subproduct_access(
            _u(subproduct_subscriptions=json.dumps(blob)), "crypto",
        ))

    def test_none_user(self):
        self.assertFalse(sa.has_subproduct_access(None, "sports"))

    def test_corrupt_json(self):
        self.assertFalse(sa.has_subproduct_access(
            _u(subproduct_subscriptions="not-json"), "sports",
        ))


class TestRequireSubproductAccessHttp(unittest.TestCase):
    """The dependency returns 402 for unauthorised users.

    We build a tiny FastAPI app, attach the dependency to a route, and
    inject the user via a middleware that sets request.state from a
    custom header. This way we don't need the real session layer.
    """

    def _client_for(self, user: dict | None, subproduct: str | None):
        app = FastAPI()

        @app.middleware("http")
        async def _inject(request, call_next):  # type: ignore[override]
            request.state.user = user
            request.state.subproduct = subproduct
            return await call_next(request)

        dep = sa.require_subproduct_access("sports")

        @app.get("/sports", dependencies=[Depends(dep)])
        def _sports(): return {"ok": True}

        return TestClient(app)

    def test_no_user_402(self):
        c = self._client_for(None, None)
        r = c.get("/sports")
        self.assertEqual(r.status_code, 402)

    def test_admin_passes(self):
        c = self._client_for(_u(is_admin=2), None)
        r = c.get("/sports")
        self.assertEqual(r.status_code, 200)

    def test_pro_passes(self):
        c = self._client_for(_u(subscription_tier="pro"), None)
        r = c.get("/sports")
        self.assertEqual(r.status_code, 200)

    def test_wrong_subdomain_402(self):
        """User is Pro but visiting from crypto.narve.ai asking for sports."""
        c = self._client_for(_u(subscription_tier="pro"), "crypto")
        r = c.get("/sports")
        self.assertEqual(r.status_code, 402)

    def test_active_subproduct_sub_passes(self):
        blob = {"sports": {"status": "active",
                           "period_end": int(time.time()) + 3600}}
        c = self._client_for(_u(subproduct_subscriptions=json.dumps(blob)), None)
        r = c.get("/sports")
        self.assertEqual(r.status_code, 200)

    def test_wrong_slug_rejected(self):
        blob = {"crypto": {"status": "active",
                           "period_end": int(time.time()) + 3600}}
        c = self._client_for(_u(subproduct_subscriptions=json.dumps(blob)), None)
        r = c.get("/sports")
        self.assertEqual(r.status_code, 402)


if __name__ == "__main__":
    unittest.main()

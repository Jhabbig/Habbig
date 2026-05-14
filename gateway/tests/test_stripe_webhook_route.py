"""Integration tests for POST /stripe/webhook (live route in
:mod:`stripe_webhook_routes`).

Covers:
  * non-Stripe IP -> 403
  * bad signature -> 400
  * valid signature -> 200
  * duplicate event_id -> 200 already_processed
  * livemode=True event in test mode -> 400
  * customer.subscription.created writes a subscriptions row
  * stripe SDK absent -> 503

A minimal stripe stub is installed in sys.modules['stripe'] that
verifies signatures against the same HMAC scheme
:func:`tests.helpers.signed_stripe_event` uses.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

USES_TESTDB = True
from tests import _testdb  # noqa: F401,E402


_SECRET = "whsec_test_route_secret"


class _SignatureVerificationError(Exception):
    pass


def _verify_and_parse(payload, sig_header, secret):
    if not sig_header or not isinstance(sig_header, str):
        raise _SignatureVerificationError("missing signature header")
    items: dict = {}
    for part in sig_header.split(","):
        if "=" not in part:
            continue
        k, v = part.strip().split("=", 1)
        items.setdefault(k, []).append(v)
    ts_list = items.get("t") or []
    sigs = items.get("v1") or []
    if not ts_list or not sigs:
        raise _SignatureVerificationError("malformed signature header")
    ts = ts_list[0]
    body = payload if isinstance(payload, bytes) else payload.encode()
    expected = hmac.new(
        secret.encode(), f"{ts}.".encode() + body, hashlib.sha256,
    ).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        raise _SignatureVerificationError("signature mismatch")
    return json.loads(body)


def _install_fake_stripe():
    mod = types.ModuleType("stripe")

    class _Webhook:
        @staticmethod
        def construct_event(payload, sig_header, secret):
            return _verify_and_parse(payload, sig_header, secret)

    mod.Webhook = _Webhook
    mod.error = types.SimpleNamespace(
        SignatureVerificationError=_SignatureVerificationError,
    )
    mod.api_key = ""
    sys.modules["stripe"] = mod
    return mod


def _signed_event(event_type, data_object, *, event_id=None, livemode=False,
                  secret=_SECRET, ts=None):
    ts = int(ts or time.time())
    payload = {
        "id": event_id or f"evt_route_{ts}",
        "object": "event",
        "type": event_type,
        "created": ts,
        "livemode": livemode,
        "data": {"object": data_object},
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(
        secret.encode(), f"{ts}.".encode() + body, hashlib.sha256,
    ).hexdigest()
    headers = {
        "Stripe-Signature": f"t={ts},v1={sig}",
        "Content-Type": "application/json",
    }
    return body, headers


def _reorder_stripe_webhook_first(app):
    """Hoist /stripe/webhook to the front of app.router.routes so the
    catch-all wildcard registered earlier in server.py doesn't swallow
    it. See the comment on the @app.post decorator in
    stripe_webhook_routes.py for why this is needed in test mode.
    """
    target = "/stripe/webhook"
    routes = app.router.routes
    for i, r in enumerate(routes):
        if getattr(r, "path", None) == target:
            route = routes.pop(i)
            routes.insert(0, route)
            return


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["STRIPE_WEBHOOK_SECRET"] = _SECRET
        os.environ["STRIPE_IP_ALLOWLIST_ENFORCE"] = "true"
        os.environ["RATE_LIMIT_ENABLED"] = "false"
        os.environ.pop("STRIPE_LIVE_MODE", None)
        os.environ.pop("PRODUCTION", None)

        _install_fake_stripe()
        for name in ("stripe_webhook_hardening", "stripe_webhook_routes"):
            sys.modules.pop(name, None)

        import server  # noqa: F401
        import stripe_webhook_routes  # noqa: F401
        _reorder_stripe_webhook_first(server.app)

        from fastapi.testclient import TestClient
        cls.client = TestClient(server.app)

    @classmethod
    def tearDownClass(cls):
        for k in ("STRIPE_WEBHOOK_SECRET", "STRIPE_IP_ALLOWLIST_ENFORCE",
                  "RATE_LIMIT_ENABLED"):
            os.environ.pop(k, None)

    def _post(self, body, headers, *, cf_ip="3.18.12.63"):
        h = dict(headers)
        if cf_ip is not None:
            h.setdefault("CF-Connecting-IP", cf_ip)
        return self.client.post("/stripe/webhook", content=body, headers=h)


class TestStripeWebhookRoute(_Base):
    def test_non_stripe_ip_returns_403(self):
        body, headers = _signed_event(
            "customer.subscription.created",
            {"id": "sub_x", "metadata": {"user_id": "1", "dashboard_key": "x"}},
        )
        r = self._post(body, headers, cf_ip="8.8.8.8")
        self.assertEqual(r.status_code, 403, r.text[:200])
        self.assertIn("Forbidden", r.text)

    def test_bad_signature_returns_400(self):
        body, headers = _signed_event(
            "customer.subscription.created",
            {"id": "sub_y", "metadata": {"user_id": "1", "dashboard_key": "x"}},
        )
        headers["Stripe-Signature"] = headers["Stripe-Signature"][:-4] + "dead"
        r = self._post(body, headers)
        self.assertEqual(r.status_code, 400, r.text[:200])

    def test_valid_signature_returns_200(self):
        body, headers = _signed_event(
            "customer.subscription.created",
            {"id": "sub_ok_200",
             "metadata": {"user_id": "1", "dashboard_key": "valid"}},
            event_id="evt_route_valid_200",
        )
        r = self._post(body, headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json().get("status"), "ok")

    def test_duplicate_event_id_is_idempotent(self):
        body, headers = _signed_event(
            "customer.subscription.created",
            {"id": "sub_dup",
             "metadata": {"user_id": "1", "dashboard_key": "dup"}},
            event_id="evt_route_dup_1",
        )
        first = self._post(body, headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json().get("status"), "ok")

        second = self._post(body, headers)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json().get("status"), "already_processed")

    def test_livemode_event_in_test_mode_returns_400(self):
        os.environ.pop("STRIPE_LIVE_MODE", None)
        body, headers = _signed_event(
            "customer.subscription.created",
            {"id": "sub_live_in_test",
             "metadata": {"user_id": "1", "dashboard_key": "live_test"}},
            event_id="evt_route_live_in_test",
            livemode=True,
        )
        r = self._post(body, headers)
        self.assertEqual(r.status_code, 400, r.text[:200])
        self.assertIn("Live events", r.text)

    def test_subscription_created_writes_row(self):
        import db
        # Unique-per-run email so the UNIQUE(email) constraint on the
        # shared in-memory DB doesn't clash with rows seeded by other
        # test classes earlier in the suite.
        email = f"route_{os.getpid()}_{int(time.time())}_x@test.example"
        username = f"routeuser_{os.getpid()}_{int(time.time())}"
        with db.conn() as c:
            c.execute(
                "INSERT INTO users (username, email, password_hash, "
                "password_salt, created_at, is_admin) "
                "VALUES (?, ?, '', '', ?, 0)",
                (username, email, int(time.time())),
            )
            uid = c.execute(
                "SELECT id FROM users WHERE email = ?", (email,),
            ).fetchone()["id"]
        body, headers = _signed_event(
            "customer.subscription.created",
            {
                "id": f"sub_route_grant_{uid}",
                "metadata": {"user_id": str(uid),
                             "dashboard_key": "climate",
                             "plan": "pro"},
            },
            event_id=f"evt_route_grant_{uid}",
        )
        r = self._post(body, headers)
        self.assertEqual(r.status_code, 200, r.text)
        with db.conn() as c:
            row = c.execute(
                "SELECT plan, status, source FROM subscriptions "
                "WHERE user_id = ? AND dashboard_key = ?",
                (uid, "climate"),
            ).fetchone()
        self.assertIsNotNone(row, "subscription row was not written")
        self.assertEqual(row["plan"], "pro")
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["source"], "stripe")


class TestStripeWebhookRouteWithoutLibrary(unittest.TestCase):
    """``stripe`` SDK missing -> 503. We can't easily uninstall a real
    install; instead, install a meta-path finder that raises
    ImportError when the handler tries ``import stripe`` mid-request.
    """

    @classmethod
    def setUpClass(cls):
        os.environ["STRIPE_WEBHOOK_SECRET"] = _SECRET
        os.environ["STRIPE_IP_ALLOWLIST_ENFORCE"] = "false"
        os.environ["RATE_LIMIT_ENABLED"] = "false"
        for name in ("stripe_webhook_hardening", "stripe_webhook_routes"):
            sys.modules.pop(name, None)
        import server  # noqa: F401
        import stripe_webhook_routes  # noqa: F401
        _reorder_stripe_webhook_first(server.app)
        from fastapi.testclient import TestClient
        cls.client = TestClient(server.app)

    @classmethod
    def tearDownClass(cls):
        for k in ("STRIPE_WEBHOOK_SECRET", "STRIPE_IP_ALLOWLIST_ENFORCE",
                  "RATE_LIMIT_ENABLED"):
            os.environ.pop(k, None)

    def test_missing_stripe_library_returns_503(self):
        sys.modules.pop("stripe", None)

        class _Blocker:
            def find_spec(self, name, path=None, target=None):
                if name == "stripe":
                    raise ImportError("stripe blocked for test")
                return None

        blocker = _Blocker()
        sys.meta_path.insert(0, blocker)
        try:
            body, headers = _signed_event(
                "customer.subscription.created",
                {"id": "sub_z", "metadata": {}},
            )
            r = self.client.post(
                "/stripe/webhook",
                content=body,
                headers={**headers, "CF-Connecting-IP": "3.18.12.63"},
            )
            self.assertEqual(r.status_code, 503, r.text[:200])
            self.assertIn("not configured", r.text.lower())
        finally:
            sys.meta_path.remove(blocker)


if __name__ == "__main__":
    unittest.main()

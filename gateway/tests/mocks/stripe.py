"""Stripe mock — webhook-event builder + SDK stubs.

The gateway calls Stripe in three places:

    1. /api/stripe/webhook               — verifies + dispatches events
    2. billing routes (create_checkout)  — server-to-Stripe SDK calls
    3. reconcile job                     — polls subscriptions

The real Stripe SDK is never imported in the tests; this module fakes
both the SDK's ``stripe.Webhook.construct_event`` signature verification
AND the ``stripe.checkout.Session.create`` / ``stripe.Subscription.*``
shape.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from typing import Optional

import pytest


DEFAULT_SECRET = "whsec_test_placeholder"


def signed_event(
    event_type: str,
    data: dict,
    *,
    secret: str = DEFAULT_SECRET,
    timestamp: Optional[int] = None,
) -> tuple[bytes, dict]:
    """Build a (body, headers) pair that passes
    ``stripe.Webhook.construct_event`` when the webhook handler uses
    ``secret`` to verify. Mirrors Stripe's timestamp-scheme v1."""
    ts = int(timestamp or time.time())
    payload = {
        "id": f"evt_test_{ts}",
        "object": "event",
        "api_version": "2024-04-10",
        "created": ts,
        "type": event_type,
        "data": {"object": data},
        "livemode": False,
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signed = f"{ts}.".encode() + body
    v1 = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    headers = {
        "stripe-signature": f"t={ts},v1={v1}",
        "content-type": "application/json",
    }
    return body, headers


class MockStripeSDK:
    """Minimal SDK stand-in.

    Usage::

        def test_checkout(mock_stripe):
            mock_stripe.next_session_id = "cs_test_fake"
            ...

    Any attribute access falls through to a SimpleNamespace that can be
    further configured; unknown calls raise ``NotImplementedError`` so
    untested call-sites fail loudly rather than silently returning None.
    """

    def __init__(self):
        self.next_session_id = "cs_test_default"
        self.next_subscription_id = "sub_test_default"
        self.next_customer_id = "cus_test_default"
        self.calls: list[dict] = []

    # ── Checkout sessions ────────────────────────────────────────
    class checkout:  # noqa: N801 — match Stripe SDK exactly
        pass

    def __getattr__(self, name):
        # Lazy-build nested namespaces on first access. Keeps the class
        # body short and makes it obvious tests are hitting a stub.
        def _unsupported(*a, **kw):
            raise NotImplementedError(
                f"MockStripeSDK.{name}: tests must set this explicitly"
            )
        return SimpleNamespace(create=_unsupported)

    def set_checkout_create(self, fn):
        """Call ``fn(**kwargs)`` and return its result for
        ``stripe.checkout.Session.create(...)``."""
        def _wrap(**kwargs):
            self.calls.append({"kind": "checkout", "kwargs": kwargs})
            return fn(**kwargs) if fn else SimpleNamespace(
                id=self.next_session_id, url="https://stripe.test/pay/" + self.next_session_id
            )
        self.checkout.Session = SimpleNamespace(create=_wrap)  # type: ignore[attr-defined]


# ── Pytest fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def stripe_secret(monkeypatch):
    """Pin STRIPE_WEBHOOK_SECRET to a known value and yield it so tests
    can build signatures the handler will accept."""
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", DEFAULT_SECRET)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_placeholder")
    return DEFAULT_SECRET


@pytest.fixture
def mock_stripe(monkeypatch, stripe_secret):
    """Replace any imported ``stripe`` module with the mock SDK."""
    mock = MockStripeSDK()
    mock.set_checkout_create(None)
    import sys
    sys.modules["stripe"] = mock  # type: ignore[assignment]
    return mock

"""Tests for the tier-change cache-busting fix.

AUDIT (CRIT, audit_tier_change.md): every code path that mutates a
user's effective tier MUST invalidate two caches:

  * The sync ``ttl_cache`` (``cache/ttl.py``) — specifically the per-user
    feed namespace (``feed:user_{uid}:*``) and the tier-scoped best-bets
    pages (``best_bets:*``). Wired via
    ``ttl_invalidate.on_subscription_change(user_id)``.
  * The in-process subproduct access verdict cache
    (``subproduct_access._verify_cache``). Wired via
    ``subproduct_access.invalidate_user(user_id)``.

Pre-fix state: the negative-direction writers (cancel/pause/payment_failed)
already busted both caches. Every positive direction (in-app upgrade,
Stripe ``subscription.created`` / ``updated`` / ``invoice.paid``, admin
grant) wrote the row and walked away — leaving the dashboard cache stale
for 60s and the subproduct gate stale for 5min after the user paid.

Coverage in this file:

  * ``db.upsert_subscription`` busts both caches (canonical helper).
  * ``db.cancel_subscription`` busts both caches (canonical helper).
  * ``POST /billing/subscribe`` (free→pro): both caches busted, next
    request observes the new tier.
  * ``POST /billing/subscribe`` (pro→trader downgrade): both caches busted.
  * ``customer.subscription.created`` webhook: both caches busted.
  * Stripe webhook negative branch (``customer.subscription.deleted``)
    continues to bust caches — pin the existing behaviour so this fix
    doesn't accidentally regress the negative-side coverage that already
    existed.

These tests use the shared in-memory DB and stub Stripe so no network is
needed.
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

# Opt into the shared in-memory test DB so the canonical helpers (which
# do real ``db.conn() as c`` writes) see the same connection across
# imports.
USES_TESTDB = True

# Must import _testdb BEFORE server / stripe_webhook_routes.
from tests import _testdb  # noqa: E402,F401


# ── Fake Stripe SDK (minimal, signature-aware) ───────────────────────────────
#
# Mirrors test_stripe_webhook_route.py. We need ``stripe.Webhook.construct_event``
# to verify the HMAC and parse the JSON so the route reaches the dispatch
# branch under test.

_SECRET = "whsec_test_tier_change_cache"


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


def _install_fake_stripe() -> None:
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


_install_fake_stripe()


# ── Env setup before server import ───────────────────────────────────────────
os.environ["STRIPE_WEBHOOK_SECRET"] = _SECRET
os.environ["STRIPE_IP_ALLOWLIST_ENFORCE"] = "false"
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ.pop("STRIPE_LIVE_MODE", None)
os.environ.pop("PRODUCTION", None)

# Reset hardening / route modules so they pick up the env above and the
# fake stripe module.
for _name in ("stripe_webhook_hardening", "stripe_webhook_routes"):
    sys.modules.pop(_name, None)


import db  # noqa: E402
import server  # noqa: E402
import stripe_webhook_routes  # noqa: F401,E402
import subproduct_access  # noqa: E402
from cache.ttl import ttl_cache  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _reorder_stripe_webhook_first(app) -> None:
    """Hoist /stripe/webhook to the front of app.router.routes so the
    catch-all wildcard registered earlier in server.py doesn't swallow
    it. Same trick as test_stripe_webhook_route.py.
    """
    target = "/stripe/webhook"
    routes = app.router.routes
    for i, r in enumerate(routes):
        if getattr(r, "path", None) == target:
            route = routes.pop(i)
            routes.insert(0, route)
            return


_reorder_stripe_webhook_first(server.app)

client = TestClient(server.app)


_SHARED_DB_CONN = _testdb._fake_conn


def _pin_shared_db() -> None:
    db.conn = _SHARED_DB_CONN


def _clear_client_cookies() -> None:
    try:
        client.cookies.clear()
    except Exception:
        pass


# ── Helpers for seeding state ────────────────────────────────────────────────


def _seed_user_caches(user_id: int) -> None:
    """Plant per-user feed + tier-scoped best-bets entries plus a
    positive verdict in subproduct_access._verify_cache, so the next
    invalidation has something concrete to wipe.
    """
    # Sync TTL cache: per-user feed page + a tier-scoped best-bets page.
    # The on_subscription_change helper wipes the entire ``feed:user_{uid}:``
    # prefix plus the entire ``best_bets:`` prefix, so any keys with those
    # prefixes work as proof.
    ttl_cache.set(
        f"feed:user_{user_id}:cat_all:sort_new:page_1", ["stale"], 60,
    )
    ttl_cache.set("best_bets:tier_free:page_1", ["stale"], 120,
                  )
    # Subproduct verdict cache: store a stale "false" so we can prove
    # invalidate_user dropped it.
    subproduct_access._store_verify(user_id, "dash_truth", False)


def _user_cache_state(user_id: int) -> dict:
    return {
        "feed": ttl_cache.get(
            f"feed:user_{user_id}:cat_all:sort_new:page_1",
        ),
        "best_bets": ttl_cache.get("best_bets:tier_free:page_1"),
        "verify": subproduct_access._cached_verify(user_id, "dash_truth"),
    }


def _create_user(email_prefix: str) -> int:
    suffix = f"{os.getpid()}_{int(time.time() * 1000) % 10_000_000}"
    email = f"{email_prefix}_{suffix}@test.example"
    username = f"{email_prefix}_{suffix}"
    return db.create_user(email, "TestPass123!", username=username)


def _prime_csrf(token: str) -> str:
    client.get(
        "/billing",
        cookies={server.COOKIE_NAME: token},
        follow_redirects=False,
    )
    return client.cookies.get("_csrf") or ""


def _post_billing_subscribe(token: str, plan: str, interval: str = "monthly"):
    csrf = _prime_csrf(token)
    payload = {"plan": plan, "interval": interval}
    if csrf:
        payload["_csrf"] = csrf
    return client.post(
        "/billing/subscribe",
        data=payload,
        cookies={server.COOKIE_NAME: token, "_csrf": csrf},
        follow_redirects=False,
    )


# ── Signed Stripe webhook fixtures ───────────────────────────────────────────


def _signed_event(event_type, data_object, *, event_id=None,
                  livemode=False, secret=_SECRET, ts=None):
    ts = int(ts or time.time())
    payload = {
        "id": event_id or f"evt_tier_cache_{ts}",
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


def _post_webhook(body, headers, *, cf_ip="3.18.12.63"):
    h = dict(headers)
    if cf_ip is not None:
        h.setdefault("CF-Connecting-IP", cf_ip)
    return client.post("/stripe/webhook", content=body, headers=h)


# ── Base class ───────────────────────────────────────────────────────────────


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _pin_shared_db()
        super().setUpClass()

    def setUp(self):
        _pin_shared_db()
        _clear_client_cookies()
        # Make sure no leftover stripe.api_key env triggers a real call.
        os.environ.pop("STRIPE_SECRET_KEY", None)
        # Wipe both caches between tests so seeded state never leaks.
        ttl_cache.clear()
        with subproduct_access._verify_lock:
            subproduct_access._verify_cache.clear()
        super().setUp()


# ── Canonical-helper tests ───────────────────────────────────────────────────


class TestCanonicalHelperBustsCaches(_Base):
    """The `db.upsert_subscription` / `db.cancel_subscription` helpers
    must bust both caches every time. Any future caller of these helpers
    inherits the fix for free — that's the whole point of lifting the
    invalidation into the canonical helpers."""

    def test_upsert_subscription_busts_ttl_cache_and_verify_cache(self):
        uid = _create_user("upsert_busts")
        _seed_user_caches(uid)

        # Pre-condition: caches contain stale entries.
        pre = _user_cache_state(uid)
        self.assertEqual(pre["feed"], ["stale"])
        self.assertEqual(pre["best_bets"], ["stale"])
        self.assertEqual(pre["verify"], False)

        db.upsert_subscription(
            user_id=uid,
            dashboard_key="__plan__",
            plan="pro_monthly",
            duration_days=30,
            source="test",
        )

        post = _user_cache_state(uid)
        self.assertIsNone(
            post["feed"], "upsert_subscription must drop per-user feed cache",
        )
        self.assertIsNone(
            post["best_bets"],
            "upsert_subscription must drop tier-scoped best_bets cache",
        )
        self.assertIsNone(
            post["verify"],
            "upsert_subscription must drop subproduct access verdict cache",
        )

    def test_cancel_subscription_busts_ttl_cache_and_verify_cache(self):
        uid = _create_user("cancel_busts")
        # Seed a subscription so cancel has something to flip.
        db.upsert_subscription(
            user_id=uid,
            dashboard_key="__plan__",
            plan="pro_monthly",
            duration_days=30,
            source="test",
        )
        # The upsert above already busted the caches; reseed so we can
        # observe the cancel-driven bust independently.
        _seed_user_caches(uid)
        pre = _user_cache_state(uid)
        self.assertEqual(pre["feed"], ["stale"])
        self.assertEqual(pre["verify"], False)

        db.cancel_subscription(uid, "__plan__")

        post = _user_cache_state(uid)
        self.assertIsNone(post["feed"])
        self.assertIsNone(post["best_bets"])
        self.assertIsNone(post["verify"])

    def test_upsert_only_busts_owning_user(self):
        """A bust for user A must not wipe user B's per-user feed cache.

        Sanity check that we're hitting the scoped helper rather than the
        nuclear ``everything()``."""
        uid_a = _create_user("scope_a")
        uid_b = _create_user("scope_b")
        ttl_cache.set(
            f"feed:user_{uid_a}:cat_all:sort_new:page_1", ["A"], 60,
        )
        ttl_cache.set(
            f"feed:user_{uid_b}:cat_all:sort_new:page_1", ["B"], 60,
        )
        # We also seed both verdict caches; invalidate_user only drops A.
        subproduct_access._store_verify(uid_a, "dash_truth", True)
        subproduct_access._store_verify(uid_b, "dash_truth", True)

        db.upsert_subscription(
            user_id=uid_a,
            dashboard_key="__plan__",
            plan="pro_monthly",
            duration_days=30,
            source="test",
        )

        self.assertIsNone(
            ttl_cache.get(f"feed:user_{uid_a}:cat_all:sort_new:page_1"),
        )
        self.assertEqual(
            ttl_cache.get(f"feed:user_{uid_b}:cat_all:sort_new:page_1"),
            ["B"],
            "user B's feed cache must survive a user A bust",
        )
        self.assertIsNone(
            subproduct_access._cached_verify(uid_a, "dash_truth"),
        )
        self.assertTrue(
            subproduct_access._cached_verify(uid_b, "dash_truth"),
            "user B's verdict cache must survive a user A bust",
        )


# ── /billing/subscribe tests ─────────────────────────────────────────────────


class TestBillingSubscribeBustsCaches(_Base):
    """End-to-end test: a real POST to /billing/subscribe must invalidate
    both caches so the next render of the dashboards / settings / subproduct
    gate observes the new tier."""

    def _login(self, email_prefix: str) -> tuple[int, str]:
        uid = _create_user(email_prefix)
        token = db.create_session(uid)
        return uid, token

    def test_free_to_pro_upgrade_busts_both_caches(self):
        uid, token = self._login("free_to_pro")
        _seed_user_caches(uid)

        resp = _post_billing_subscribe(token, plan="pro", interval="monthly")
        # 302 to /billing on success; 400-range would mean the handler
        # rejected the form. Surface the body so the test fails loudly
        # rather than via the cache-state assertion.
        self.assertEqual(resp.status_code, 302, resp.text[:200])

        post = _user_cache_state(uid)
        self.assertIsNone(
            post["feed"],
            "free→pro must bust per-user feed cache",
        )
        self.assertIsNone(
            post["best_bets"],
            "free→pro must bust tier-scoped best_bets cache",
        )
        self.assertIsNone(
            post["verify"],
            "free→pro must bust subproduct access verdict cache",
        )

    def test_free_to_trader_upgrade_busts_both_caches(self):
        uid, token = self._login("free_to_trader")
        _seed_user_caches(uid)

        resp = _post_billing_subscribe(token, plan="trader", interval="monthly")
        self.assertEqual(resp.status_code, 302, resp.text[:200])

        post = _user_cache_state(uid)
        self.assertIsNone(post["feed"])
        self.assertIsNone(post["best_bets"])
        self.assertIsNone(post["verify"])

    def test_pro_to_trader_downgrade_busts_both_caches(self):
        """The pro→trader downgrade goes through a direct-SQL branch
        (raw UPDATE + INSERT OR REPLACE) that bypasses ``upsert_subscription``.
        The explicit bust at the end of the handler is the only thing
        that keeps the cache invalidation contract intact here."""
        uid, token = self._login("pro_to_trader")

        # Seed a Pro plan with future expires_at, otherwise the handler's
        # "downgrade" branch falls back to "fresh trader" via upsert.
        now = int(time.time())
        with db.conn() as c:
            c.execute(
                "INSERT INTO subscriptions "
                "(user_id, dashboard_key, plan, status, started_at, expires_at, source) "
                "VALUES (?, '__plan__', 'pro_monthly', 'active', ?, ?, 'test')",
                (uid, now, now + 30 * 86400),
            )

        _seed_user_caches(uid)

        resp = _post_billing_subscribe(token, plan="trader", interval="monthly")
        self.assertEqual(resp.status_code, 302, resp.text[:200])

        post = _user_cache_state(uid)
        self.assertIsNone(
            post["feed"],
            "pro→trader downgrade must bust per-user feed cache",
        )
        self.assertIsNone(post["best_bets"])
        self.assertIsNone(
            post["verify"],
            "pro→trader downgrade must bust subproduct verdict cache",
        )


# ── Stripe webhook tests ─────────────────────────────────────────────────────


class TestStripeWebhookBustsCaches(_Base):
    """The Stripe webhook positive branches must invalidate both caches
    so a subscription created/updated/renewed via Stripe Checkout takes
    effect on the very next request — same contract as the in-app path."""

    def test_subscription_created_busts_both_caches(self):
        uid = _create_user("sw_created")
        _seed_user_caches(uid)

        body, headers = _signed_event(
            "customer.subscription.created",
            {
                "id": f"sub_swc_{uid}",
                "metadata": {
                    "user_id": str(uid),
                    "dashboard_key": "climate",
                    "plan": "pro",
                },
                "current_period_end": int(time.time()) + 30 * 86400,
            },
            event_id=f"evt_sw_created_{uid}",
        )
        r = _post_webhook(body, headers)
        self.assertEqual(r.status_code, 200, r.text[:200])

        post = _user_cache_state(uid)
        self.assertIsNone(
            post["feed"],
            "subscription.created must bust per-user feed cache",
        )
        self.assertIsNone(post["best_bets"])
        self.assertIsNone(
            post["verify"],
            "subscription.created must bust subproduct verdict cache",
        )

    def test_subscription_deleted_still_busts_both_caches(self):
        """Negative-direction sanity: the cancellation path already busted
        both caches before this fix landed. Pin the existing behaviour so
        the positive-branch additions don't accidentally regress the
        negative coverage by, e.g., importing in the wrong order."""
        uid = _create_user("sw_deleted")
        # Need a subscription row for the cancellation handler to find.
        # Map customer→user so the trust check passes and the user has a
        # stripe_customer_id linked.
        cust_id = f"cus_swd_{uid}"
        with db.conn() as c:
            c.execute(
                "UPDATE users SET stripe_customer_id = ? WHERE id = ?",
                (cust_id, uid),
            )
        # Seed the subproduct blob so apply_subscription_cancelled has
        # something to flip; otherwise it short-circuits before the bust.
        with db.conn() as c:
            c.execute(
                "UPDATE users SET subproduct_subscriptions = ? WHERE id = ?",
                (json.dumps({"truth": {"status": "active",
                                       "period_end": int(time.time()) + 86400}}),
                 uid),
            )

        # Now seed caches with stale entries.
        _seed_user_caches(uid)

        body, headers = _signed_event(
            "customer.subscription.deleted",
            {
                "id": f"sub_swd_{uid}",
                "customer": cust_id,
                "metadata": {
                    "user_id": str(uid),
                    "subproduct_slug": "truth",
                },
            },
            event_id=f"evt_sw_deleted_{uid}",
        )
        r = _post_webhook(body, headers)
        self.assertEqual(r.status_code, 200, r.text[:200])

        post = _user_cache_state(uid)
        self.assertIsNone(
            post["feed"],
            "subscription.deleted must continue to bust feed cache",
        )
        self.assertIsNone(
            post["verify"],
            "subscription.deleted must continue to bust verdict cache",
        )


if __name__ == "__main__":
    unittest.main()

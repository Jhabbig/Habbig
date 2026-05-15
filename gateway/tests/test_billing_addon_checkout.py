"""Tests for audit-fix H3 + C1 on /settings/billing/cancel and
/settings/billing/addon.

  * **C1**: POST /settings/billing/addon used to call
    ``db.set_trading_addon(uid, True, ...)`` directly, granting any
    logged-in user a free 30-day trading-addon entitlement. The
    handler MUST now create a Stripe Checkout session and 302 to its
    URL; if Stripe is unavailable (SDK absent, key missing, or
    price-id missing) it MUST return 503 with no DB write. The local
    flag is flipped only by the Stripe webhook on
    ``checkout.session.completed``.

  * **H3**: POST /settings/billing/cancel step=3 used to flip every
    active subscription with a bare ``WHERE user_id=? AND
    status='active'`` clause. The handler MUST now require either a
    ``slug`` (per-dashboard cancel, validated against the catalogue
    allowlist) or ``cancel_all=true``; neither -> 400. The trading
    add-on lives on the users table and MUST NOT be touched by either
    branch.

The Stripe SDK is replaced with an in-process stub via
``sys.modules['stripe']`` BEFORE ``server`` is imported (mirrors the
pattern in test_billing_portal.py and test_stripe_webhook_route.py).
"""

from __future__ import annotations

import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

USES_TESTDB = True

# Must import _testdb BEFORE server so the shared in-memory DB is live.
from tests import _testdb  # noqa: E402,F401


# ── Fake Stripe SDK ─────────────────────────────────────────────────────────
#
# The handler does ``import stripe`` lazily inside the request. We need
# that import to find OUR stub. Capture every checkout.Session.create
# call so tests can assert handler-side params.

_LAST_CHECKOUT_CALL: dict = {}
_NEXT_CHECKOUT_URL = "https://checkout.stripe.com/c/pay/test_default"


def _install_fake_stripe():
    mod = types.ModuleType("stripe")
    mod.api_key = ""

    def _checkout_create(*args, **kwargs):
        _LAST_CHECKOUT_CALL.clear()
        _LAST_CHECKOUT_CALL.update(kwargs)
        return types.SimpleNamespace(
            id="cs_test_addon_default",
            url=_NEXT_CHECKOUT_URL,
        )

    mod.checkout = types.SimpleNamespace(
        Session=types.SimpleNamespace(create=_checkout_create),
    )
    sys.modules["stripe"] = mod
    return mod


_install_fake_stripe()


os.environ["STRIPE_IP_ALLOWLIST_ENFORCE"] = "false"
os.environ["RATE_LIMIT_ENABLED"] = "false"


import db  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


_SHARED_DB_CONN = _testdb._fake_conn


def _pin_shared_db() -> None:
    db.conn = _SHARED_DB_CONN


client = TestClient(server.app)


def _clear_client_cookies() -> None:
    try:
        client.cookies.clear()
    except Exception:
        pass


# ── Helpers ─────────────────────────────────────────────────────────────────


def _create_session_compat(user_id):
    """Create a session row compatible with whichever schema the test
    DB happens to carry (legacy ``token`` column or migration-191
    ``token_hash``). The CSRF middleware compares the cookie value
    against the hash in ``sessions`` when ``token_hash`` is present,
    so we store the hash there and return the raw cookie value.
    """
    import hashlib as _h
    import secrets as _s
    import time as _t

    raw = _s.token_urlsafe(48)
    now = int(_t.time())
    token_hash = _h.sha256(raw.encode()).hexdigest()
    with db.conn() as c:
        cols = {
            r[1] for r in c.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "token_hash" in cols:
            c.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, "
                "expires_at) VALUES (?, ?, ?, ?)",
                (token_hash, user_id, now, now + 86400 * 30),
            )
        else:
            c.execute(
                "INSERT INTO sessions (token, user_id, created_at, "
                "expires_at) VALUES (?, ?, ?, ?)",
                (raw, user_id, now, now + 86400 * 30),
            )
    return raw


def _make_user(email, username):
    uid = db.create_user(email, "TestPass123!", username=username)
    token = _create_session_compat(uid)
    return uid, token


def _prime_csrf(token):
    client.get(
        "/settings/billing",
        cookies={server.COOKIE_NAME: token},
        follow_redirects=False,
    )
    return client.cookies.get("_csrf") or ""


def _post_form(path, *, token, data=None):
    csrf = _prime_csrf(token)
    payload = dict(data or {})
    if csrf:
        payload["_csrf"] = csrf
    return client.post(
        path,
        data=payload,
        cookies={server.COOKIE_NAME: token, "_csrf": csrf},
        follow_redirects=False,
    )


# ── Base ────────────────────────────────────────────────────────────────────


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _pin_shared_db()
        super().setUpClass()

    def setUp(self):
        _pin_shared_db()
        _clear_client_cookies()
        _LAST_CHECKOUT_CALL.clear()
        super().setUp()


# ── 1) POST /addon redirects to Stripe checkout URL (mocked) ────────────────


class TestAddonRedirectsToStripeCheckout(_Base):
    """When Stripe is configured the handler MUST redirect to the
    Stripe-hosted checkout URL and MUST NOT flip the local flag.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Configure Stripe so the happy-path is exercised.
        cls._saved_key = os.environ.get("STRIPE_SECRET_KEY")
        cls._saved_price = os.environ.get("STRIPE_PRICE_TRADING_ADDON_MONTHLY")
        os.environ["STRIPE_SECRET_KEY"] = "sk_test_addon_default"
        os.environ["STRIPE_PRICE_TRADING_ADDON_MONTHLY"] = "price_test_trading_addon"

    @classmethod
    def tearDownClass(cls):
        if cls._saved_key is None:
            os.environ.pop("STRIPE_SECRET_KEY", None)
        else:
            os.environ["STRIPE_SECRET_KEY"] = cls._saved_key
        if cls._saved_price is None:
            os.environ.pop("STRIPE_PRICE_TRADING_ADDON_MONTHLY", None)
        else:
            os.environ["STRIPE_PRICE_TRADING_ADDON_MONTHLY"] = cls._saved_price

    def test_redirect_target_is_stripe_url(self):
        uid, token = _make_user(
            f"addon-redir-{int(time.time())}-{os.getpid()}@test.example",
            f"addon_redir_{int(time.time())}_{os.getpid()}",
        )
        # Pre-state: addon OFF.
        db.set_trading_addon(uid, False, None)

        r = _post_form(
            "/settings/billing/addon", token=token,
            data={"addon": "trading"},
        )
        self.assertIn(r.status_code, (302, 303), r.text[:200])
        loc = r.headers.get("location", "")
        self.assertTrue(
            loc.startswith("https://checkout.stripe.com/"),
            f"expected stripe checkout URL, got {loc!r}",
        )
        # The addon flag must NOT have flipped — only the webhook does that.
        self.assertFalse(
            db.get_trading_addon_status(uid)["active"],
            "addon flipped inline in handler — audit regression!",
        )

    def test_stripe_called_with_correct_metadata(self):
        uid, token = _make_user(
            f"addon-meta-{int(time.time())}-{os.getpid()}@test.example",
            f"addon_meta_{int(time.time())}_{os.getpid()}",
        )
        r = _post_form(
            "/settings/billing/addon", token=token,
            data={"addon": "trading"},
        )
        self.assertIn(r.status_code, (302, 303), r.text[:200])
        # The webhook needs user_id + addon + flow to know what to flip.
        meta = _LAST_CHECKOUT_CALL.get("metadata", {})
        self.assertEqual(meta.get("user_id"), str(uid))
        self.assertEqual(meta.get("addon"), "trading")
        self.assertEqual(meta.get("flow"), "addon")
        # Subscription metadata too so renewal events can be traced.
        sub_meta = (_LAST_CHECKOUT_CALL.get("subscription_data") or {}).get("metadata") or {}
        self.assertEqual(sub_meta.get("user_id"), str(uid))
        self.assertEqual(sub_meta.get("addon"), "trading")
        # Line item must be the trading-addon price id.
        line_items = _LAST_CHECKOUT_CALL.get("line_items") or []
        self.assertEqual(line_items[0]["price"], "price_test_trading_addon")
        self.assertEqual(_LAST_CHECKOUT_CALL.get("mode"), "subscription")


# ── 2) POST /addon when Stripe unavailable returns 503, no DB write ─────────


class TestAddonFailsClosedWhenStripeUnavailable(_Base):
    """When Stripe is NOT configured the handler MUST return 503 and
    MUST NOT flip the local flag. This is the audit-fix invariant."""

    def setUp(self):
        super().setUp()
        # Tear down every Stripe-configuration env var so each branch
        # of the "fail closed" path is exercised in turn.
        self._saved = {}
        for k in ("STRIPE_SECRET_KEY", "STRIPE_PRICE_TRADING_ADDON_MONTHLY"):
            if k in os.environ:
                self._saved[k] = os.environ.pop(k)

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ[k] = v
        super().tearDown()

    def test_missing_secret_key_returns_503(self):
        uid, token = _make_user(
            f"addon-nokey-{int(time.time())}-{os.getpid()}@test.example",
            f"addon_nokey_{int(time.time())}_{os.getpid()}",
        )
        db.set_trading_addon(uid, False, None)
        # Set the price id but NOT the secret key.
        os.environ["STRIPE_PRICE_TRADING_ADDON_MONTHLY"] = "price_test_x"
        try:
            r = _post_form(
                "/settings/billing/addon", token=token,
                data={"addon": "trading"},
            )
            self.assertEqual(r.status_code, 503, r.text[:200])
            self.assertFalse(
                db.get_trading_addon_status(uid)["active"],
                "addon flipped despite missing Stripe key — self-grant regression!",
            )
            # Stripe must NOT have been called.
            self.assertEqual(_LAST_CHECKOUT_CALL, {})
        finally:
            os.environ.pop("STRIPE_PRICE_TRADING_ADDON_MONTHLY", None)

    def test_missing_price_id_returns_503(self):
        uid, token = _make_user(
            f"addon-noprice-{int(time.time())}-{os.getpid()}@test.example",
            f"addon_noprice_{int(time.time())}_{os.getpid()}",
        )
        db.set_trading_addon(uid, False, None)
        os.environ["STRIPE_SECRET_KEY"] = "sk_test_x"
        try:
            r = _post_form(
                "/settings/billing/addon", token=token,
                data={"addon": "trading"},
            )
            self.assertEqual(r.status_code, 503, r.text[:200])
            self.assertFalse(
                db.get_trading_addon_status(uid)["active"],
                "addon flipped despite missing price id — self-grant regression!",
            )
            self.assertEqual(_LAST_CHECKOUT_CALL, {})
        finally:
            os.environ.pop("STRIPE_SECRET_KEY", None)

    def test_stripe_sdk_absent_returns_503(self):
        """When the ``stripe`` module is unavailable the handler MUST
        return 503 with no DB write. We swap our stub out for a None
        sentinel that triggers ImportError on attribute access and
        confirm the handler bails before touching db.set_trading_addon."""
        uid, token = _make_user(
            f"addon-nosdk-{int(time.time())}-{os.getpid()}@test.example",
            f"addon_nosdk_{int(time.time())}_{os.getpid()}",
        )
        db.set_trading_addon(uid, False, None)
        os.environ["STRIPE_SECRET_KEY"] = "sk_test_x"
        os.environ["STRIPE_PRICE_TRADING_ADDON_MONTHLY"] = "price_test_x"

        saved = sys.modules.pop("stripe", None)
        # Sentinel that triggers ImportError on any attempt to import
        # ``stripe`` from the handler.
        sys.modules["stripe"] = None  # type: ignore[assignment]
        try:
            r = _post_form(
                "/settings/billing/addon", token=token,
                data={"addon": "trading"},
            )
            self.assertEqual(r.status_code, 503, r.text[:200])
            self.assertFalse(
                db.get_trading_addon_status(uid)["active"],
                "addon flipped despite missing Stripe SDK — self-grant regression!",
            )
        finally:
            if saved is not None:
                sys.modules["stripe"] = saved
            else:
                sys.modules.pop("stripe", None)
            os.environ.pop("STRIPE_SECRET_KEY", None)
            os.environ.pop("STRIPE_PRICE_TRADING_ADDON_MONTHLY", None)


# ── 3) Cancel handler — slug-scoped + cancel_all opt-in ─────────────────────


class TestCancelScopedToDashboardKey(_Base):
    """AUDIT (H3): the cancel-finalize UPDATE used to flip every active
    subscription row for the user with a bare WHERE user_id=?
    AND status='active'. The handler now requires an explicit scope:
    either ``slug=<dashboard_key>`` (per-dashboard cancel) or
    ``cancel_all=true`` (legacy nuke-all retention path).
    """

    def _seed_user_with_subs(self, *, slugs):
        """Create a user with N active subscriptions, one per slug.
        Returns (uid, token).
        """
        now = int(time.time())
        email = f"cancel-scope-{now}-{os.getpid()}-{id(self)}@test.example"
        uname = f"cancel_scope_{now}_{os.getpid()}_{id(self)}"
        uid = db.create_user(email, "TestPass123!", username=uname)
        token = _create_session_compat(uid)
        expires = now + 30 * 86400
        with db.conn() as c:
            for slug in slugs:
                c.execute(
                    "INSERT INTO subscriptions "
                    "(user_id, dashboard_key, plan, status, started_at, expires_at) "
                    "VALUES (?, ?, 'pro_monthly', 'active', ?, ?)",
                    (uid, slug, now, expires),
                )
        return uid, token

    def _advance_to_step3(self, token):
        """Open a cancellation_attempt via step=1 and return the id."""
        r1 = _post_form(
            "/settings/billing/cancel", token=token,
            data={"reason": "too_expensive", "step": "1"},
        )
        self.assertEqual(r1.status_code, 302, r1.text[:200])
        return int(r1.headers["location"].split("attempt_id=")[-1])

    def _statuses(self, uid):
        with db.conn() as c:
            rows = c.execute(
                "SELECT dashboard_key, status FROM subscriptions "
                "WHERE user_id = ?",
                (uid,),
            ).fetchall()
        return {r["dashboard_key"]: r["status"] for r in rows}

    def test_cancel_with_slug_only_flips_that_row(self):
        uid, token = self._seed_user_with_subs(slugs=["sports", "climate", "crypto"])
        attempt = self._advance_to_step3(token)
        r = _post_form(
            "/settings/billing/cancel", token=token,
            data={"step": "3", "attempt_id": str(attempt), "slug": "sports"},
        )
        self.assertEqual(r.status_code, 302, r.text[:200])
        self.assertIn("saved=cancelled", r.headers["location"])
        statuses = self._statuses(uid)
        self.assertEqual(
            statuses.get("sports"), "cancelled",
            f"sports row not flipped (got {statuses!r})",
        )
        # The other rows must remain active.
        self.assertEqual(
            statuses.get("climate"), "active",
            f"climate row was flipped — slug scope leaked! (got {statuses!r})",
        )
        self.assertEqual(
            statuses.get("crypto"), "active",
            f"crypto row was flipped — slug scope leaked! (got {statuses!r})",
        )

    def test_cancel_without_slug_or_cancel_all_returns_400(self):
        uid, token = self._seed_user_with_subs(slugs=["sports"])
        attempt = self._advance_to_step3(token)
        r = _post_form(
            "/settings/billing/cancel", token=token,
            data={"step": "3", "attempt_id": str(attempt)},
        )
        self.assertEqual(r.status_code, 400, r.text[:200])
        # The row must remain active — no implicit cancel-all.
        self.assertEqual(
            self._statuses(uid).get("sports"), "active",
            "row was flipped on a 400 — handler should fail before any UPDATE",
        )

    def test_cancel_with_unknown_slug_returns_400(self):
        uid, token = self._seed_user_with_subs(slugs=["sports"])
        attempt = self._advance_to_step3(token)
        r = _post_form(
            "/settings/billing/cancel", token=token,
            data={"step": "3", "attempt_id": str(attempt), "slug": "not_a_real_slug"},
        )
        self.assertEqual(r.status_code, 400, r.text[:200])
        self.assertEqual(self._statuses(uid).get("sports"), "active")

    def test_cancel_all_flips_every_sub_but_not_trading_addon(self):
        uid, token = self._seed_user_with_subs(
            slugs=["sports", "climate", "__plan__"],
        )
        # Pre-stamp the trading addon ON.
        db.set_trading_addon(uid, True, period_end=int(time.time()) + 30 * 86400)
        self.assertTrue(db.get_trading_addon_status(uid)["active"])

        attempt = self._advance_to_step3(token)
        r = _post_form(
            "/settings/billing/cancel", token=token,
            data={"step": "3", "attempt_id": str(attempt), "cancel_all": "true"},
        )
        self.assertEqual(r.status_code, 302, r.text[:200])
        statuses = self._statuses(uid)
        # All subscriptions flipped to cancelled.
        for slug in ("sports", "climate", "__plan__"):
            self.assertEqual(
                statuses.get(slug), "cancelled",
                f"{slug} row not flipped on cancel_all (got {statuses!r})",
            )
        # The trading add-on (lives on users table, not subscriptions)
        # MUST NOT be flipped by cancel_all — it has its own route.
        self.assertTrue(
            db.get_trading_addon_status(uid)["active"],
            "cancel_all=true flipped the trading add-on — scope leak!",
        )


if __name__ == "__main__":
    unittest.main()

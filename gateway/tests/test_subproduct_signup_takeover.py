"""AUDIT #15 CRIT #1 — subproduct-signup account-takeover regression.

The attack the fix closes:

  1. Attacker POSTs ``/subproduct-signup`` (or
     ``/api/billing/subproduct-checkout``) with the victim's email and
     a real ``subproduct`` slug.
  2. Pre-fix, ``_create_or_get_shell_user`` cheerfully returned the
     victim's existing ``users.id`` because the email was already in
     the table — regardless of whether that row was a shell user
     (zero-password pre-registration) or a fully registered account.
  3. ``_build_checkout_session`` then minted a signed magic-link
     auth token for the victim's user_id and embedded it in Stripe's
     ``success_url``.
  4. After the attacker paid, Stripe redirected the browser to
     ``/onboarding?auth=<victim-token>``. ``_consume_magic_link``
     verified the signature, burnt the jti, and minted a session
     cookie — the attacker walked away with the victim's account
     (admin, API keys, payment methods, trading history).

The fix lives in three places, all of which this file exercises:

  * ``_create_or_get_shell_user`` now raises ``RegisteredUserConflict``
    when the email already belongs to a registered user (non-empty
    ``password_hash`` AND non-empty ``password_salt``). Both routes
    translate that into a user-visible refusal BEFORE
    ``_build_checkout_session`` is ever reached.
  * ``_magic_link_secret`` no longer falls back to
    ``SITE_ACCESS_TOKEN``. Production refuses to start without
    ``SUBPRODUCT_MAGIC_LINK_SECRET`` (audit MED #2).
  * Every magic-link mint + redeem writes to ``audit_log`` (audit
    MED #1) so an admin can reconstruct the trail post-incident.

Coverage targets:
    * Registered user's email → 409 / ``error=already_registered``
      with NO row added to ``users`` and NO row added to ``audit_log``
      for the magic-link MINT action.
    * Shell user email → 200 (JSON route) or 302 to Stripe (form route)
      — back-compat for the legitimate "pay then sign in" flow.
    * Unknown email → 200/302 + a new shell user row in ``users``.
    * ``register(app)`` in production with no
      ``SUBPRODUCT_MAGIC_LINK_SECRET`` → RuntimeError at startup.
    * ``burn_magic_link_jti`` writes a ``magic_link.redeem`` row.
"""

from __future__ import annotations

import os
import sys
import time
import types
import unittest
import uuid

# Shared in-memory DB + migrations (must precede ``db`` / ``server`` imports).
from tests import _testdb  # noqa: F401

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Stripe SDK stub ─────────────────────────────────────────────────────────
#
# ``_build_checkout_session`` does ``import stripe`` lazily. We replace
# the real SDK with a tiny stub that records the kwargs it was called
# with — that's enough surface to exercise the checkout-URL branch and
# to assert the magic-link token gets embedded in success_url.

_STRIPE_CALLS: list[dict] = []
_STRIPE_NEXT_URL = "https://checkout.stripe.test/pay/cs_test_takeover"


def _install_stripe_stub() -> None:
    mod = types.ModuleType("stripe")
    mod.api_key = ""

    def _session_create(**kwargs):
        _STRIPE_CALLS.append(dict(kwargs))
        return types.SimpleNamespace(
            id="cs_test_takeover", url=_STRIPE_NEXT_URL,
        )

    mod.checkout = types.SimpleNamespace(
        Session=types.SimpleNamespace(create=_session_create),
    )
    sys.modules["stripe"] = mod


_install_stripe_stub()


# ── Stripe price env vars ───────────────────────────────────────────────────
#
# ``_stripe_price_id("sports")`` reads ``STRIPE_PRICE_ID_SPORTS_MONTHLY``
# from the env. We pin a known fake so the "shell user → 200/302" tests
# reach the checkout branch.

os.environ.setdefault("STRIPE_PRICE_ID_SPORTS_MONTHLY", "price_test_sports")


import db  # noqa: E402
import subproduct_signup_routes as ssr  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _build_app() -> tuple[FastAPI, TestClient]:
    """Minimal FastAPI app with only the signup routes attached.

    Mirrors the pattern in ``test_subproduct_signup_redirect.py`` — we
    don't pay the cost of importing ``server`` for what amounts to a
    handful of route-level assertions.
    """
    app = FastAPI()
    ssr.register(app)
    return app, TestClient(app, follow_redirects=False)


def _fresh_ip_header() -> dict:
    """Unique X-Forwarded-For per request so the per-IP rate limiter
    never trips across tests in the same run. Same trick as the
    redirect-regression suite."""
    return {"X-Forwarded-For": f"203.0.113.{uuid.uuid4().int % 254 + 1}"}


# ── DB helpers ──────────────────────────────────────────────────────────────


def _make_registered_user(email: str) -> int:
    """Create a fully registered user (real password_hash + salt) via
    the canonical ``db.create_user`` path."""
    return db.create_user(email, "TestPass123!", username=f"u_{int(time.time()*1000)%100000}_{uuid.uuid4().hex[:6]}")


def _make_shell_user(email: str) -> int:
    """Create a shell user the same way the signup route does. Bypasses
    ``_create_or_get_shell_user`` so we can isolate the "existing shell
    row is reused" branch from the "new shell row is created" branch."""
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO users (username, email, password_hash, "
            "password_salt, created_at, is_admin, subproduct_subscriptions) "
            "VALUES (?, ?, '', '', ?, 0, '{}')",
            (f"shell_{uuid.uuid4().hex[:8]}", email.lower().strip(),
             int(time.time())),
        )
        return int(cur.lastrowid)


def _user_count(email: str) -> int:
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM users WHERE email = ?",
            (email.lower().strip(),),
        ).fetchone()
        return int(row["n"]) if row else 0


def _mint_audit_count(user_id: int) -> int:
    """Count ``magic_link.mint`` rows that reference ``user_id``."""
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM audit_log "
            "WHERE action = ? AND (admin_user_id = ? OR target_id = ?)",
            ("magic_link.mint", user_id, str(user_id)),
        ).fetchone()
        return int(row["n"]) if row else 0


def _redeem_audit_count(user_id: int) -> int:
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM audit_log "
            "WHERE action = ? AND (admin_user_id = ? OR target_id = ?)",
            ("magic_link.redeem", user_id, str(user_id)),
        ).fetchone()
        return int(row["n"]) if row else 0


def _clear_rate_store() -> None:
    """Reset the gateway-wide rate-limit buckets so the per-IP and
    per-email caps don't bleed across tests in the same run."""
    try:
        import server as _srv
        if hasattr(_srv, "_rate_store"):
            _srv._rate_store.clear()
    except Exception:
        pass


def _wipe_audit_log() -> None:
    with db.conn() as c:
        c.execute("DELETE FROM audit_log")


def _wipe_users() -> None:
    """Best-effort wipe — child tables first to keep FKs happy."""
    with db.conn() as c:
        for table in (
            "sessions", "user_sessions", "csrf_tokens",
            "subscriptions", "users",
        ):
            try:
                c.execute(f"DELETE FROM {table}")
            except Exception:
                pass


# ── Direct unit tests on the takeover guard ─────────────────────────────────


class TestCreateOrGetShellUserRefusesRegistered(unittest.TestCase):
    """``_create_or_get_shell_user`` MUST raise on registered emails."""

    def setUp(self):
        _wipe_users()
        _wipe_audit_log()
        _clear_rate_store()
        _STRIPE_CALLS.clear()

    def test_registered_email_raises_conflict(self):
        email = f"victim_{uuid.uuid4().hex[:6]}@example.com"
        uid = _make_registered_user(email)
        # The whole point of the fix — re-calling on a registered email
        # must NOT return the user_id.
        with self.assertRaises(ssr.RegisteredUserConflict) as ctx:
            ssr._create_or_get_shell_user(email)
        # The exception carries the user_id so audit logs can reference
        # it, but routes MUST NOT leak it to the caller.
        self.assertEqual(ctx.exception.user_id, uid)
        # No phantom row was inserted.
        self.assertEqual(_user_count(email), 1)

    def test_shell_email_returns_existing_id(self):
        """Shell user reuse — explicit pre-fix behaviour preserved."""
        email = f"shell_{uuid.uuid4().hex[:6]}@example.com"
        uid = _make_shell_user(email)
        # Same email, second call → same id, no duplicate row.
        out = ssr._create_or_get_shell_user(email)
        self.assertEqual(out, uid)
        self.assertEqual(_user_count(email), 1)

    def test_unknown_email_creates_shell_row(self):
        email = f"new_{uuid.uuid4().hex[:6]}@example.com"
        self.assertEqual(_user_count(email), 0)
        uid = ssr._create_or_get_shell_user(email)
        self.assertGreater(uid, 0)
        self.assertEqual(_user_count(email), 1)
        # The freshly-inserted row should be a shell — no password.
        with db.conn() as c:
            row = c.execute(
                "SELECT password_hash, password_salt FROM users WHERE id = ?",
                (uid,),
            ).fetchone()
        self.assertEqual(row["password_hash"], "")
        self.assertEqual(row["password_salt"], "")

    def test_half_initialised_row_is_treated_as_shell(self):
        """A row with hash set but salt blank (or vice versa) — caused
        by a crashed signup — must NOT be classified as registered,
        otherwise a single bad write locks an email out of the flow."""
        email = f"half_{uuid.uuid4().hex[:6]}@example.com"
        with db.conn() as c:
            cur = c.execute(
                "INSERT INTO users (username, email, password_hash, "
                "password_salt, created_at, is_admin, "
                "subproduct_subscriptions) "
                "VALUES (?, ?, 'partial-hash', '', ?, 0, '{}')",
                (f"half_{uuid.uuid4().hex[:8]}", email, int(time.time())),
            )
            uid = cur.lastrowid
        # Should NOT raise — partial row counts as shell.
        out = ssr._create_or_get_shell_user(email)
        self.assertEqual(out, uid)


# ── HTTP-level: /api/billing/subproduct-checkout (JSON sibling) ─────────────


class TestJSONCheckoutRefusesRegistered(unittest.TestCase):
    """The JSON sibling MUST close the same path the form route closes."""

    def setUp(self):
        _wipe_users()
        _wipe_audit_log()
        _clear_rate_store()
        _STRIPE_CALLS.clear()
        self.app, self.client = _build_app()

    def tearDown(self):
        self.client.close()

    def test_registered_email_returns_409_and_no_mint(self):
        email = f"victim_{uuid.uuid4().hex[:6]}@example.com"
        uid = _make_registered_user(email)
        r = self.client.post(
            "/api/billing/subproduct-checkout",
            json={"email": email, "subproduct": "sports"},
            headers=_fresh_ip_header(),
        )
        self.assertEqual(r.status_code, 409)
        body = r.json()
        # The error message tells the user what to do; the code is
        # machine-readable for the SPA.
        self.assertEqual(body.get("code"), "email_already_registered")
        self.assertIn("Sign in", body.get("error", ""))
        # Stripe SDK must NOT have been called.
        self.assertEqual(len(_STRIPE_CALLS), 0,
                         f"Stripe was called: {_STRIPE_CALLS!r}")
        # No magic-link mint audit row referencing the victim.
        self.assertEqual(_mint_audit_count(uid), 0)
        # The victim's row is untouched (still registered).
        self.assertEqual(_user_count(email), 1)

    def test_shell_email_returns_200_with_checkout_url(self):
        email = f"shell_{uuid.uuid4().hex[:6]}@example.com"
        uid = _make_shell_user(email)
        r = self.client.post(
            "/api/billing/subproduct-checkout",
            json={"email": email, "subproduct": "sports"},
            headers=_fresh_ip_header(),
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body.get("checkout_url"), _STRIPE_NEXT_URL)
        # Stripe WAS called once for the shell user.
        self.assertEqual(len(_STRIPE_CALLS), 1)
        # And the magic-link mint was audited for the shell user_id.
        self.assertGreaterEqual(_mint_audit_count(uid), 1)
        # The success_url must carry the auth token bound to the
        # shell user_id, NOT to any other identity.
        success_url = _STRIPE_CALLS[0].get("success_url", "")
        self.assertIn("auth=", success_url)

    def test_unknown_email_creates_shell_and_returns_200(self):
        email = f"new_{uuid.uuid4().hex[:6]}@example.com"
        self.assertEqual(_user_count(email), 0)
        r = self.client.post(
            "/api/billing/subproduct-checkout",
            json={"email": email, "subproduct": "sports"},
            headers=_fresh_ip_header(),
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json().get("checkout_url"), _STRIPE_NEXT_URL)
        # A new shell row exists post-call.
        self.assertEqual(_user_count(email), 1)
        # Stripe was called exactly once.
        self.assertEqual(len(_STRIPE_CALLS), 1)


# ── HTTP-level: /subproduct-signup (form route) ─────────────────────────────


class TestFormSignupRefusesRegistered(unittest.TestCase):
    """The form route MUST close the takeover path too — same fix,
    different response shape (302 to ``?error=already_registered``).
    """

    def setUp(self):
        _wipe_users()
        _wipe_audit_log()
        _clear_rate_store()
        _STRIPE_CALLS.clear()
        self.app, self.client = _build_app()

    def tearDown(self):
        self.client.close()

    def test_registered_email_bounces_with_already_registered(self):
        email = f"victim_{uuid.uuid4().hex[:6]}@example.com"
        uid = _make_registered_user(email)
        r = self.client.post(
            "/subproduct-signup",
            data={"email": email, "subproduct": "sports"},
            headers=_fresh_ip_header(),
        )
        self.assertEqual(r.status_code, 302)
        loc = r.headers.get("location", "")
        # Stays on-platform, bounces to the per-product landing with
        # an unambiguous error code the landing can render as
        # "you're already a member — sign in first".
        self.assertEqual(
            loc, "https://sports.narve.ai/?error=already_registered",
        )
        # Stripe must not have been called.
        self.assertEqual(len(_STRIPE_CALLS), 0)
        # No MINT audit row referencing the victim.
        self.assertEqual(_mint_audit_count(uid), 0)

    def test_shell_email_routes_to_stripe(self):
        email = f"shell_{uuid.uuid4().hex[:6]}@example.com"
        _make_shell_user(email)
        r = self.client.post(
            "/subproduct-signup",
            data={"email": email, "subproduct": "sports"},
            headers=_fresh_ip_header(),
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers.get("location"), _STRIPE_NEXT_URL)
        self.assertEqual(len(_STRIPE_CALLS), 1)

    def test_unknown_email_routes_to_stripe_and_creates_row(self):
        email = f"new_{uuid.uuid4().hex[:6]}@example.com"
        self.assertEqual(_user_count(email), 0)
        r = self.client.post(
            "/subproduct-signup",
            data={"email": email, "subproduct": "sports"},
            headers=_fresh_ip_header(),
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers.get("location"), _STRIPE_NEXT_URL)
        self.assertEqual(_user_count(email), 1)


# ── Magic-link secret startup guard (audit MED #2) ──────────────────────────


class TestMagicLinkSecretProductionGuard(unittest.TestCase):
    """``register(app)`` must refuse to start in production when
    ``SUBPRODUCT_MAGIC_LINK_SECRET`` is unset or too short.

    We probe ``_ensure_magic_link_secret_configured`` directly rather
    than spinning up the full app — same code path, no FastAPI noise.
    """

    def setUp(self):
        self._saved_env = {
            k: os.environ.get(k)
            for k in (
                "PRODUCTION", "IS_PRODUCTION",
                "SUBPRODUCT_MAGIC_LINK_SECRET",
            )
        }

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _set_prod(self, on: bool) -> None:
        if on:
            os.environ["PRODUCTION"] = "1"
        else:
            os.environ.pop("PRODUCTION", None)
            os.environ.pop("IS_PRODUCTION", None)

    def test_prod_unset_secret_refuses_to_start(self):
        self._set_prod(True)
        os.environ.pop("SUBPRODUCT_MAGIC_LINK_SECRET", None)
        with self.assertRaises(RuntimeError) as ctx:
            ssr._ensure_magic_link_secret_configured()
        msg = str(ctx.exception)
        self.assertIn("SUBPRODUCT_MAGIC_LINK_SECRET", msg)

    def test_prod_short_secret_refuses_to_start(self):
        self._set_prod(True)
        os.environ["SUBPRODUCT_MAGIC_LINK_SECRET"] = "tooshort"
        with self.assertRaises(RuntimeError) as ctx:
            ssr._ensure_magic_link_secret_configured()
        self.assertIn("at least 32", str(ctx.exception))

    def test_prod_long_secret_starts_cleanly(self):
        self._set_prod(True)
        os.environ["SUBPRODUCT_MAGIC_LINK_SECRET"] = "a" * 32
        # Should NOT raise.
        ssr._ensure_magic_link_secret_configured()

    def test_dev_unset_secret_starts_cleanly(self):
        """Dev / tests must NOT require the env var — that's the whole
        point of guarding the check behind ``_is_production()``."""
        self._set_prod(False)
        os.environ.pop("SUBPRODUCT_MAGIC_LINK_SECRET", None)
        ssr._ensure_magic_link_secret_configured()

    def test_register_app_propagates_prod_guard(self):
        """``register(app)`` is the entry point server.py imports; the
        guard MUST fire there so a misconfigured deploy never reaches
        the request-accept phase."""
        self._set_prod(True)
        os.environ.pop("SUBPRODUCT_MAGIC_LINK_SECRET", None)
        app = FastAPI()
        with self.assertRaises(RuntimeError):
            ssr.register(app)


# ── Magic-link secret no longer falls back to SITE_ACCESS_TOKEN ─────────────


class TestMagicLinkSecretDoesNotFallbackToSITE_ACCESS_TOKEN(unittest.TestCase):
    """Audit MED #2 — a token signed with SITE_ACCESS_TOKEN MUST NOT
    verify under the new signing key. The prior implementation fell
    back to SITE_ACCESS_TOKEN, letting anyone who knew the gate
    password forge magic links.
    """

    def setUp(self):
        self._saved = {
            k: os.environ.get(k)
            for k in (
                "PRODUCTION", "IS_PRODUCTION",
                "SUBPRODUCT_MAGIC_LINK_SECRET", "SITE_ACCESS_TOKEN",
                "GATEWAY_COOKIE_SECRET",
            )
        }

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_token_signed_with_site_access_token_does_not_verify(self):
        """Forge a token with the OLD fallback key (SITE_ACCESS_TOKEN)
        and confirm the new ``verify_magic_link_token`` rejects it."""
        import base64
        import hashlib
        import hmac
        # Pin the dedicated secret AND a known SITE_ACCESS_TOKEN.
        os.environ["SUBPRODUCT_MAGIC_LINK_SECRET"] = "a" * 40
        os.environ["SITE_ACCESS_TOKEN"] = "the-old-public-gate-password-12345"
        # Make sure dev fallback can't muddy the result.
        os.environ.pop("PRODUCTION", None)
        os.environ.pop("IS_PRODUCTION", None)

        # Forge a token using SITE_ACCESS_TOKEN as the key.
        user_id = 4242
        jti = "forged-jti"
        expires_at = int(time.time()) + 600
        payload = f"{user_id}.{jti}.{expires_at}"
        forged_mac = hmac.new(
            os.environ["SITE_ACCESS_TOKEN"].encode(),
            payload.encode(),
            hashlib.sha256,
        ).digest()
        forged_mac_b64 = base64.urlsafe_b64encode(forged_mac).rstrip(b"=").decode()
        forged_token = f"{payload}.{forged_mac_b64}"

        # Pre-fix this would have validated. Post-fix it must reject.
        self.assertIsNone(ssr.verify_magic_link_token(forged_token))

        # And a token minted under the new secret must still
        # round-trip — back-compat.
        ok_token = ssr.mint_magic_link_token(user_id)
        self.assertIsNotNone(ssr.verify_magic_link_token(ok_token))


# ── Audit-log MINT + REDEEM rows (audit MED #1) ─────────────────────────────


class TestAuditLogMintAndRedeem(unittest.TestCase):
    """Every magic-link mint MUST land a row with action=magic_link.mint.
    Every magic-link redeem MUST land a row with action=magic_link.redeem.
    """

    def setUp(self):
        _wipe_users()
        _wipe_audit_log()
        _clear_rate_store()
        _STRIPE_CALLS.clear()
        self.app, self.client = _build_app()

    def tearDown(self):
        self.client.close()

    def test_mint_writes_audit_row_with_user_id_and_slug(self):
        email = f"mint_{uuid.uuid4().hex[:6]}@example.com"
        r = self.client.post(
            "/api/billing/subproduct-checkout",
            json={"email": email, "subproduct": "sports"},
            headers=_fresh_ip_header(),
        )
        self.assertEqual(r.status_code, 200, r.text)
        with db.conn() as c:
            row = c.execute(
                "SELECT admin_user_id, target_type, target_id, notes "
                "FROM audit_log WHERE action = ? ORDER BY id DESC LIMIT 1",
                ("magic_link.mint",),
            ).fetchone()
        self.assertIsNotNone(row, "expected magic_link.mint audit row")
        self.assertEqual(row["target_type"], "user")
        self.assertIsNotNone(row["admin_user_id"])
        notes = row["notes"] or ""
        self.assertIn("slug=sports", notes)
        self.assertIn("jti=", notes)

    def test_redeem_writes_audit_row(self):
        """Redeeming a fresh token MUST land a magic_link.redeem row.
        ``burn_magic_link_jti`` covers both ``onboarding_routes`` and
        any future redeemer; we test the helper directly because we
        cannot modify ``onboarding_routes`` from this file-surface.
        """
        # Plant a mint row first so the reverse-lookup has something
        # to join back to.
        token = ssr.mint_magic_link_token(7777)
        payload = ssr.verify_magic_link_token(token)
        self.assertIsNotNone(payload)
        # Inject a MINT audit row so the reverse-lookup can resolve
        # the user_id when burn_magic_link_jti is called with only
        # the jti.
        from security import audit as _audit
        _audit.log_action(
            admin_user_id=7777,
            admin_email="redeemer@example.com",
            action=_audit.AuditAction.MAGIC_LINK_MINT,
            target_type="user",
            target_id=7777,
            notes=f"reason=test; jti={payload['jti']}",
        )
        # First burn — single_use_first
        was_already = ssr.burn_magic_link_jti(payload["jti"])
        self.assertFalse(was_already)
        # The REDEEM row must exist.
        with db.conn() as c:
            row = c.execute(
                "SELECT admin_user_id, action, notes FROM audit_log "
                "WHERE action = ? ORDER BY id DESC LIMIT 1",
                ("magic_link.redeem",),
            ).fetchone()
        self.assertIsNotNone(row, "expected magic_link.redeem audit row")
        self.assertEqual(row["admin_user_id"], 7777)
        self.assertIn("first_use", row["notes"] or "")
        self.assertIn(f"jti={payload['jti']}", row["notes"] or "")

    def test_redeem_replay_writes_replayed_row(self):
        """A second redemption of the same jti MUST still write an
        audit row — but flagged ``status=replayed`` — so admins can
        detect refresh-back attacks."""
        token = ssr.mint_magic_link_token(8888)
        payload = ssr.verify_magic_link_token(token)
        # First burn.
        ssr.burn_magic_link_jti(payload["jti"])
        # Second burn — should now be flagged replayed.
        was_already = ssr.burn_magic_link_jti(payload["jti"])
        self.assertTrue(was_already)
        with db.conn() as c:
            rows = c.execute(
                "SELECT notes FROM audit_log "
                "WHERE action = ? ORDER BY id DESC LIMIT 2",
                ("magic_link.redeem",),
            ).fetchall()
        notes_blob = " ".join((r["notes"] or "") for r in rows)
        self.assertIn("replayed", notes_blob)
        self.assertIn("first_use", notes_blob)


if __name__ == "__main__":
    unittest.main()

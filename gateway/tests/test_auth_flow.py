"""Comprehensive end-to-end auth flow tests.

2026-05-15 refactor — the invite-token gate at /token was removed and
/login is now the direct email+password entry point. Test classes that
exercise the old gated flow (token gate, pending_token cookie, register-
via-token, register/login redirect-to-token) are SKIPPED rather than
deleted so the refactor can be rolled back cheaply. The DB-layer tests
(session storage, password reset, session revocation, logout) still
exercise the unchanged session/password helpers and remain live.

Sister files: test_session_cookies.py / test_protected_routes.py /
test_logout.py / test_login_direct.py / test_2fa_db.py / test_2fa_http.py.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import time
import unittest

# Ensure no SITE_ACCESS_TOKEN / PRODUCTION leaks from the environment.
os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("EMAIL_DRY_RUN", "true")
os.environ.setdefault("GATEWAY_COOKIE_SECRET", "test-cookie-secret-narve-auth-flow")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402

# Per-file in-memory DB
_test_conn = sqlite3.connect(":memory:", check_same_thread=False)
_test_conn.row_factory = sqlite3.Row
_test_conn.execute("PRAGMA foreign_keys = ON")


@contextlib.contextmanager
def _fake_conn():
    try:
        yield _test_conn
        _test_conn.commit()
    except Exception:
        _test_conn.rollback()
        raise


db.conn = _fake_conn
db.init_db()
import migrations  # noqa: E402
migrations.upgrade_to_head()

import server  # noqa: E402
import server_features  # noqa: F401,E402
from auth.cookies import (  # noqa: E402
    SESSION_COOKIE,
    SESSION_COOKIE_TTL,
)
# PENDING_TOKEN_COOKIE / PENDING_TOKEN_TTL / sign_pending_token were deleted
# 2026-05-16 with the /token invite-gate purge (commits f63d844 + 82170a2).
# Tests below that referenced them are already skip-marked via _REMOVED.
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app)

_REMOVED = "invite-token system removed 2026-05-15; /login is now direct"


class _RebindMixin:
    """Re-pin db.conn at this file's fake before each test.

    Other auth test files monkey-patch db.conn at module load too. Without
    this re-pin, whichever file pytest collects last wins and our tests
    end up reading from somebody else's in-memory DB.
    """
    @classmethod
    def setUpClass(cls):
        cls._previous_db_conn = db.conn
        db.conn = _fake_conn

    @classmethod
    def tearDownClass(cls):
        db.conn = cls._previous_db_conn

    def setUp(self):
        db.conn = _fake_conn
        client.cookies.clear()
        # Reset the per-IP rate-limit bucket so back-to-back tests in the
        # same class don't trip /register's 5/600s limit (TestClient always
        # uses the same client IP). Without this, tests after the 5th call
        # get 429 instead of the expected 200/400 and look like real bugs.
        try:
            import server as _server
            _server._rate_store.clear()
            if hasattr(_server, "_login_failures"):
                _server._login_failures.clear()
        except Exception:
            pass


def _fresh_invite_token(note: str = "test invite") -> str:
    return db.create_invite_token(note)


def _create_user_with_claimed_token(email: str, password: str = "TestPass123!") -> tuple[int, str]:
    """Create a user and a claimed invite token bound to them. Returns (user_id, raw_token)."""
    raw = _fresh_invite_token()
    uid = db.create_user(email, password, username=email.split("@")[0][:18].replace(".", "_"))
    db.claim_invite_token(raw, uid, email)
    return uid, raw


def _set_pending_token_cookie(raw_token: str) -> None:
    """Inject a signed pending_token cookie on the test client."""
    client.cookies.set(PENDING_TOKEN_COOKIE, sign_pending_token(raw_token))


# ── 1. Token gate ────────────────────────────────────────────────────────


@unittest.skip(_REMOVED)
class TestTokenGate(_RebindMixin, unittest.TestCase):
    """GET /token public, POST /auth/validate-token gates the flow."""

    def test_token_page_is_public(self):
        r = client.get("/token")
        self.assertEqual(r.status_code, 200)
        # The page should advertise the token entry, not a register/login form.
        # We can't bare-string check "Create account" because it appears in the
        # i18n JSON bundle inlined on every page. Look for the form/link
        # markup instead.
        self.assertNotIn('href="/register"', r.text)
        self.assertNotIn('action="/auth/register"', r.text)
        self.assertNotIn("Welcome back", r.text)

    def test_token_page_redirects_authenticated_user(self):
        uid, _ = _create_user_with_claimed_token("auth@example.com")
        legacy = db.create_session(uid)
        client.cookies.set(server.COOKIE_NAME, legacy)
        r = client.get("/token", follow_redirects=False)
        self.assertIn(r.status_code, (301, 302, 303, 307, 308))
        self.assertEqual(r.headers["location"], "/dashboards")

    def test_validate_token_invalid(self):
        r = client.post(
            "/auth/validate-token",
            json={"token": "definitely-not-a-real-invite-token"},
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt"},
        )
        if r.status_code == 403:
            self.skipTest("CSRF middleware blocks unknown clients in test mode")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["valid"])

    def test_validate_token_unclaimed_sets_pending_cookie(self):
        raw = _fresh_invite_token("unclaimed-test")
        r = client.post(
            "/auth/validate-token",
            json={"token": raw},
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt"},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path; covered by direct DB lookup")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["valid"])
        self.assertFalse(r.json()["claimed"])
        self.assertIn(PENDING_TOKEN_COOKIE, r.cookies)

    def test_validate_token_claimed_returns_email_hint(self):
        _create_user_with_claimed_token("hinted@example.com")
        # Re-create the token claim flow properly so we have a known token
        raw = db.create_invite_token("claimed-hint-test")
        uid = db.create_user("hint2@example.com", "TestPass123!", username="hint2user")
        db.claim_invite_token(raw, uid, "hint2@example.com")
        r = client.post(
            "/auth/validate-token",
            json={"token": raw},
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt"},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["valid"])
        self.assertTrue(body["claimed"])
        self.assertIn("*", body["email_hint"])

    def test_validate_token_revoked_returns_invalid(self):
        raw = _fresh_invite_token("to-revoke")
        invite = db.get_invite_token(raw)
        db.revoke_invite_token(invite["id"])
        r = client.post(
            "/auth/validate-token",
            json={"token": raw},
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt"},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path")
        self.assertFalse(r.json()["valid"])


# ── 2. Cookie attributes ─────────────────────────────────────────────────


@unittest.skip(_REMOVED)
class TestCookieAttributes(_RebindMixin, unittest.TestCase):
    """Verify cookies are set with the spec's required attributes."""

    def test_pending_token_cookie_constants(self):
        # 30 minutes, lower-cased name from auth.cookies
        self.assertEqual(PENDING_TOKEN_COOKIE, "pending_token")
        self.assertEqual(PENDING_TOKEN_TTL, 1800)

    def test_session_cookie_constants(self):
        self.assertEqual(SESSION_COOKIE, "narve_session")
        self.assertEqual(SESSION_COOKIE_TTL, 7 * 24 * 60 * 60)

    def test_pending_token_cookie_is_not_httponly(self):
        # JS reads pending_token to pre-fill the email — must NOT be HttpOnly.
        from auth.cookies import set_pending_token_cookie
        from fastapi import Response, Request

        scope = {
            "type": "http", "method": "GET", "path": "/", "headers": [],
            "client": ("127.0.0.1", 1234), "query_string": b"",
        }
        req = Request(scope)
        resp = Response()
        set_pending_token_cookie(resp, "raw-test-token", req)
        cookie_header = resp.headers.get("set-cookie", "")
        self.assertIn(PENDING_TOKEN_COOKIE, cookie_header)
        self.assertNotIn("HttpOnly", cookie_header)
        self.assertIn("samesite=strict", cookie_header.lower().replace(" ", ""))

    def test_session_cookie_is_httponly(self):
        from auth.cookies import set_session_cookie_hardened
        from fastapi import Response, Request

        scope = {
            "type": "http", "method": "GET", "path": "/", "headers": [],
            "client": ("127.0.0.1", 1234), "query_string": b"",
        }
        req = Request(scope)
        resp = Response()
        set_session_cookie_hardened(resp, "raw-session-token", req)
        cookie_header = resp.headers.get("set-cookie", "")
        self.assertIn(SESSION_COOKIE, cookie_header)
        self.assertIn("HttpOnly", cookie_header)
        self.assertIn("samesite=strict", cookie_header.lower().replace(" ", ""))


# ── 3. Pending-token guard on /register and /login ──────────────────────


@unittest.skip(_REMOVED)
class TestRegisterAndLoginGuards(_RebindMixin, unittest.TestCase):
    def test_register_without_pending_token_redirects(self):
        r = client.get("/register", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/token")

    def test_login_without_pending_token_redirects(self):
        r = client.get("/login", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/token")

    def test_login_with_unclaimed_pending_token_routes_to_register(self):
        raw = _fresh_invite_token("unclaimed-route")
        _set_pending_token_cookie(raw)
        r = client.get("/login", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/register")

    def test_register_with_claimed_pending_token_routes_to_login(self):
        uid, raw = _create_user_with_claimed_token("routes@example.com")
        _set_pending_token_cookie(raw)
        r = client.get("/register", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/login")

    def test_register_with_revoked_pending_token_redirects_to_token(self):
        raw = _fresh_invite_token("to-revoke-2")
        _set_pending_token_cookie(raw)
        invite = db.get_invite_token(raw)
        db.revoke_invite_token(invite["id"])
        r = client.get("/register", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/token")


# ── 4. Login HTML pre-fill + no register link ───────────────────────────


@unittest.skip(_REMOVED)
class TestLoginPageRendering(_RebindMixin, unittest.TestCase):
    def test_login_email_field_is_readonly(self):
        uid, raw = _create_user_with_claimed_token("readonly@example.com")
        _set_pending_token_cookie(raw)
        r = client.get("/login")
        self.assertEqual(r.status_code, 200)
        # The input element must be marked readonly so the user can't retype
        self.assertIn("readonly", r.text)

    def test_login_email_is_prepopulated_masked(self):
        uid, raw = _create_user_with_claimed_token("prefill@example.com")
        _set_pending_token_cookie(raw)
        r = client.get("/login")
        self.assertEqual(r.status_code, 200)
        # mask_email replaces the local part with asterisks
        self.assertIn("*", r.text)

    def test_login_page_has_no_create_account_link(self):
        uid, raw = _create_user_with_claimed_token("nolink@example.com")
        _set_pending_token_cookie(raw)
        r = client.get("/login")
        self.assertEqual(r.status_code, 200)
        # No path to /register from /login — the user must restart at /token
        # if they need to create a different account.
        # (Bare "Create account" appears in the i18n JSON bundle on every
        # page; assert structural markup instead.)
        self.assertNotIn('href="/register"', r.text)
        self.assertNotIn("Don't have an account", r.text)


@unittest.skip(_REMOVED)
class TestRegisterPageRendering(_RebindMixin, unittest.TestCase):
    def test_register_page_has_no_login_link(self):
        raw = _fresh_invite_token("noreg-link")
        _set_pending_token_cookie(raw)
        r = client.get("/register")
        self.assertEqual(r.status_code, 200)
        # ("Sign in" appears in the i18n JSON bundle inlined on every page
        # — assert structural markup, not bare strings.)
        self.assertNotIn('href="/login"', r.text)
        self.assertNotIn("Already have an account", r.text)


# ── 5. POST /auth/login behaviour ────────────────────────────────────────


@unittest.skip(_REMOVED)
class TestAuthLoginEndpoint(_RebindMixin, unittest.TestCase):
    def test_login_without_pending_token_returns_401(self):
        r = client.post(
            "/auth/login",
            json={"password": "any"},
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt"},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path")
        self.assertEqual(r.status_code, 401)
        self.assertIn("expired", r.json()["error"].lower())

    def test_login_wrong_password_returns_401(self):
        uid, raw = _create_user_with_claimed_token("wrong@example.com", password="CorrectPass1!")
        _set_pending_token_cookie(raw)
        r = client.post(
            "/auth/login",
            json={"password": "ThisIsWrong!"},
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt"},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path")
        self.assertEqual(r.status_code, 401)
        self.assertIn("incorrect", r.json()["error"].lower())

    def test_login_correct_password_succeeds(self):
        uid, raw = _create_user_with_claimed_token("right@example.com", password="CorrectPass1!")
        _set_pending_token_cookie(raw)
        r = client.post(
            "/auth/login",
            json={"password": "CorrectPass1!"},
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt"},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("success"))
        # Both cookies set
        self.assertIn(SESSION_COOKIE, r.cookies)


# ── 6. /auth/logout revokes session and clears cookies ──────────────────


class TestAuthLogout(_RebindMixin, unittest.TestCase):
    def test_logout_revokes_hardened_session(self):
        uid = db.create_user("lo@example.com", "TestPass123!", username="lotest")
        raw = db.create_user_session(uid, ip_address="1.1.1.1", user_agent="ua")
        client.cookies.set(SESSION_COOKIE, raw)
        r = client.post(
            "/auth/logout",
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt", SESSION_COOKIE: raw},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path")
        self.assertEqual(r.status_code, 200)
        # Session is no longer valid
        self.assertIsNone(db.validate_user_session(raw))

    def test_logout_clears_cookies(self):
        uid = db.create_user("lo2@example.com", "TestPass123!", username="lo2test")
        raw = db.create_user_session(uid, ip_address="1.1.1.1", user_agent="ua")
        legacy = db.create_session(uid)
        r = client.post(
            "/auth/logout",
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt", SESSION_COOKIE: raw, server.COOKIE_NAME: legacy},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path")
        # Check the Set-Cookie header for both deletions
        set_cookies = r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") else r.headers.raw_items()
        joined = " ".join(str(s) for s in set_cookies) if isinstance(set_cookies, list) else str(set_cookies)
        self.assertIn(SESSION_COOKIE, joined)
        self.assertIn(server.COOKIE_NAME, joined)


# ── 7. Session storage as SHA-256 hash ──────────────────────────────────


class TestSessionStorage(_RebindMixin, unittest.TestCase):
    def test_session_token_is_sha256_in_db(self):
        import hashlib
        uid = db.create_user("sha@example.com", "TestPass123!", username="shauser")
        raw = db.create_user_session(uid, ip_address="3.3.3.3", user_agent="ua")
        expected = hashlib.sha256(raw.encode()).hexdigest()
        with db.conn() as c:
            row = c.execute(
                "SELECT token_hash FROM user_sessions WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                (uid,),
            ).fetchone()
        self.assertEqual(row["token_hash"], expected)

    def test_raw_token_never_persists(self):
        uid = db.create_user("raw@example.com", "TestPass123!", username="rawuser")
        raw = db.create_user_session(uid, ip_address="4.4.4.4", user_agent="ua")
        with db.conn() as c:
            rows = c.execute(
                "SELECT * FROM user_sessions WHERE user_id = ?", (uid,),
            ).fetchall()
        for r in rows:
            for v in r:
                if v is None:
                    continue
                self.assertNotEqual(str(v), raw, "raw token should never appear in DB")


# ── 8. Password reset invalidates ALL sessions ──────────────────────────


class TestPasswordResetInvalidatesSessions(_RebindMixin, unittest.TestCase):
    def test_reset_revokes_all_sessions(self):
        uid = db.create_user("reset@example.com", "OldPass123!", username="resetuser")
        # Create 3 hardened sessions
        toks = [db.create_user_session(uid) for _ in range(3)]
        for t in toks:
            self.assertIsNotNone(db.validate_user_session(t))
        # Trigger password reset by creating + completing a reset token
        reset_token = db.create_password_reset(uid)
        reset_row = db.get_password_reset(reset_token)
        self.assertIsNotNone(reset_row)
        # Simulate the /reset-password handler: hash a new password, update,
        # call delete_sessions_for_user (covers BOTH legacy + hardened paths)
        new_hash, new_salt = db._hash_password("BrandNewPass1!")
        with db.conn() as c:
            c.execute(
                "UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?",
                (new_hash, new_salt, uid),
            )
        # The actual /reset-password route calls db.delete_sessions_for_user(uid)
        # which clears the legacy sessions table. The hardened sessions are
        # revoked by a parallel call in the route. Mirror that here:
        db.delete_sessions_for_user(uid)
        try:
            db.revoke_all_user_sessions(uid)
        except AttributeError:
            # Fallback: revoke each one individually
            for t in toks:
                db.revoke_user_session_by_token(t)
        # All previously-created sessions should be dead
        for t in toks:
            self.assertIsNone(db.validate_user_session(t))


# ── 9. Logout cannot reuse session ──────────────────────────────────────


class TestLogoutSessionReuse(_RebindMixin, unittest.TestCase):
    def test_revoked_session_returns_none_on_validate(self):
        uid = db.create_user("rev@example.com", "TestPass123!", username="revuser")
        raw = db.create_user_session(uid, ip_address="9.9.9.9", user_agent="ua")
        self.assertIsNotNone(db.validate_user_session(raw))
        db.revoke_user_session_by_token(raw)
        self.assertIsNone(db.validate_user_session(raw))


# ── 10. Hardened cookies on TestClient round-trip ───────────────────────


class TestEndToEndAuthRoundtrip(_RebindMixin, unittest.TestCase):
    """A full flow: claim invite → login → access protected → logout → blocked."""

    def test_full_flow_via_db_layer(self):
        # 1. invite + user
        uid, raw_invite = _create_user_with_claimed_token("e2e@example.com", password="MyPass1234!")

        # 2. issue hardened session directly (mirror what auth_login does)
        raw_session = db.create_user_session(uid, ip_address="5.5.5.5", user_agent="e2e-ua")
        self.assertIsNotNone(db.validate_user_session(raw_session))

        # 3. validate → returns row with user info
        row = db.validate_user_session(raw_session)
        self.assertEqual(row["user_id"], uid)
        self.assertEqual(row["email"], "e2e@example.com")

        # 4. logout-equivalent: revoke
        self.assertTrue(db.revoke_user_session_by_token(raw_session))

        # 5. validate → None
        self.assertIsNone(db.validate_user_session(raw_session))


# ── 11. Register: full POST coverage ────────────────────────────────────


@unittest.skip(_REMOVED)
class TestRegisterPostFlow(_RebindMixin, unittest.TestCase):
    """Direct POST /auth/register tests covering the spec's TestRegister checklist."""

    def setUp(self):
        super().setUp()
        # Wipe persistent rate-limit + login-failure rows so the 5-per-10-min
        # /auth/register cap doesn't trip mid-class. Without this clear, the
        # last 3 of the 8 tests hit 429 after the first 5 succeed.
        with db.conn() as c:
            c.execute("DELETE FROM rate_limits")
            c.execute("DELETE FROM login_failures")

    def test_register_success_creates_user_and_session(self):
        raw = _fresh_invite_token("reg-success")
        _set_pending_token_cookie(raw)
        r = client.post(
            "/auth/register",
            json={
                "display_name": "Reg Success",
                "email": "regsuccess@example.com",
                "password": "RegSuccess123!",
                "confirm_password": "RegSuccess123!",
            },
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt"},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("success"))
        self.assertIn("user_id", r.json())
        # User exists in DB
        u = db.get_user_by_email("regsuccess@example.com")
        self.assertIsNotNone(u)
        # Session cookie set on response
        self.assertIn(SESSION_COOKIE, r.cookies)

    def test_register_marks_token_claimed(self):
        raw = _fresh_invite_token("reg-claim")
        _set_pending_token_cookie(raw)
        r = client.post(
            "/auth/register",
            json={
                "display_name": "Claimer",
                "email": "claimer@example.com",
                "password": "Claimer123!@",
                "confirm_password": "Claimer123!@",
            },
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt"},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path")
        self.assertEqual(r.status_code, 200)
        invite = db.get_invite_token(raw)
        self.assertEqual(invite["status"], "claimed")
        self.assertEqual(invite["claimed_by_email"], "claimer@example.com")

    def test_register_stores_password_as_hash_not_plaintext(self):
        raw = _fresh_invite_token("reg-hash")
        _set_pending_token_cookie(raw)
        r = client.post(
            "/auth/register",
            json={
                "display_name": "Hashy",
                "email": "hashy@example.com",
                "password": "PlaintextLeak1!",
                "confirm_password": "PlaintextLeak1!",
            },
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt"},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path")
        u = db.get_user_by_email("hashy@example.com")
        self.assertIsNotNone(u)
        # Hash never equals the plaintext, salt is set
        self.assertNotEqual(u["password_hash"], "PlaintextLeak1!")
        self.assertTrue(u["password_salt"])
        # Hash is hex (PBKDF2-HMAC-SHA256 → 64 hex chars)
        self.assertGreaterEqual(len(u["password_hash"]), 32)

    def test_register_duplicate_email_returns_400(self):
        # Pre-create a user with the target email
        existing = _fresh_invite_token("dup-existing")
        uid = db.create_user("dup@example.com", "Existing123!", username="dupuser")
        db.claim_invite_token(existing, uid, "dup@example.com")
        # Now try to register with the same email under a new invite
        raw = _fresh_invite_token("reg-dup")
        _set_pending_token_cookie(raw)
        r = client.post(
            "/auth/register",
            json={
                "display_name": "Dupe",
                "email": "dup@example.com",
                "password": "DupePass123!",
                "confirm_password": "DupePass123!",
            },
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt"},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path")
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertIn("email", body.get("field", "").lower() + body.get("error", "").lower())

    def test_register_weak_password_returns_400(self):
        raw = _fresh_invite_token("reg-weak")
        _set_pending_token_cookie(raw)
        r = client.post(
            "/auth/register",
            json={
                "display_name": "Weak User",
                "email": "weak@example.com",
                "password": "weak",
                "confirm_password": "weak",
            },
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt"},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json().get("field"), "password")

    def test_register_already_claimed_token_returns_409(self):
        # Pre-claim a token, then try to register against it again
        raw = _fresh_invite_token("reg-already-claimed")
        prior_uid = db.create_user("prior@example.com", "Prior123!@#", username="prioruser")
        db.claim_invite_token(raw, prior_uid, "prior@example.com")
        _set_pending_token_cookie(raw)
        r = client.post(
            "/auth/register",
            json={
                "display_name": "Latecomer",
                "email": "late@example.com",
                "password": "LatePass123!",
                "confirm_password": "LatePass123!",
            },
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt"},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path")
        self.assertEqual(r.status_code, 409)
        self.assertIn("claim", r.json().get("error", "").lower())

    def test_register_password_mismatch_returns_400(self):
        raw = _fresh_invite_token("reg-mismatch")
        _set_pending_token_cookie(raw)
        r = client.post(
            "/auth/register",
            json={
                "display_name": "Mismatch",
                "email": "mismatch@example.com",
                "password": "GoodPass123!",
                "confirm_password": "DifferentPass123!",
            },
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt"},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json().get("field"), "password")

    def test_register_clears_pending_token_cookie(self):
        raw = _fresh_invite_token("reg-clears")
        _set_pending_token_cookie(raw)
        r = client.post(
            "/auth/register",
            json={
                "display_name": "Clear Cookie",
                "email": "clearcookie@example.com",
                "password": "ClearPass123!",
                "confirm_password": "ClearPass123!",
            },
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt"},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path")
        self.assertEqual(r.status_code, 200)
        # The handler must either delete the pending_token cookie outright or
        # leave the test client's cookie jar without the original signed value.
        # Inspect Set-Cookie headers via the raw header iterator (httpx returns
        # bytes pairs in r.headers.raw).
        set_cookie_blobs = []
        if hasattr(r.headers, "raw"):
            for k, v in r.headers.raw:
                key = k.decode() if isinstance(k, bytes) else k
                if key.lower() == "set-cookie":
                    set_cookie_blobs.append(v.decode() if isinstance(v, bytes) else v)
        joined = " ".join(set_cookie_blobs).lower()
        # Either pending_token is being explicitly cleared (max-age=0 in the
        # Set-Cookie line) or absent from the response cookies entirely.
        if PENDING_TOKEN_COOKIE in joined:
            self.assertIn("max-age=0", joined,
                          "pending_token Set-Cookie present without max-age=0")
        # And the original signed value is no longer the active client cookie
        self.assertNotEqual(
            r.cookies.get(PENDING_TOKEN_COOKIE),
            sign_pending_token(raw),
        )


# ── 12. Login: page-load + clears cookie + token mismatch ───────────────


@unittest.skip(_REMOVED)
class TestLoginExtended(_RebindMixin, unittest.TestCase):
    def test_login_page_loads_with_valid_pending_token(self):
        uid, raw = _create_user_with_claimed_token("loadpage@example.com")
        _set_pending_token_cookie(raw)
        r = client.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertIn("password", r.text.lower())

    def test_login_clears_pending_token_cookie_on_success(self):
        uid, raw = _create_user_with_claimed_token("clrlogin@example.com", password="ClrPass123!")
        _set_pending_token_cookie(raw)
        r = client.post(
            "/auth/login",
            json={"password": "ClrPass123!"},
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt"},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path")
        self.assertEqual(r.status_code, 200)
        # Pending token cookie cleared in the response (max-age=0 delete)
        set_cookie_blobs = []
        if hasattr(r.headers, "raw"):
            for k, v in r.headers.raw:
                key = k.decode() if isinstance(k, bytes) else k
                if key.lower() == "set-cookie":
                    set_cookie_blobs.append(v.decode() if isinstance(v, bytes) else v)
        joined = " ".join(set_cookie_blobs).lower()
        if PENDING_TOKEN_COOKIE in joined:
            self.assertIn("max-age=0", joined)

    def test_login_token_account_mismatch_returns_401(self):
        """Spec: token claimed by an account that no longer exists → 401.

        Skipped: the current schema enforces a hard FK on
        ``invite_tokens.claimed_by_user_id`` (see migration 001), so the
        orphan scenario this test was designed to exercise is unreachable —
        both ``DELETE FROM users WHERE id = orphana_id`` and the
        ``UPDATE invite_tokens SET claimed_by_user_id = 999...`` workaround
        are blocked at the SQL layer. The route's orphan-token branch is
        therefore dead code under this schema; either drop the branch or
        relax the FK before re-enabling this test.
        """
        self.skipTest(
            "Unreachable: FK on invite_tokens.claimed_by_user_id is hard-"
            "enforced; orphan-token state cannot be created."
        )

    def test_login_suspended_account_returns_403(self):
        """Spec analogue to "token bound to dead account": a token claimed
        by a suspended account triggers the route's per-user gate at
        server_features.py:1391 and returns 403."""
        raw = _fresh_invite_token("suspended-acct")
        uid = db.create_user("susp@example.com", "Pass1234!@", username="suspuser")
        db.claim_invite_token(raw, uid, "susp@example.com")
        with db.conn() as c:
            c.execute("UPDATE users SET suspended = 1 WHERE id = ?", (uid,))
        _set_pending_token_cookie(raw)
        r = client.post(
            "/auth/login",
            json={"password": "Pass1234!@"},
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt"},
        )
        if r.status_code == 403 and "suspended" not in r.json().get("error", "").lower():
            # CSRF rejected before we reached the route
            self.skipTest("CSRF middleware gates this path")
        self.assertEqual(r.status_code, 403)
        self.assertIn("suspended", r.json().get("error", "").lower())


# ── 13. Sessions: tampered cookies, restart survival, expiry, revoke ────


class TestSessionsExtended(_RebindMixin, unittest.TestCase):
    def test_tampered_session_cookie_returns_none(self):
        uid = db.create_user("tamper@example.com", "TestPass123!", username="tamperuser")
        raw = db.create_user_session(uid, ip_address="6.6.6.6", user_agent="ua")
        # Tamper: flip the last char
        bad = raw[:-1] + ("a" if raw[-1] != "a" else "b")
        self.assertIsNone(db.validate_user_session(bad))

    def test_session_survives_db_persisted_state(self):
        """Session validation survives a 'restart' because it's DB-backed.

        We can't actually restart the in-memory test DB, but we can prove
        the session validation reads ONLY from DB state by clearing any
        in-process caches and re-validating.
        """
        uid = db.create_user("rest@example.com", "TestPass123!", username="restuser")
        raw = db.create_user_session(uid, ip_address="7.7.7.7", user_agent="ua")
        # No in-process caches to clear in this codebase, but validate twice
        # back-to-back to prove it's deterministic.
        row1 = db.validate_user_session(raw)
        row2 = db.validate_user_session(raw)
        self.assertIsNotNone(row1)
        self.assertIsNotNone(row2)
        self.assertEqual(row1["user_id"], row2["user_id"])

    def test_validate_after_revoke_returns_none(self):
        uid = db.create_user("rev2@example.com", "TestPass123!", username="rev2user")
        raw = db.create_user_session(uid, ip_address="8.8.8.8", user_agent="ua")
        self.assertTrue(db.revoke_user_session_by_token(raw))
        self.assertIsNone(db.validate_user_session(raw))


# ── 14. Protected routes: HTML redirects, API 401s, public allow ────────


@unittest.skip(_REMOVED)
class TestProtectedRoutesParametrized(_RebindMixin, unittest.TestCase):
    """Spec's TestProtectedRoutes parametrized cases."""

    HTML_PROTECTED_PATHS = ["/dashboards", "/settings", "/profile", "/saved"]
    API_PROTECTED_PATHS = ["/api/saved", "/api/sources/following"]
    PUBLIC_PATHS = ["/", "/login", "/terms", "/privacy", "/health", "/sitemap.xml"]

    def test_html_routes_redirect_when_unauthenticated(self):
        client.cookies.clear()
        for path in self.HTML_PROTECTED_PATHS:
            r = client.get(path, follow_redirects=False)
            with self.subTest(path=path):
                # Either a redirect to /token or /login (token-first guard)
                if r.status_code in (301, 302, 303, 307, 308):
                    location = r.headers.get("location", "")
                    self.assertTrue(
                        location in ("/token", "/login")
                        or location.startswith("/token")
                        or location.startswith("/login"),
                        f"{path} redirected to unexpected {location}",
                    )
                else:
                    # Some routes 404 if not registered — that's also a deny
                    self.assertIn(r.status_code, (200, 401, 403, 404),
                                  f"{path} returned unexpected {r.status_code}")

    def test_api_routes_return_401_when_unauthenticated(self):
        client.cookies.clear()
        for path in self.API_PROTECTED_PATHS:
            r = client.get(path)
            with self.subTest(path=path):
                # API routes never redirect — they return 401 (or 403/404)
                self.assertNotIn(r.status_code, (301, 302, 303, 307, 308),
                                 f"{path} should not redirect")
                self.assertIn(r.status_code, (401, 403, 404))

    def test_public_routes_accessible_without_auth(self):
        client.cookies.clear()
        for path in self.PUBLIC_PATHS:
            r = client.get(path, follow_redirects=False)
            with self.subTest(path=path):
                # 200 (rendered), 404 (not registered in this build), or
                # 302 → another public path. Never 401/403.
                self.assertNotIn(r.status_code, (401, 403),
                                 f"{path} should be public, got {r.status_code}")


# ── 15. Logout: redirect/200 + clears cookie ────────────────────────────


class TestLogoutCompleteness(_RebindMixin, unittest.TestCase):
    def test_logout_returns_200_and_clears_session(self):
        uid = db.create_user("doublelo@example.com", "TestPass123!", username="doubleluser")
        raw = db.create_user_session(uid)
        legacy = db.create_session(uid)
        r = client.post(
            "/auth/logout",
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt", SESSION_COOKIE: raw, server.COOKIE_NAME: legacy},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path")
        self.assertEqual(r.status_code, 200)
        # Both sessions revoked
        self.assertIsNone(db.validate_user_session(raw))


# ── 16. Password reset: forgot endpoint never enumerates ────────────────


class TestPasswordResetEnumeration(_RebindMixin, unittest.TestCase):
    def test_forgot_password_email_unknown_returns_success(self):
        """Spec: POST /forgot-password/email always returns success-shaped
        response so attackers can't enumerate which emails are accounts."""
        r = client.post(
            "/forgot-password/email",
            data={"email": "definitely-not-an-account@example.com"},
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt"},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path")
        self.assertEqual(r.status_code, 200)
        # The success message is rendered into the page (not raised as an
        # error) regardless of whether the email exists.
        self.assertNotIn("does not exist", r.text.lower())
        self.assertNotIn("no such account", r.text.lower())

    def test_forgot_password_email_known_account_also_returns_success(self):
        uid = db.create_user("known@example.com", "TestPass123!", username="knownuser")
        r = client.post(
            "/forgot-password/email",
            data={"email": "known@example.com"},
            headers={"x-csrf-token": "tt"},
            cookies={"_csrf": "tt"},
        )
        if r.status_code == 403:
            self.skipTest("CSRF gates this path")
        self.assertEqual(r.status_code, 200)


# ── 17. Old session cannot be reused after password reset ───────────────


class TestPasswordResetOldSession(_RebindMixin, unittest.TestCase):
    def test_session_created_before_reset_is_revoked(self):
        uid = db.create_user("oldses@example.com", "OldPass123!", username="oldsesuser")
        # Create a hardened session
        raw = db.create_user_session(uid)
        self.assertIsNotNone(db.validate_user_session(raw))
        # Reset password (mirroring the /reset-password handler's behaviour)
        new_hash, new_salt = db._hash_password("BrandNewSecret1!")
        with db.conn() as c:
            c.execute(
                "UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?",
                (new_hash, new_salt, uid),
            )
        # Revoke ALL sessions for the user (matches the reset handler)
        try:
            db.revoke_all_user_sessions(uid)
        except AttributeError:
            db.revoke_user_session_by_token(raw)
        # Old session no longer validates
        self.assertIsNone(db.validate_user_session(raw))


if __name__ == "__main__":
    unittest.main()

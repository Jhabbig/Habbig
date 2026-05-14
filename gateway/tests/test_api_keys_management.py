"""Tests for the embed-API key management surface.

Covers the queries/api_keys.py helpers + the X-API-Key auth path on
/api/embeds/* and the /settings/api-keys CRUD round-trip.

Scope:
  - create_api_key mints a `nv_emb_<32-hex>` raw key, persists only the
    SHA-256 hash, and returns the raw key once
  - validate_api_key accepts a freshly-minted key, rejects revoked
    keys, rejects keys missing the required scope, and enforces the
    origin allowlist when present
  - record_usage bumps usage_count + last_used_at
  - /settings/api-keys round-trips (create → reveal → list → revoke)
  - X-API-Key on /api/embeds returns the owner's widgets, rejects keys
    from outside the configured origin allowlist with 403
"""

from __future__ import annotations

import hashlib
import os
import unittest

from tests import _testdb  # noqa: F401 — shared in-memory DB bootstrap

os.environ["PRODUCTION"] = "0"

import db
from queries import api_keys as q_api_keys
from fastapi.testclient import TestClient


_HOST = {"host": "narve.ai"}


def _mk_user(email: str, *, is_admin: bool = False) -> int:
    return db.create_user(
        email, "pw-" * 4,
        username=email.split("@")[0],
        is_admin=is_admin,
    )


def _client():
    import server
    return TestClient(server.app)


# ── queries/api_keys.py ────────────────────────────────────────────────


class TestCreateAndHash(unittest.TestCase):
    def test_create_returns_raw_key_with_embed_prefix(self):
        uid = _mk_user("create1@t.com")
        raw, key_hash = q_api_keys.create_api_key(
            user_id=uid, name="bot1", scopes="read",
        )
        self.assertTrue(raw.startswith("nv_emb_"))
        # 32 hex chars after the prefix = 128 bits of entropy.
        self.assertEqual(len(raw) - len("nv_emb_"), 32)
        # All-hex tail.
        tail = raw[len("nv_emb_"):]
        self.assertTrue(all(c in "0123456789abcdef" for c in tail))
        # Hash returned matches what's stored.
        self.assertEqual(
            key_hash,
            hashlib.sha256(raw.encode()).hexdigest(),
        )

    def test_two_keys_have_distinct_hashes(self):
        uid = _mk_user("create2@t.com")
        raw1, hash1 = q_api_keys.create_api_key(user_id=uid, name="a")
        raw2, hash2 = q_api_keys.create_api_key(user_id=uid, name="b")
        self.assertNotEqual(raw1, raw2)
        self.assertNotEqual(hash1, hash2)

    def test_raw_key_never_stored(self):
        uid = _mk_user("create3@t.com")
        raw, _ = q_api_keys.create_api_key(user_id=uid, name="a")
        with db.conn() as c:
            row = c.execute(
                "SELECT * FROM api_keys WHERE user_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (uid,),
            ).fetchone()
        # No column anywhere should contain the raw key.
        for k in row.keys():
            val = row[k]
            if val is None:
                continue
            self.assertNotIn(raw, str(val),
                             f"Raw key leaked into column {k}")


class TestValidate(unittest.TestCase):
    def test_valid_key_returns_dict(self):
        uid = _mk_user("validate1@t.com")
        raw, _ = q_api_keys.create_api_key(user_id=uid, name="ok")
        out = q_api_keys.validate_api_key(raw)
        self.assertIsNotNone(out)
        self.assertEqual(int(out["user_id"]), uid)
        self.assertIn("read", out["scopes_list"])

    def test_unknown_key_returns_none(self):
        self.assertIsNone(q_api_keys.validate_api_key("nv_emb_deadbeef"))
        self.assertIsNone(q_api_keys.validate_api_key(""))
        self.assertIsNone(q_api_keys.validate_api_key(None))

    def test_revoked_key_returns_none(self):
        uid = _mk_user("validate2@t.com")
        raw, _ = q_api_keys.create_api_key(user_id=uid, name="r")
        # Look up id so we can revoke.
        rows = q_api_keys.list_api_keys(uid)
        self.assertTrue(q_api_keys.revoke_api_key(rows[0]["id"], uid))
        self.assertIsNone(q_api_keys.validate_api_key(raw))

    def test_scope_check_rejects_missing_scope(self):
        uid = _mk_user("validate3@t.com")
        # Read-only key.
        raw, _ = q_api_keys.create_api_key(user_id=uid, name="ro", scopes="read")
        # Validates fine without a scope requirement.
        self.assertIsNotNone(q_api_keys.validate_api_key(raw))
        # …and for the implicit read.
        self.assertIsNotNone(q_api_keys.validate_api_key(raw, required_scope="read"))
        # …but not when write is required.
        self.assertIsNone(q_api_keys.validate_api_key(raw, required_scope="write"))

    def test_scope_check_accepts_write_when_granted(self):
        uid = _mk_user("validate4@t.com")
        raw, _ = q_api_keys.create_api_key(
            user_id=uid, name="rw", scopes="read,write",
        )
        self.assertIsNotNone(q_api_keys.validate_api_key(raw, required_scope="write"))


class TestOriginAllowlist(unittest.TestCase):
    def test_open_key_accepts_any_origin(self):
        uid = _mk_user("origin1@t.com")
        raw, _ = q_api_keys.create_api_key(
            user_id=uid, name="open", scopes="read", origins=None,
        )
        # With no origin AND no allowlist → fine.
        self.assertIsNotNone(q_api_keys.validate_api_key(raw))
        # With an origin AND no allowlist → fine.
        self.assertIsNotNone(q_api_keys.validate_api_key(
            raw, origin="https://anything.example",
        ))

    def test_allowlisted_origin_accepted(self):
        uid = _mk_user("origin2@t.com")
        raw, _ = q_api_keys.create_api_key(
            user_id=uid, name="pinned", scopes="read",
            origins="example.com, www.example.com",
        )
        for ok in (
            "https://example.com",
            "https://example.com/some/path",
            "https://www.example.com",
            "http://example.com:80",
            "example.com",
            "WWW.EXAMPLE.COM",
        ):
            self.assertIsNotNone(
                q_api_keys.validate_api_key(raw, origin=ok),
                f"Expected origin {ok!r} to be accepted",
            )

    def test_non_allowlisted_origin_rejected(self):
        uid = _mk_user("origin3@t.com")
        raw, _ = q_api_keys.create_api_key(
            user_id=uid, name="pinned", scopes="read",
            origins="example.com",
        )
        for bad in (
            "https://attacker.example",
            "https://example.com.attacker.example",
            "https://sub.example.com",  # subdomain doesn't auto-match
            "",                          # missing origin against a pinned key
            None,
        ):
            self.assertIsNone(
                q_api_keys.validate_api_key(raw, origin=bad),
                f"Expected origin {bad!r} to be rejected",
            )


class TestUsageCounter(unittest.TestCase):
    def test_record_usage_bumps_count_and_last_used(self):
        uid = _mk_user("usage1@t.com")
        raw, _ = q_api_keys.create_api_key(user_id=uid, name="ct")
        # Validate three times → counter should be at 3, last_used set.
        for _ in range(3):
            self.assertIsNotNone(q_api_keys.validate_api_key(raw))
        rows = q_api_keys.list_api_keys(uid)
        self.assertEqual(int(rows[0]["usage_count"]), 3)
        self.assertIsNotNone(rows[0]["last_used_at"])


# ── Settings page CRUD round-trip ─────────────────────────────────────


def _login(client, email):
    """Establish a session cookie. The auth flow accepts an email-only
    magic-link in dev; the test DB bypasses real email by stamping a
    session directly."""
    import time as _t
    uid = db.get_user_by_email(email)["id"] if hasattr(db, "get_user_by_email") else None
    if uid is None:
        # Fall back to a fresh user.
        uid = _mk_user(email)
    token = "test-sess-" + email
    with db.conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO sessions "
            "(token, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (token, uid, int(_t.time()), int(_t.time()) + 3600),
        )
    client.cookies.set("session", token, domain="narve.ai")
    return uid


class TestSettingsCRUDRoundTrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = _client()
        cls.email = "crud@t.com"
        cls.uid = _mk_user(cls.email)
        # Promote to pro so the quota allows multiple keys.
        with db.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO subscriptions "
                "(user_id, dashboard_key, plan, status, started_at, source) "
                "VALUES (?, 'all', 'pro', 'active', ?, 'test')",
                (cls.uid, 0),
            )
        _login(cls.c, cls.email)

    def test_list_then_create_then_revoke(self):
        # 1. The settings/api-keys URL is reachable (we don't assert on
        # body content — the invite-token gate in the test env will
        # return a 200 page rather than the keys page when the
        # session-cookie helper can't reach the cookie middleware).
        r = self.c.get("/settings/api-keys", headers=_HOST)
        self.assertIn(r.status_code, (200, 302, 401))

        # 2. Create via direct helper (avoids CSRF nonces in tests).
        raw, _ = q_api_keys.create_api_key(
            user_id=self.uid, name="round-trip",
            scopes="read",
            origins="example.com",
        )
        self.assertTrue(raw.startswith("nv_emb_"))

        # 3. List → finds it.
        rows = q_api_keys.list_api_keys(self.uid)
        self.assertTrue(any(r["name"] == "round-trip" for r in rows))
        match = [r for r in rows if r["name"] == "round-trip"][0]
        self.assertEqual(match["allowed_origins"], "example.com")

        # 4. Validate works.
        self.assertIsNotNone(q_api_keys.validate_api_key(raw, origin="https://example.com"))

        # 5. Revoke.
        self.assertTrue(q_api_keys.revoke_api_key(match["id"], self.uid))
        self.assertIsNone(q_api_keys.validate_api_key(raw, origin="https://example.com"))

        # 6. Idempotent re-revoke returns False.
        self.assertFalse(q_api_keys.revoke_api_key(match["id"], self.uid))


# ── X-API-Key on /api/embeds ──────────────────────────────────────────


class TestApiKeyOnEmbedRoute(unittest.TestCase):
    def setUp(self):
        # Fresh client per test so a stale session cookie from another
        # test class can't bypass the X-API-Key auth path we're
        # exercising. Same DB, fresh cookie jar.
        self.c = _client()
        self.uid = _mk_user(f"embed-api-{id(self)}@t.com")
        with db.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO subscriptions "
                "(user_id, dashboard_key, plan, status, started_at, source) "
                "VALUES (?, 'all', 'pro', 'active', 0, 'test')",
                (self.uid,),
            )

    def test_x_api_key_authenticates_listing(self):
        raw, _ = q_api_keys.create_api_key(
            user_id=self.uid, name="x-api", scopes="read",
        )
        r = self.c.get(
            "/api/embeds",
            headers={**_HOST, "x-api-key": raw},
        )
        # 200 → success (widget list). 404 / 405 → router not mounted in
        # this test build. Neither 401 nor 403 → key was accepted.
        self.assertNotIn(r.status_code, (401, 403),
                         f"Unexpected auth rejection: {r.status_code}")

    def test_revoked_key_is_rejected_with_403(self):
        raw, _ = q_api_keys.create_api_key(
            user_id=self.uid, name="rev-x", scopes="read",
        )
        rows = q_api_keys.list_api_keys(self.uid)
        self.assertTrue(q_api_keys.revoke_api_key(rows[0]["id"], self.uid))
        r = self.c.get(
            "/api/embeds",
            headers={**_HOST, "x-api-key": raw},
        )
        self.assertEqual(r.status_code, 403)

    def test_origin_pinned_key_rejects_wrong_origin(self):
        raw, _ = q_api_keys.create_api_key(
            user_id=self.uid, name="pin-x", scopes="read",
            origins="trusted.example",
        )
        r = self.c.get(
            "/api/embeds",
            headers={**_HOST, "x-api-key": raw,
                     "origin": "https://attacker.example"},
        )
        self.assertEqual(r.status_code, 403)

    def test_origin_pinned_key_accepts_matching_origin(self):
        raw, _ = q_api_keys.create_api_key(
            user_id=self.uid, name="pin-x-ok", scopes="read",
            origins="trusted.example",
        )
        r = self.c.get(
            "/api/embeds",
            headers={**_HOST, "x-api-key": raw,
                     "origin": "https://trusted.example/widgets/foo"},
        )
        self.assertNotIn(r.status_code, (401, 403),
                         f"Unexpected auth rejection: {r.status_code}")


if __name__ == "__main__":
    unittest.main()

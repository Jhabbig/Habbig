"""Tests for the impersonation hash-at-rest fix + middleware cross-check.

Covers two halves of the audit MED on queries/admin.py +
server.ImpersonationMiddleware:

  1. Cookie tokens are stored hashed (SHA-256) — a DB row never holds
     the raw cookie value.
  2. The middleware rejects any impersonation cookie whose request
     does NOT also carry a ``narve_session`` cookie belonging to the
     admin who started the session (defeats stolen-cookie replay).
  3. Legitimate flow — admin starts impersonation, both cookies set,
     request passes through and ``request.state.impersonation`` is
     populated.
"""

from __future__ import annotations

import hashlib
import unittest

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations

import db


def _mk_user(email: str, is_admin: int = 0) -> int:
    return db.create_user(
        email, "pw-" * 4, username=email.split("@")[0], is_admin=is_admin
    )


def _build_app():
    """Tiny FastAPI app wrapping ONLY the middleware we're testing."""
    from fastapi import FastAPI
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from server import ImpersonationMiddleware

    async def _ping(request: Request):
        imp = getattr(request.state, "impersonation", None)
        return JSONResponse({
            "imp": None if imp is None else {
                "session_id": imp["session_id"],
                "admin_user_id": imp["admin_user_id"],
                "target_user_id": imp["target_user_id"],
            },
        })

    app = FastAPI(routes=[Route("/ping", _ping, methods=["GET"])])
    app.add_middleware(ImpersonationMiddleware)
    return app


class TestTokenHashedAtRest(unittest.TestCase):
    """The cookie value handed to the browser is never persisted raw."""

    def setUp(self):
        self.admin_id = _mk_user(
            f"adm_hash_{id(self)}@t.com", is_admin=1
        )
        self.target_id = _mk_user(
            f"tgt_hash_{id(self)}@t.com", is_admin=0
        )

    def test_db_stores_hash_not_raw_token(self):
        result = db.create_impersonation_session(
            admin_user_id=self.admin_id,
            target_user_id=self.target_id,
            reason="hashing-check",
        )
        raw = result["cookie_token"]
        self.assertTrue(len(raw) > 20)

        row = db.get_impersonation_session_by_token(raw)
        self.assertIsNotNone(row)

        expected_hash = hashlib.sha256(raw.encode()).hexdigest()
        self.assertEqual(row["cookie_token_hash"], expected_hash)
        self.assertNotEqual(row["cookie_token_hash"], raw)

        # cookie_token (legacy column) must not contain the raw value.
        self.assertNotIn(raw, row["cookie_token"] or "")

    def test_lookup_with_wrong_token_returns_none(self):
        db.create_impersonation_session(
            admin_user_id=self.admin_id,
            target_user_id=self.target_id,
            reason="lookup-check",
        )
        self.assertIsNone(
            db.get_impersonation_session_by_token("not-the-real-token")
        )
        self.assertIsNone(db.get_impersonation_session_by_token(""))


class TestMiddlewareCrossCheck(unittest.TestCase):
    """A valid impersonation cookie alone must NOT grant access."""

    def setUp(self):
        from fastapi.testclient import TestClient

        self.admin_id = _mk_user(
            f"adm_mw_{id(self)}@t.com", is_admin=1
        )
        self.target_id = _mk_user(
            f"tgt_mw_{id(self)}@t.com", is_admin=0
        )
        self.other_admin_id = _mk_user(
            f"other_adm_{id(self)}@t.com", is_admin=1
        )

        imp = db.create_impersonation_session(
            admin_user_id=self.admin_id,
            target_user_id=self.target_id,
            reason="middleware-test",
        )
        self.imp_session_id = imp["id"]
        self.imp_cookie_value = imp["cookie_token"]

        self._app = _build_app()
        self._client = TestClient(self._app)

    def test_stolen_imp_cookie_no_admin_session_is_rejected(self):
        """An attacker with only the impersonation cookie → kicked out."""
        r = self._client.get(
            "/ping",
            cookies={"narve_impersonation": self.imp_cookie_value},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/admin/users")

        row = db.get_impersonation_session(self.imp_session_id)
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(row["end_reason"], "admin_session_mismatch")

    def test_stolen_imp_cookie_plus_nonmatching_session_is_rejected(self):
        """Stolen imp cookie + a DIFFERENT admin's session → rejected."""
        imp = db.create_impersonation_session(
            admin_user_id=self.admin_id,
            target_user_id=self.target_id,
            reason="mismatch-test",
        )
        imp_session_id = imp["id"]
        imp_cookie_value = imp["cookie_token"]

        other_session_token = db.create_user_session(self.other_admin_id)

        r = self._client.get(
            "/ping",
            cookies={
                "narve_impersonation": imp_cookie_value,
                "narve_session": other_session_token,
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/admin/users")

        row = db.get_impersonation_session(imp_session_id)
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(row["end_reason"], "admin_session_mismatch")

    def test_legitimate_flow_passes_through(self):
        """Imp cookie + admin's own session → request goes through."""
        imp = db.create_impersonation_session(
            admin_user_id=self.admin_id,
            target_user_id=self.target_id,
            reason="happy-path",
        )
        imp_cookie_value = imp["cookie_token"]
        admin_session_token = db.create_user_session(self.admin_id)

        r = self._client.get(
            "/ping",
            cookies={
                "narve_impersonation": imp_cookie_value,
                "narve_session": admin_session_token,
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIsNotNone(body["imp"])
        self.assertEqual(body["imp"]["admin_user_id"], self.admin_id)
        self.assertEqual(body["imp"]["target_user_id"], self.target_id)


if __name__ == "__main__":
    unittest.main()

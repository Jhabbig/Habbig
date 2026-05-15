"""Regression tests for the admin-auth guard on /admin/api-keys.

Audit finding: ``admin_api_keys_page`` and ``admin_api_keys_revoke`` in
gateway/api_keys_routes.py guarded admin access via
``if hasattr(admin, "status_code"): return admin``. But
``_require_admin_user(request, page=True)`` returns **None** (not a
RedirectResponse) for non-admin users — so the hasattr check let the
flow fall through and ``q_api_keys.list_all_api_keys()`` executed for
any authenticated user, leaking every tenant's API keys.

These tests exercise the route handlers directly (rather than through
TestClient), because the in-memory test-DB has a session-table schema
drift unrelated to this fix that prevents the integration auth path
from working. Calling the async handlers with a stub Request and
monkey-patching ``_require_admin_user`` / ``list_all_api_keys`` lets
us pin the exact guard behaviour the audit demands.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest import mock

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("EMAIL_DRY_RUN", "true")

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations

import api_keys_routes  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402


# ── Test doubles ──────────────────────────────────────────────────────


class _StubRequest:
    """Minimal stand-in for fastapi.Request — the handlers only need
    something they can pass through to ``_require_admin_user`` (which is
    patched) and to ``render_page`` (also patched on the happy path)."""

    def __init__(self):
        self.method = "GET"
        self.url = mock.Mock(path="/admin/api-keys")
        self.cookies = {}
        self.headers = {}
        self.state = mock.Mock()


def _runsafe(coro):
    """Run an async handler in a fresh event loop. The handlers are
    short-lived and we want loop isolation per test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── The audit fix: GET /admin/api-keys ────────────────────────────────


class TestAdminApiKeysPageGuard(unittest.TestCase):
    """``admin_api_keys_page`` must NOT call ``list_all_api_keys`` for any
    caller that ``_require_admin_user(page=True)`` rejects (returns None).
    The bug was: ``hasattr(None, "status_code")`` is False, so the
    function flowed past the guard and leaked every tenant's keys.
    """

    def test_non_admin_returns_redirect_and_does_not_list_keys(self):
        """Simulates the exact attack the audit describes:
        ``_require_admin_user(page=True)`` returns ``None`` for a non-admin
        authed user. The handler MUST short-circuit before reading keys."""
        req = _StubRequest()
        list_spy = mock.Mock()
        with mock.patch.object(
            api_keys_routes, "_require_admin_user", return_value=None,
        ), mock.patch(
            "api_keys_routes.q_api_keys.list_all_api_keys", list_spy,
        ):
            resp = _runsafe(api_keys_routes.admin_api_keys_page(req))

        # Must be a redirect — not a 200 listing.
        self.assertIsInstance(
            resp, RedirectResponse,
            f"Expected RedirectResponse for non-admin caller, got {type(resp).__name__}",
        )
        self.assertEqual(resp.status_code, 302)
        # Cross-tenant key listing MUST NOT have run.
        self.assertFalse(
            list_spy.called,
            "list_all_api_keys was invoked for a non-admin caller — "
            "this is the exact cross-tenant key disclosure the audit "
            "flagged. The hasattr-only guard let None fall through.",
        )

    def test_redirect_response_from_guard_passes_through(self):
        """When the guard returns a RedirectResponse (e.g. 2FA path), the
        handler must return it verbatim and again must not list keys."""
        req = _StubRequest()
        redirect = RedirectResponse("/auth/2fa", status_code=303)
        list_spy = mock.Mock()
        with mock.patch.object(
            api_keys_routes, "_require_admin_user", return_value=redirect,
        ), mock.patch(
            "api_keys_routes.q_api_keys.list_all_api_keys", list_spy,
        ):
            resp = _runsafe(api_keys_routes.admin_api_keys_page(req))
        self.assertIs(resp, redirect)
        self.assertFalse(list_spy.called)

    def test_admin_user_sees_full_listing(self):
        """Happy path: a real admin user dict flows through to
        ``list_all_api_keys`` and the rendered page is returned."""
        req = _StubRequest()
        admin = {
            "user_id": 1, "email": "admin@narve.ai",
            "username": "admin", "is_admin": True,
        }
        # Two fake key rows so we can assert they reach the renderer.
        fake_rows = [
            {
                "id": 11, "key_prefix": "nv_emb_aaaa",
                "name": "alpha", "scopes": "read",
                "allowed_origins": "", "created_at": 1700000000,
                "last_used_at": None, "usage_count": 0,
                "revoked_at": None, "owner_email": "u1@narve.ai",
            },
            {
                "id": 12, "key_prefix": "nv_emb_bbbb",
                "name": "beta", "scopes": "read,write",
                "allowed_origins": "example.com", "created_at": 1700000001,
                "last_used_at": 1700000500, "usage_count": 42,
                "revoked_at": None, "owner_email": "u2@narve.ai",
            },
        ]

        # The rows are accessed with sqlite-Row-style ``"k" in r.keys()``
        # checks; make .keys() return the dict's keys.
        for r in fake_rows:
            r_keys = list(r)  # snapshot before we monkey-patch.
            r["keys"] = lambda _ks=r_keys: _ks

        captured = {}

        def _fake_render(name, request=None, **ctx):
            captured["name"] = name
            captured["ctx"] = ctx
            return mock.Mock(status_code=200, body=ctx.get("raw_key_rows", "").encode())

        with mock.patch.object(
            api_keys_routes, "_require_admin_user", return_value=admin,
        ), mock.patch.object(
            api_keys_routes, "_render", side_effect=_fake_render,
        ), mock.patch(
            "api_keys_routes.q_api_keys.list_all_api_keys", return_value=fake_rows,
        ):
            resp = _runsafe(api_keys_routes.admin_api_keys_page(req))

        self.assertEqual(captured.get("name"), "admin_api_keys")
        self.assertEqual(captured["ctx"]["total"], 2)
        rendered = captured["ctx"]["raw_key_rows"]
        self.assertIn("nv_emb_aaaa", rendered)
        self.assertIn("nv_emb_bbbb", rendered)
        self.assertIn("alpha", rendered)
        self.assertIn("beta", rendered)
        # Owner emails must appear so the admin can audit per-tenant.
        self.assertIn("u1@narve.ai", rendered)
        self.assertIn("u2@narve.ai", rendered)


# ── The audit fix: POST /admin/api-keys/{id}/revoke ────────────────────


class TestAdminApiKeysRevokeGuard(unittest.TestCase):
    """``admin_api_keys_revoke`` is normally page=False (raises 403), but
    we still defend against a future None return so the audit fix is
    symmetric across the two admin routes."""

    def test_non_admin_none_does_not_revoke(self):
        req = _StubRequest()
        req.method = "POST"
        revoke_spy = mock.Mock()
        with mock.patch.object(
            api_keys_routes, "_require_admin_user", return_value=None,
        ), mock.patch(
            "api_keys_routes.q_api_keys.admin_revoke_api_key", revoke_spy,
        ):
            resp = _runsafe(api_keys_routes.admin_api_keys_revoke(req, 99))

        # Must short-circuit. Revoke MUST NOT have run.
        self.assertIsInstance(resp, RedirectResponse)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(
            revoke_spy.called,
            "admin_revoke_api_key was invoked despite a None auth result — "
            "the guard let the request through.",
        )

    def test_redirect_from_guard_passes_through(self):
        req = _StubRequest()
        req.method = "POST"
        redirect = RedirectResponse("/auth/2fa", status_code=303)
        revoke_spy = mock.Mock()
        with mock.patch.object(
            api_keys_routes, "_require_admin_user", return_value=redirect,
        ), mock.patch(
            "api_keys_routes.q_api_keys.admin_revoke_api_key", revoke_spy,
        ):
            resp = _runsafe(api_keys_routes.admin_api_keys_revoke(req, 99))
        self.assertIs(resp, redirect)
        self.assertFalse(revoke_spy.called)

    def test_admin_can_revoke(self):
        req = _StubRequest()
        req.method = "POST"
        admin = {"user_id": 1, "email": "admin@narve.ai", "is_admin": True}
        with mock.patch.object(
            api_keys_routes, "_require_admin_user", return_value=admin,
        ), mock.patch(
            "api_keys_routes.q_api_keys.admin_revoke_api_key", return_value=True,
        ) as revoke_mock:
            resp = _runsafe(api_keys_routes.admin_api_keys_revoke(req, 77))

        # Successful revoke → redirect back to the listing page.
        self.assertIsInstance(resp, RedirectResponse)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], "/admin/api-keys")
        revoke_mock.assert_called_once_with(77)


if __name__ == "__main__":
    unittest.main()

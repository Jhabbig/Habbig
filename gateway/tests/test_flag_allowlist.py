"""Regression tests for the HIGH feature-flag-keyspace audit finding.

Background
----------
The audit flagged two related issues on the feature-flag surface:

  1. ``/admin/flags`` (POST) accepted any key as long as it matched the
     loose ``[a-z0-9_-]{1,80}`` pattern. An admin could persist arbitrary
     keys — including typos that no code reads — and the resulting dead
     rows became indistinguishable from real flags during routine
     admin operations.

  2. ``/api/flags/evaluate/{key}`` returned a 200 JSON document for any
     key that existed in the DB, even when the caller was not an admin.
     That gave non-admins a differential probe: a 200 body for an
     admin-only flag confirmed the flag's existence even when the
     evaluation said ``enabled: false``. The information leak ordered
     external traffic to learn which experimental features the admin
     team was preparing.

The fix is to pin the keyspace to a code-defined registry
(``features.KNOWN_FLAGS``):

  * ``flag_create`` rejects unknown keys with 400 BEFORE any DB write,
    so admins must add the key in code first.
  * ``flag_evaluate_api`` returns 404 (NOT 200/false) for unknown keys
    when the caller is not an admin. Admins still get the normal eval
    path so newly-added registry entries are debuggable before the
    consumer code lands.

This module locks both behaviours in.

Why we hit the handlers directly
--------------------------------
We could in theory drive these tests through TestClient + a real
``/admin/flags`` POST, but the rest of the suite is currently dealing
with a migration-189 sessions-token-hash drift that breaks
``db.create_session`` under the in-memory test DB (the legacy ``token``
column is gone after the migration runs, but ``queries/auth.py`` still
INSERTs into it). Going around the auth layer keeps THIS regression
focused on the two new guards we just added — auth coverage lives in
``test_admin_audit_log.py`` and friends, and will start passing again
once the legacy ``create_session`` is repointed at ``token_hash``.
"""

from __future__ import annotations

import asyncio
import re as _re
import unittest
from unittest.mock import patch

from tests import _testdb  # noqa: F401  — shared in-memory DB

USES_TESTDB = True

from fastapi import HTTPException  # noqa: E402

import admin_routes  # noqa: E402
import db  # noqa: E402
import features  # noqa: E402


# ── Test scaffolding ────────────────────────────────────────────────────


def _run(coro):
    """Run an async coroutine in a one-shot loop. Keeps the failure stack
    readable when a handler raises (vs IsolatedAsyncioTestCase ceremony)."""
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeForm(dict):
    """Mimics the Starlette FormData interface flag_create touches.

    ``get(key)`` is the only method called in the handler; subclass ``dict``
    so default-argument semantics carry across without re-implementing.
    """


class _FakeRequest:
    """A pared-down stand-in for fastapi.Request.

    Implements just the surface the flag handlers touch: ``form()``,
    ``headers``, ``client.host``, ``query_params``. Bypassing the real
    middleware lets us drive the handlers with no session cookie, no CSRF
    pair, and no broken ``db.create_session`` involvement.
    """

    def __init__(self, *, form_data=None, query=None):
        self._form = _FakeForm(form_data or {})
        self.query_params = dict(query or {})
        self.headers = {
            "user-agent": "pytest-flag-allowlist/1.0",
            "x-request-id": "req-flag-allowlist",
            "x-forwarded-for": "127.0.0.1",
        }
        class _C:
            host = "127.0.0.1"
        self.client = _C()
        # The audit layer reads ``request.state.impersonation`` defensively;
        # provide an empty namespace so it doesn't AttributeError.
        class _S:
            impersonation = None
        self.state = _S()

    async def form(self):
        return self._form


# An "admin" dict shaped like ``server.current_user()`` returns. The handler
# only reads ``user_id``; the audit layer reads ``email``. Test-only fixture.
_ADMIN_DICT = {"user_id": 1, "email": "flag_allowlist@test.local", "is_admin": 1}
_NON_ADMIN_DICT = {"user_id": 2, "email": "regular@test.local", "is_admin": 0}


def _patch_admin_user(admin_dict=_ADMIN_DICT):
    """Patch ``admin_routes._require_admin_user`` to return ``admin_dict``
    without exercising the real session machinery."""
    return patch.object(admin_routes, "_require_admin_user", return_value=admin_dict)


def _patch_current_user(user_dict):
    """Patch ``admin_routes._current_user`` so the evaluator sees ``user_dict``."""
    return patch.object(admin_routes, "_current_user", return_value=user_dict)


# ── 1. KNOWN_FLAGS registry sanity ──────────────────────────────────────


class TestKnownFlagsRegistry(unittest.TestCase):
    """The registry is the single source of truth — verify its shape."""

    def test_registry_is_a_set(self):
        self.assertIsInstance(features.KNOWN_FLAGS, set)

    def test_registry_is_non_empty(self):
        self.assertGreater(len(features.KNOWN_FLAGS), 0,
                           "KNOWN_FLAGS must enumerate at least one flag")

    def test_is_known_flag_true_for_each_entry(self):
        for key in features.KNOWN_FLAGS:
            self.assertTrue(features.is_known_flag(key),
                            f"{key!r} must be considered known")

    def test_is_known_flag_false_for_typos(self):
        # Pick a typo that's overwhelmingly unlikely to ever be a real flag.
        self.assertFalse(features.is_known_flag("ths_is_not_a_flag_qwerty"))

    def test_registry_keys_match_format(self):
        """Every registry key must satisfy the same regex flag_create
        accepts — guard against an entry that the create handler would
        immediately reject."""
        pattern = _re.compile(r"[a-z0-9_\-]{1,80}")
        for key in features.KNOWN_FLAGS:
            self.assertTrue(pattern.fullmatch(key),
                            f"KNOWN_FLAGS entry {key!r} fails the create regex")


# ── 2. flag_create rejects unknown keys ─────────────────────────────────


class TestFlagCreateRejectsUnknownKey(unittest.TestCase):
    """flag_create() must 400 on a key NOT in KNOWN_FLAGS, BEFORE any DB write."""

    def _invoke(self, key, *, name="Test Flag"):
        """Drive the async ``flag_create`` handler with a fake form.
        Returns a dict ``{status_code, detail}`` so tests can assert
        on both the HTTP response shape and the underlying behaviour.
        """
        req = _FakeRequest(form_data={
            "key": key, "name": name, "description": "test from regression",
        })
        with _patch_admin_user():
            try:
                _run(admin_routes.flag_create(req))
            except HTTPException as exc:
                return {"status_code": exc.status_code, "detail": exc.detail}
            return {"status_code": 302, "detail": "ok"}  # redirect on success

    def test_unknown_key_returns_400(self):
        key = "totally_unknown_flag_zxc"
        self.assertNotIn(key, features.KNOWN_FLAGS)
        out = self._invoke(key)
        self.assertEqual(out["status_code"], 400,
                         f"Expected 400 for unknown key, got {out['status_code']}: {out['detail']!r}")

    def test_unknown_key_error_mentions_registry(self):
        out = self._invoke("another_typo_flag_qweasd")
        self.assertEqual(out["status_code"], 400)
        detail = str(out["detail"]).lower()
        # The error should signal the fix path — admins should know the
        # rejection comes from the registry, not a generic 400.
        self.assertTrue(
            "known_flags" in detail or "registry" in detail or "unknown" in detail,
            f"Error detail must reference the registry: {out['detail']!r}",
        )

    def test_unknown_key_does_not_persist_a_row(self):
        key = "should_never_be_persisted_flag"
        self.assertNotIn(key, features.KNOWN_FLAGS)
        out = self._invoke(key)
        self.assertEqual(out["status_code"], 400)
        # No row in the DB — the handler rejects BEFORE the create call.
        self.assertIsNone(db.get_feature_flag(key),
                          "Rejected create must not leave a stray DB row")

    def test_known_key_is_accepted(self):
        """Positive control — picking a real registry entry succeeds (or
        409s if a previous test happened to create it). The point is we
        DON'T get the 400 from the registry guard."""
        known_key = "love_beta"
        self.assertIn(known_key, features.KNOWN_FLAGS)
        # Wipe any pre-existing row so we test the create path, not the
        # duplicate-409 path. Use subproduct_key=None (global row only).
        try:
            db.delete_feature_flag(known_key, subproduct_key=None)
        except Exception:
            pass
        out = self._invoke(known_key)
        # 302 (redirect-on-success) or 409 (duplicate). Crucially NOT 400.
        self.assertNotEqual(out["status_code"], 400,
                            f"Known key {known_key!r} must not be rejected: {out['detail']!r}")
        self.assertIn(out["status_code"], (302, 409),
                      f"Unexpected status for known key: {out['status_code']}: {out['detail']!r}")


# ── 3. flag_evaluate_api returns 404 for unknown keys (non-admin) ───────


class TestFlagEvaluateHidesUnknownKeys(unittest.TestCase):
    """The public evaluator must not leak admin-only flag existence."""

    def _invoke(self, key, *, user):
        """Drive ``flag_evaluate_api`` and capture the resulting HTTPException
        (if any) or the JSONResponse body."""
        req = _FakeRequest(query={})
        with _patch_current_user(user):
            try:
                resp = _run(admin_routes.flag_evaluate_api(req, key))
            except HTTPException as exc:
                return {"status_code": exc.status_code, "detail": exc.detail, "body": None}
            import json
            return {
                "status_code": resp.status_code,
                "detail": "",
                "body": json.loads(resp.body.decode()),
            }

    def test_unknown_key_returns_404_for_non_admin(self):
        """A non-admin probing for an unknown flag key must see 404 — not
        a 200 with ``enabled: false`` that confirms the route works."""
        out = self._invoke("never_a_real_flag_xyz", user=_NON_ADMIN_DICT)
        self.assertEqual(out["status_code"], 404,
                         f"Non-admin probe of unknown flag must 404, got {out['status_code']}")

    def test_unknown_key_anonymous_is_401(self):
        """Anonymous callers should never reach the registry check —
        they're rejected at the auth layer first (C5 in the audit)."""
        out = self._invoke("never_a_real_flag_xyz", user=None)
        self.assertEqual(out["status_code"], 401)

    def test_known_key_returns_200_for_non_admin(self):
        """Positive control — a key that IS in the registry returns the
        normal 200 evaluator response for non-admins. This confirms the
        404 in the previous test is the registry guard, not a blanket
        non-admin block."""
        known_key = next(iter(features.KNOWN_FLAGS))
        out = self._invoke(known_key, user=_NON_ADMIN_DICT)
        self.assertEqual(out["status_code"], 200,
                         f"Known flag {known_key!r} must evaluate for non-admin")
        body = out["body"]
        self.assertEqual(body["key"], known_key)
        self.assertIn("enabled", body)
        self.assertIsInstance(body["enabled"], bool)

    def test_admin_can_evaluate_unknown_keys(self):
        """Admins keep the unrestricted path so newly-added registry
        entries are debuggable BEFORE the consumer code wires them up.
        Intentional asymmetry: admins see ground truth, the public surface
        obeys the registry."""
        out = self._invoke("admin_debug_unknown_flag_pq", user=_ADMIN_DICT)
        self.assertEqual(out["status_code"], 200,
                         f"Admin must bypass the registry guard, got {out['status_code']}: {out['detail']!r}")
        body = out["body"]
        self.assertEqual(body["enabled"], False,
                         "Missing DB row → fail-closed evaluation (False)")


if __name__ == "__main__":
    unittest.main()

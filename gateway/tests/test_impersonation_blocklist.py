"""Blocklist tests for the impersonation TTL audit follow-up.

The audit identified one CRIT (``/profile/password`` reachable under
impersonation = full account takeover) plus 5 unblocked write surfaces
(HIGH x 3) and 5 MED items where an admin viewing as another user could
trigger irreversible actions under the target's identity. This file
pins the new blocklist entries so a future refactor cannot silently
drop one.

Coverage is two-layer:

  1. Pure ``is_action_blocked`` matching — fast, no DB. Each audit
     route is checked for the documented method/path.
  2. End-to-end through ``ImpersonationMiddleware`` — every blocked
     path returns HTTP 403 when called with a valid impersonation
     cookie + the admin's own ``narve_session`` cookie. Mirrors the
     setup in ``test_impersonation_middleware.py``.
"""

from __future__ import annotations

import unittest

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations

import db
import impersonation as imp


def _mk_user(email: str, is_admin: int = 0) -> int:
    return db.create_user(
        email, "pw-" * 4, username=email.split("@")[0], is_admin=is_admin
    )


# Audit-identified routes the new blocklist must cover. Tuples are
# ``(method, path, severity)`` where ``path`` is a realistic concrete
# request — the middleware uses re.search against the bare path, so
# query strings are irrelevant to matching.
_AUDIT_ROUTES = [
    # CRIT — admin can change the target user's password.
    ("POST", "/profile/password", "CRIT"),
    # HIGH — disconnects market positions + credentials.
    ("POST", "/settings/disconnect/polymarket", "HIGH"),
    ("POST", "/settings/disconnect/kalshi", "HIGH"),
    # HIGH — create / delete / rotate widget embed tokens.
    ("POST", "/api/embeds", "HIGH"),
    ("DELETE", "/api/embeds/42", "HIGH"),
    ("POST", "/api/embeds/42/rotate-token", "HIGH"),
    # HIGH — start a subproduct signup under the user identity.
    ("POST", "/subproduct-signup", "HIGH"),
    # MED — toggles user trading-addon integration.
    ("PATCH", "/api/trading-addon/config", "MED"),
    # MED — share write endpoints.
    ("POST", "/api/share/market", "MED"),
    ("POST", "/api/share/source", "MED"),
    ("POST", "/api/share/prediction", "MED"),
    # MED — saved-prediction list mutations.
    ("POST", "/api/saved/123", "MED"),
    ("DELETE", "/api/saved/123", "MED"),
    ("PATCH", "/api/saved/123", "MED"),
    # MED — follow / unfollow a source under the user identity.
    ("POST", "/api/sources/elonmusk/follow", "MED"),
    ("DELETE", "/api/sources/elonmusk/follow", "MED"),
    # MED — email-preferences mutation.
    ("POST", "/api/notifications/email-preferences", "MED"),
    # MED — feedback submission / vote / comment.
    ("POST", "/api/feedback", "MED"),
    ("POST", "/api/feedback/7/vote", "MED"),
    ("POST", "/api/feedback/7/comment", "MED"),
]


# ── Layer 1: pure pattern matching ────────────────────────────────────────


class TestPatternMatching(unittest.TestCase):
    """``is_action_blocked`` returns True for every audited surface."""

    def test_every_audit_route_is_blocked(self):
        for method, path, severity in _AUDIT_ROUTES:
            with self.subTest(method=method, path=path, severity=severity):
                self.assertTrue(
                    imp.is_action_blocked(method, path),
                    f"{severity} route {method} {path} must be blocked",
                )

    def test_profile_password_prefix_catches_subpaths(self):
        # Prefix-style pattern is the point — sibling routes should
        # match without enumerating every suffix.
        self.assertTrue(imp.is_action_blocked("POST", "/profile/password/change"))
        self.assertTrue(imp.is_action_blocked("POST", "/profile/password-reset"))

    def test_get_on_api_embeds_blocked_read_leak(self):
        # GET /api/embeds returns include_token=True payloads — a read
        # alone leaks credentials. ``_READ_ALSO_BLOCKED_RE`` must cover it.
        self.assertTrue(imp.is_action_blocked("GET", "/api/embeds"))
        self.assertTrue(imp.is_action_blocked("GET", "/api/embeds/42"))

    def test_existing_safe_routes_still_pass(self):
        # Sanity: prior allow-list invariants must not regress.
        self.assertFalse(imp.is_action_blocked("GET", "/settings"))
        # /api/feedback is blocked, bare /feedback is not.
        self.assertFalse(imp.is_action_blocked("POST", "/feedback"))
        self.assertFalse(imp.is_action_blocked("POST", "/dashboards"))
        self.assertFalse(imp.is_action_blocked("POST", "/admin/impersonations/end"))


# ── Layer 2: end-to-end through ImpersonationMiddleware ───────────────────


def _build_app():
    """FastAPI app wrapping ONLY ImpersonationMiddleware with a
    catch-all route so any non-blocked path returns 200 OK and any
    blocked path returns the middleware's 403 HTMLResponse.

    The catch-all is required: FastAPI's routing still runs even when
    the middleware short-circuits, so the route must exist for every
    method we'll exercise — otherwise we'd get a 405/404 from
    routing on the non-blocked sanity cases.
    """
    from fastapi import FastAPI
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from server import ImpersonationMiddleware

    async def _ok(request: Request):
        return JSONResponse({"ok": True, "path": request.url.path})

    routes = [
        Route(
            "/{full_path:path}",
            _ok,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        ),
    ]
    app = FastAPI(routes=routes)
    app.add_middleware(ImpersonationMiddleware)
    return app


class TestMiddlewareReturns403(unittest.TestCase):
    """Every blocked path returns HTTP 403 through the real middleware."""

    def setUp(self):
        # Per-method setUp (not setUpClass) so each test has a fresh
        # impersonation session row. The middleware ends the session
        # on certain cookie-validation paths, and reusing it across
        # tests is brittle — mirrors the approach in
        # ``test_impersonation_middleware.py``.
        from fastapi.testclient import TestClient

        self.admin_id = _mk_user(
            f"adm_blk_{id(self)}@t.com", is_admin=1
        )
        self.target_id = _mk_user(
            f"tgt_blk_{id(self)}@t.com", is_admin=0
        )

        imp_row = db.create_impersonation_session(
            admin_user_id=self.admin_id,
            target_user_id=self.target_id,
            reason="blocklist-test",
        )
        self.imp_session_id = imp_row["id"]
        self.imp_cookie = imp_row["cookie_token"]
        self.admin_session = db.create_user_session(self.admin_id)

        self.client = TestClient(_build_app())
        # Set cookies on the client (not per-request) — per-request
        # cookies are deprecated in httpx ≥ 0.27 and warn loudly.
        self.client.cookies.set("narve_impersonation", self.imp_cookie)
        self.client.cookies.set("narve_session", self.admin_session)

    def _request(self, method: str, path: str):
        return self.client.request(method, path, follow_redirects=False)

    def test_every_audit_route_returns_403(self):
        for method, path, severity in _AUDIT_ROUTES:
            with self.subTest(method=method, path=path, severity=severity):
                r = self._request(method, path)
                self.assertEqual(
                    r.status_code, 403,
                    f"{severity} {method} {path} → expected 403, got "
                    f"{r.status_code}: {r.text[:200]}",
                )
                self.assertIn("Action blocked", r.text)

    def test_get_profile_password_passes_through(self):
        # GET on /profile/password is NOT in ``_READ_ALSO_BLOCKED`` —
        # the admin can still view the password-change form rendered
        # for the target user. Only the POST destruction is blocked.
        r = self._request("GET", "/profile/password")
        self.assertEqual(r.status_code, 200)

    def test_get_api_embeds_returns_403_token_leak(self):
        # GET /api/embeds returns widget tokens (include_token=True)
        # — pinned in ``_READ_ALSO_BLOCKED_RE`` so the read itself is
        # treated as a credential leak.
        r = self._request("GET", "/api/embeds")
        self.assertEqual(r.status_code, 403)
        self.assertIn("Action blocked", r.text)

    def test_blocked_action_recorded_in_audit_log(self):
        # The CRIT case — /profile/password — must appear in
        # ``impersonation_actions`` with was_blocked=1 so the admin
        # audit log can prove the attempt.
        before = len(db.list_impersonation_actions(self.imp_session_id))
        r = self._request("POST", "/profile/password")
        self.assertEqual(r.status_code, 403)
        actions = db.list_impersonation_actions(self.imp_session_id)
        self.assertEqual(len(actions), before + 1)
        latest = actions[-1]
        self.assertEqual(latest["method"], "POST")
        self.assertEqual(latest["path"], "/profile/password")
        self.assertEqual(latest["status_code"], 403)
        self.assertEqual(latest["was_blocked"], 1)


class TestNoRegressionOnExistingBlocklist(unittest.TestCase):
    """Spot-checks: existing blocklist entries continue to fire after
    the new additions. Defends against accidental list-edit fat-finger."""

    def test_existing_critical_still_blocked(self):
        for method, path in [
            ("POST", "/account/password"),
            ("POST", "/account/delete"),
            ("POST", "/billing/cancel"),
            ("POST", "/subscribe"),
            ("POST", "/api/ai/complete"),
            ("DELETE", "/api/widgets/9"),
        ]:
            with self.subTest(method=method, path=path):
                self.assertTrue(imp.is_action_blocked(method, path))


if __name__ == "__main__":
    unittest.main()

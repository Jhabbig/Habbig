"""Tests for CSRF protection — token generation, validation, rotation, exemptions."""

from __future__ import annotations

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import importlib

from security.csrf import (
    CSRF_TOKEN_LENGTH,
    CSRF_ROTATION_SECONDS,
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CSRF_FORM_FIELD,
    generate_csrf_token,
    validate_csrf_token,
    csrf_hidden_field,
)
import security.csrf as csrf_module


class TestCSRFTokenGeneration(unittest.TestCase):
    def test_token_length(self):
        """Generated tokens should be 43 chars (32 bytes URL-safe base64)."""
        token = generate_csrf_token()
        # secrets.token_urlsafe(32) -> 43 chars
        self.assertEqual(len(token), 43)

    def test_tokens_are_unique(self):
        """Each call should produce a different token."""
        tokens = {generate_csrf_token() for _ in range(100)}
        self.assertEqual(len(tokens), 100)

    def test_token_is_urlsafe(self):
        """Token should only contain URL-safe base64 characters."""
        token = generate_csrf_token()
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        self.assertTrue(all(c in allowed for c in token))


class TestCSRFValidation(unittest.TestCase):
    def test_missing_token_is_invalid(self):
        valid, reason = validate_csrf_token(cookie_token="abc", submitted_token=None)
        self.assertFalse(valid)
        self.assertEqual(reason, "missing")

    def test_no_reference_is_invalid(self):
        valid, reason = validate_csrf_token(cookie_token=None, submitted_token="abc")
        self.assertFalse(valid)
        self.assertEqual(reason, "no_reference")

    def test_matching_cookie_is_valid(self):
        token = "same_token_value"
        valid, reason = validate_csrf_token(cookie_token=token, submitted_token=token)
        self.assertTrue(valid)
        self.assertEqual(reason, "")

    def test_mismatched_cookie_is_invalid(self):
        valid, reason = validate_csrf_token(cookie_token="abc", submitted_token="xyz")
        self.assertFalse(valid)
        self.assertEqual(reason, "mismatch")

    def test_session_token_preferred_over_cookie(self):
        """Session token should be used if available."""
        valid, _ = validate_csrf_token(
            cookie_token="cookie_val",
            submitted_token="session_val",
            session_token="session_val",
        )
        self.assertTrue(valid)

    def test_session_token_mismatch_invalid(self):
        valid, reason = validate_csrf_token(
            cookie_token="cookie_val",
            submitted_token="cookie_val",
            session_token="session_val",  # this takes precedence
        )
        self.assertFalse(valid)
        self.assertEqual(reason, "mismatch")

    def test_expired_session_token_invalid(self):
        """Tokens older than CSRF_ROTATION_SECONDS should be rejected."""
        old_timestamp = int(time.time()) - (CSRF_ROTATION_SECONDS + 100)
        valid, reason = validate_csrf_token(
            cookie_token="t",
            submitted_token="t",
            session_token="t",
            session_csrf_created_at=old_timestamp,
        )
        self.assertFalse(valid)
        self.assertEqual(reason, "expired")

    def test_fresh_session_token_valid(self):
        """Tokens younger than CSRF_ROTATION_SECONDS should be accepted."""
        fresh_timestamp = int(time.time()) - 60
        valid, _ = validate_csrf_token(
            cookie_token="t",
            submitted_token="t",
            session_token="t",
            session_csrf_created_at=fresh_timestamp,
        )
        self.assertTrue(valid)


class TestCSRFHiddenField(unittest.TestCase):
    def test_hidden_field_contains_token(self):
        token = "sample_token_abc123"
        field = csrf_hidden_field(token)
        self.assertIn(token, field)
        self.assertIn('type="hidden"', field)
        self.assertIn(f'name="{CSRF_FORM_FIELD}"', field)

    def test_hidden_field_escapes_html(self):
        token = '<script>"&alert'
        field = csrf_hidden_field(token)
        # The token should be HTML-escaped
        self.assertNotIn("<script>", field)
        self.assertIn("&lt;", field)


class TestCSRFConstants(unittest.TestCase):
    def test_cookie_name(self):
        self.assertEqual(CSRF_COOKIE_NAME, "_csrf")

    def test_header_name(self):
        self.assertEqual(CSRF_HEADER_NAME, "x-csrf-token")

    def test_form_field(self):
        self.assertEqual(CSRF_FORM_FIELD, "_csrf")

    def test_rotation_seconds(self):
        self.assertEqual(CSRF_ROTATION_SECONDS, 7200)  # 2 hours


class TestCSRFMiddlewareExemptions(unittest.TestCase):
    """Test that exempt paths bypass CSRF validation."""

    def test_stripe_webhook_is_exempt(self):
        from security.csrf import _CSRF_EXEMPT_PATHS
        self.assertIn("/stripe/webhook", _CSRF_EXEMPT_PATHS)

    def test_scraper_ingest_is_exempt(self):
        # Only the specific scraper push endpoint is exempt — the broad
        # "/api/scraper/" prefix was removed (audit MED #3, narrow allowlist).
        from security.csrf import _CSRF_EXEMPT_PATHS
        self.assertIn("/api/scraper/ingest", _CSRF_EXEMPT_PATHS)

    def test_no_prefix_exemptions(self):
        # Prefix-style exemptions are intentionally empty; any future
        # exemption must be an exact path in _CSRF_EXEMPT_PATHS.
        from security.csrf import _CSRF_EXEMPT_PREFIXES
        self.assertEqual(_CSRF_EXEMPT_PREFIXES, ())

    def test_scraper_subpath_is_not_exempt(self):
        # Regression guard for the old broad prefix. An arbitrary
        # "/api/scraper/<whatever>" must NOT slip through.
        from security.csrf import _CSRF_EXEMPT_PATHS, _CSRF_EXEMPT_PREFIXES
        self.assertNotIn("/api/scraper/anything-else", _CSRF_EXEMPT_PATHS)
        self.assertFalse(any(
            "/api/scraper/anything-else".startswith(p)
            for p in _CSRF_EXEMPT_PREFIXES
        ))

    def test_health_is_exempt(self):
        from security.csrf import _CSRF_EXEMPT_PATHS
        self.assertIn("/health", _CSRF_EXEMPT_PATHS)


class TestCSRFPatchDeleteEnforceFlag(unittest.TestCase):
    """
    Phase-1 rollout flag for PATCH/PUT/DELETE.

    When CSRF_PATCH_DELETE_ENFORCE is false (default), the middleware logs
    a warning on a missing/invalid PATCH/PUT/DELETE token but lets the
    request through. When true, it behaves identically to POST (hard 403).
    POST enforcement is unaffected by the flag.
    """

    def _reload_with_flag(self, value):
        """Reload the security.csrf module with the env flag set."""
        with patch.dict(os.environ, {"CSRF_PATCH_DELETE_ENFORCE": value}):
            return importlib.reload(csrf_module)

    def tearDown(self):
        # Reset to the secure default (env var absent) so other test
        # classes see the production behaviour rather than the legacy
        # opt-out mode.
        env_without_flag = {k: v for k, v in os.environ.items()
                            if k != "CSRF_PATCH_DELETE_ENFORCE"}
        with patch.dict(os.environ, env_without_flag, clear=True):
            importlib.reload(csrf_module)

    def test_flag_defaults_to_true(self):
        """Audit HIGH FIX A: absent env var means hard-enforce mode
        (secure default). PATCH/PUT/DELETE behave like POST."""
        # Wipe the var if present so the reload sees no value at all.
        env_without_flag = {k: v for k, v in os.environ.items()
                            if k != "CSRF_PATCH_DELETE_ENFORCE"}
        with patch.dict(os.environ, env_without_flag, clear=True):
            mod = importlib.reload(csrf_module)
            self.assertTrue(mod.CSRF_PATCH_DELETE_ENFORCE)

    def test_flag_true_parses_truthy_values(self):
        for v in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(value=v):
                mod = self._reload_with_flag(v)
                self.assertTrue(mod.CSRF_PATCH_DELETE_ENFORCE)

    def test_flag_false_parses_falsy_values(self):
        for v in ("0", "false", "FALSE", "no", "off", ""):
            with self.subTest(value=v):
                mod = self._reload_with_flag(v)
                self.assertFalse(mod.CSRF_PATCH_DELETE_ENFORCE)


class TestCSRFMiddlewareMethodDispatch(unittest.TestCase):
    """
    Verify the dispatch enforces POST always, gates PATCH/PUT/DELETE on the
    rollout flag, and ignores safe verbs (GET/HEAD/OPTIONS).

    Uses a minimal fake request — we exercise the dispatch coroutine
    directly rather than spinning up Starlette so the test is hermetic.
    """

    def _make_request(self, method, *, token_in_header=None, cookie_token=None):
        req = MagicMock()
        req.method = method
        req.url.path = "/api/sample"
        req.headers = {"content-type": "application/json", "host": "localhost"}
        if token_in_header:
            req.headers[CSRF_HEADER_NAME] = token_in_header
        req.cookies = {CSRF_COOKIE_NAME: cookie_token} if cookie_token else {}
        req.client.host = "127.0.0.1"
        # Used by JSONResponse path; no state needed otherwise
        req.state = MagicMock()
        return req

    async def _dispatch(self, mw, request, expected_status_passthrough=200):
        """Run dispatch and return (status_code, was_passthrough)."""
        passthrough = {"called": False}

        async def call_next(r):
            passthrough["called"] = True
            resp = MagicMock()
            resp.status_code = expected_status_passthrough
            resp.headers = {"content-type": "application/json"}
            return resp

        result = await mw.dispatch(request, call_next)
        status = getattr(result, "status_code", None)
        return status, passthrough["called"]

    def _build_middleware(self, mod):
        # Mock log_csrf_failure to avoid touching the DB during tests.
        sys.modules.setdefault(
            "security.logger",
            MagicMock(log_csrf_failure=lambda *a, **kw: None),
        )
        return mod.CSRFMiddleware(app=MagicMock(), is_production=False)

    def test_post_without_token_returns_403_regardless_of_flag(self):
        import asyncio
        with patch.dict(os.environ, {"CSRF_PATCH_DELETE_ENFORCE": "false"}):
            mod = importlib.reload(csrf_module)
            mw = self._build_middleware(mod)
            req = self._make_request("POST")
            status, passed = asyncio.run(self._dispatch(mw, req))
            self.assertEqual(status, 403)
            self.assertFalse(passed)

    def test_patch_without_token_soft_warns_when_flag_false(self):
        """PATCH without a token gets through and only logs a warning."""
        import asyncio
        with patch.dict(os.environ, {"CSRF_PATCH_DELETE_ENFORCE": "false"}):
            mod = importlib.reload(csrf_module)
            mw = self._build_middleware(mod)
            req = self._make_request("PATCH")
            with patch.object(mod.log, "warning") as warn:
                _, passed = asyncio.run(self._dispatch(mw, req))
                self.assertTrue(passed, "PATCH should pass through in soft-warn mode")
                # At least one warning should mention soft-warn
                msgs = [c.args[0] for c in warn.call_args_list]
                self.assertTrue(
                    any("soft-warn" in m for m in msgs),
                    f"Expected soft-warn log, got: {msgs}",
                )

    def test_delete_without_token_soft_warns_when_flag_false(self):
        import asyncio
        with patch.dict(os.environ, {"CSRF_PATCH_DELETE_ENFORCE": "false"}):
            mod = importlib.reload(csrf_module)
            mw = self._build_middleware(mod)
            req = self._make_request("DELETE")
            with patch.object(mod.log, "warning"):
                _, passed = asyncio.run(self._dispatch(mw, req))
                self.assertTrue(passed, "DELETE should pass through in soft-warn mode")

    def test_patch_without_token_returns_403_when_flag_true(self):
        import asyncio
        with patch.dict(os.environ, {"CSRF_PATCH_DELETE_ENFORCE": "true"}):
            mod = importlib.reload(csrf_module)
            mw = self._build_middleware(mod)
            req = self._make_request("PATCH")
            status, passed = asyncio.run(self._dispatch(mw, req))
            self.assertEqual(status, 403)
            self.assertFalse(passed)

    def test_delete_without_token_returns_403_when_flag_true(self):
        import asyncio
        with patch.dict(os.environ, {"CSRF_PATCH_DELETE_ENFORCE": "true"}):
            mod = importlib.reload(csrf_module)
            mw = self._build_middleware(mod)
            req = self._make_request("DELETE")
            status, passed = asyncio.run(self._dispatch(mw, req))
            self.assertEqual(status, 403)
            self.assertFalse(passed)

    def test_patch_with_valid_token_passes_when_flag_true(self):
        """Happy path: valid PATCH gets through even in strict mode."""
        import asyncio
        with patch.dict(os.environ, {"CSRF_PATCH_DELETE_ENFORCE": "true"}):
            mod = importlib.reload(csrf_module)
            mw = self._build_middleware(mod)
            tok = "matching_token_value"
            req = self._make_request("PATCH", token_in_header=tok, cookie_token=tok)
            status, passed = asyncio.run(self._dispatch(mw, req))
            self.assertTrue(passed)
            self.assertNotEqual(status, 403)

    # ── Audit HIGH FIX A: secure-by-default + generic error header ──
    #
    # The two tests below pin the post-fix contract:
    #
    #   1. PATCH /api/X without a CSRF token → 403 by default (no env
    #      override). DELETE with a wrong token → 403 + the GENERIC
    #      ``X-CSRF-Error: invalid`` header (never the per-reason
    #      "missing" / "mismatch" / "expired" / "origin" value, which
    #      is a phishing/recon side-channel).
    #   2. Explicit ``CSRF_PATCH_DELETE_ENFORCE=false`` opens the
    #      legacy soft-warn escape hatch (emergency rollback only).

    def test_patch_without_token_returns_403_by_default(self):
        """PATCH must enforce CSRF by default (no env override)."""
        import asyncio
        env_without_flag = {k: v for k, v in os.environ.items()
                            if k != "CSRF_PATCH_DELETE_ENFORCE"}
        with patch.dict(os.environ, env_without_flag, clear=True):
            mod = importlib.reload(csrf_module)
            mw = self._build_middleware(mod)
            req = self._make_request("PATCH")
            status, passed = asyncio.run(self._dispatch(mw, req))
            self.assertEqual(status, 403)
            self.assertFalse(passed)

    def test_csrf_error_header_is_generic_not_leaky(self):
        """A failed DELETE returns 403 + ``X-CSRF-Error: invalid``.

        The header MUST NOT reveal the precise validation step that
        failed (missing / no_reference / mismatch / expired / origin)
        — those leak phishing-relevant state to the client.
        """
        import asyncio
        env_without_flag = {k: v for k, v in os.environ.items()
                            if k != "CSRF_PATCH_DELETE_ENFORCE"}
        with patch.dict(os.environ, env_without_flag, clear=True):
            mod = importlib.reload(csrf_module)
            mw = self._build_middleware(mod)
            req = self._make_request(
                "DELETE",
                token_in_header="wrong_value",
                cookie_token="cookie_value",
            )

            async def _grab_response(mw, request):
                async def call_next(r):
                    raise AssertionError("should not reach handler on 403")
                return await mw.dispatch(request, call_next)

            response = asyncio.run(_grab_response(mw, req))
            self.assertEqual(response.headers.get("X-CSRF-Error"), "invalid")
            for leaky in ("missing", "no_reference", "mismatch", "expired", "origin"):
                self.assertNotEqual(
                    response.headers.get("X-CSRF-Error"),
                    leaky,
                    f"X-CSRF-Error must not leak '{leaky}' to the client",
                )

    def tearDown(self):
        # Reset to secure default (env var absent) for other tests.
        env_without_flag = {k: v for k, v in os.environ.items()
                            if k != "CSRF_PATCH_DELETE_ENFORCE"}
        with patch.dict(os.environ, env_without_flag, clear=True):
            importlib.reload(csrf_module)


if __name__ == "__main__":
    unittest.main()

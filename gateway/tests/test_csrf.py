"""Tests for CSRF protection — token generation, validation, rotation, exemptions."""

from __future__ import annotations

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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


if __name__ == "__main__":
    unittest.main()

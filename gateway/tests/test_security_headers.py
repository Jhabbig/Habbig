"""Tests for security headers and security logger."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestSecurityHeaders(unittest.TestCase):
    """Test that security headers are applied via the server's middleware."""

    def test_headers_constant_exists(self):
        import server
        self.assertIn("X-Content-Type-Options", server.SECURITY_HEADERS)
        self.assertEqual(server.SECURITY_HEADERS["X-Content-Type-Options"], "nosniff")

    def test_xframe_options_deny(self):
        import server
        self.assertEqual(server.SECURITY_HEADERS["X-Frame-Options"], "DENY")

    def test_xss_protection(self):
        # Modern OWASP guidance is X-XSS-Protection: 0 (the legacy XSS auditor
        # the older "1; mode=block" value enabled has its own universal-XSS
        # bugs and is disabled in current Chromium / Safari anyway).
        import server
        self.assertEqual(server.SECURITY_HEADERS["X-XSS-Protection"], "0")

    def test_referrer_policy(self):
        import server
        self.assertIn("Referrer-Policy", server.SECURITY_HEADERS)

    def test_permissions_policy(self):
        import server
        self.assertIn("Permissions-Policy", server.SECURITY_HEADERS)

    def test_csp_contains_stripe(self):
        """CSP must allow Stripe JS for checkout integration."""
        import server
        self.assertIn("js.stripe.com", server.CSP)

    def test_csp_frame_ancestors_none(self):
        """CSP should prevent the site from being framed."""
        import server
        self.assertIn("frame-ancestors 'none'", server.CSP)

    def test_csp_no_unsafe_eval(self):
        """CSP should not allow unsafe-eval."""
        import server
        self.assertNotIn("unsafe-eval", server.CSP)


class TestSecurityLogger(unittest.TestCase):
    """Test the security logger module."""

    def test_log_csrf_failure(self):
        from security.logger import log_csrf_failure, security_logger

        # Use a temporary log handler to capture output
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            log_path = f.name

        handler = logging.FileHandler(log_path)
        handler.setLevel(logging.WARNING)
        security_logger.addHandler(handler)
        security_logger.setLevel(logging.WARNING)

        try:
            mock_request = MagicMock()
            mock_request.url.path = "/test"
            mock_request.method = "POST"
            mock_request.headers = {"cf-connecting-ip": "1.2.3.4"}
            mock_request.client.host = "10.0.0.1"

            log_csrf_failure(mock_request, reason="mismatch", user_id=42)

            handler.flush()
            with open(log_path) as f:
                content = f.read()

            self.assertIn("csrf_failure", content)
            self.assertIn("mismatch", content)
            self.assertIn("1.2.3.4", content)
        finally:
            security_logger.removeHandler(handler)
            handler.close()
            os.unlink(log_path)

    def test_log_rate_limit_hit(self):
        from security.logger import log_rate_limit_hit, security_logger

        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            log_path = f.name

        handler = logging.FileHandler(log_path)
        handler.setLevel(logging.WARNING)
        security_logger.addHandler(handler)
        security_logger.setLevel(logging.WARNING)

        try:
            log_rate_limit_hit(
                key="1.2.3.4",
                endpoint="/login",
                ip="1.2.3.4",
                user_id=None,
            )

            handler.flush()
            with open(log_path) as f:
                content = f.read()

            # Parse as JSON
            lines = [l for l in content.strip().split("\n") if l]
            parsed = [json.loads(l) for l in lines]
            self.assertTrue(any(e.get("event") == "rate_limit_hit" for e in parsed))
        finally:
            security_logger.removeHandler(handler)
            handler.close()
            os.unlink(log_path)

    def test_log_suspicious_activity(self):
        from security.logger import log_suspicious_activity

        mock_request = MagicMock()
        mock_request.url.path = "/test"
        mock_request.method = "POST"
        mock_request.headers = {"user-agent": "test-agent"}
        mock_request.client.host = "10.0.0.1"

        # Should not raise
        log_suspicious_activity(mock_request, reason="test_reason", user_id=99)

    def test_configure_security_logging_creates_logs_dir(self):
        from security.logger import configure_security_logging

        with tempfile.TemporaryDirectory() as tmpdir:
            configure_security_logging(Path(tmpdir))
            log_dir = Path(tmpdir) / "logs"
            self.assertTrue(log_dir.exists())


class TestSecurityHeadersOnRedirect(unittest.TestCase):
    """Audit HIGH FIX C — empty-body 302 redirects must carry CSP / XFO.

    A bare ``RedirectResponse(..., 302)`` from Starlette serialises with
    ``Location`` + ``Content-Length: 0`` and nothing else. Without the
    middleware applying the full ``SECURITY_HEADERS`` map to it,
    attackers could frame the redirect, downgrade an HSTS-pinned hop,
    or load mixed content via missing CSP. This pins the contract:
    every 302 emitted by the app exits the middleware with the same
    headers as any other response.
    """

    def test_302_redirect_carries_csp_xfo_xcto(self):
        """Direct unit test against the middleware — hermetic, no FastAPI."""
        # Spin up a minimal Starlette app and route a 302 through the
        # extracted middleware in isolation.
        from starlette.applications import Starlette
        from starlette.responses import RedirectResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient
        import server as _server

        async def redir(_request):
            return RedirectResponse("/elsewhere", status_code=302)

        app = Starlette(routes=[Route("/r", redir)])
        app.add_middleware(_server.SecurityHeadersMiddleware)
        client = TestClient(app, follow_redirects=False)
        try:
            r = client.get("/r")
            self.assertEqual(r.status_code, 302)
            # Empty-body redirect — Location is the only handler header.
            self.assertEqual(r.headers.get("location"), "/elsewhere")
            # The fix: middleware must have stamped each of these.
            self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")
            self.assertEqual(r.headers.get("x-frame-options"), "DENY")
            csp = r.headers.get("content-security-policy")
            self.assertIsNotNone(csp, "CSP missing on empty-body 302")
            self.assertIn("frame-ancestors 'none'", csp or "")
            self.assertIn("default-src 'self'", csp or "")
            self.assertEqual(
                r.headers.get("referrer-policy"),
                "strict-origin-when-cross-origin",
            )
        finally:
            client.close()


class TestSecurityIntegration(unittest.TestCase):
    """Integration-level sanity checks."""

    def test_security_module_imports(self):
        """All security submodules should be importable."""
        from security import csrf, rate_limiter, logger
        self.assertTrue(hasattr(csrf, "CSRFMiddleware"))
        self.assertTrue(hasattr(rate_limiter, "rate_limit"))
        self.assertTrue(hasattr(logger, "log_csrf_failure"))

    def test_server_imports_security_modules(self):
        """Server should wire in the centralised logging + CSRF module.

        The legacy `configure_security_logging` shim was retired when the
        unified `logging_config.configure_logging` rolled out — assert the
        new pipeline is in place instead.
        """
        import server
        # Centralised structured-JSON logging configured via logging_config.
        self.assertTrue(hasattr(server, "configure_logging"))
        # CSRF middleware class still exposed for tests / inspection.
        self.assertTrue(hasattr(server, "CSRFMiddleware"))

    def test_csrf_js_file_exists(self):
        """The frontend CSRF script must exist for the template injection."""
        gateway_dir = Path(__file__).parent.parent
        csrf_js = gateway_dir / "static" / "csrf.js"
        self.assertTrue(csrf_js.exists())
        content = csrf_js.read_text()
        self.assertIn("X-CSRF-Token", content)
        self.assertIn("htmx:configRequest", content)


if __name__ == "__main__":
    unittest.main()

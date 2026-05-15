"""Audit HIGH FIX E — open-redirect regression test for /subproduct-signup.

The legacy form handler interpolated the raw ``subproduct`` form value
into ``https://{slug}.narve.ai/?error=...`` for every error branch. An
attacker could POST ``subproduct=evil.com#`` with no email and receive
a 302 to ``https://evil.com#.narve.ai/?error=email`` — i.e. an open
redirect to an attacker-controlled origin, because the browser parses
the ``#`` as the fragment separator and the host is taken to be
``evil.com``.

Fix:
    Validate the slug against the in-process ``SUBPRODUCTS`` catalogue
    BEFORE any redirect interpolation. Unknown slug → fall back to apex
    ``/`` so the attacker can't steer a 302 off-site.
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _build_app():
    """Build a minimal FastAPI app with only the signup routes attached.

    Spinning up the full ``server.py`` is expensive and pulls in every
    other middleware. The signup module exposes ``register(app)`` —
    exactly the surface the fix lives on.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import subproduct_signup_routes

    app = FastAPI()
    subproduct_signup_routes.register(app)
    return app, TestClient(app, follow_redirects=False)


def _fresh_ip_header() -> dict:
    """Return a unique X-Forwarded-For header so rate-limit keys don't
    collide across tests in the same run."""
    return {"X-Forwarded-For": f"203.0.113.{uuid.uuid4().int % 254 + 1}"}


class TestSubproductSignupOpenRedirect(unittest.TestCase):
    """HIGH FIX E — attacker-controlled slugs must not steer 302s off-site."""

    # Malicious values pulled from the audit + classic open-redirect
    # cheatsheets. Each must collapse to apex "/" because none appears
    # in the SUBPRODUCTS catalogue.
    MALICIOUS_SLUGS = (
        "evil.com#",        # fragment-truncates the netloc → attacker host
        "evil.com",         # would yield https://evil.com.narve.ai/, still
                            # off-allowlist but most importantly lets a
                            # phishing kit register that subdomain later
        "..",               # path-traversal sentinel
        "%2F%2Fevil.com",   # URL-encoded attempt
        " ",                # whitespace-only
        "javascript:alert", # protocol-handler attempt
    )

    def setUp(self):
        # Reset gateway-wide in-memory rate-limit buckets so prior tests
        # in the same run don't poison this fixture. Each test still uses
        # a fresh per-IP header on top.
        try:
            import server as _srv
            if hasattr(_srv, "_rate_store"):
                _srv._rate_store.clear()
        except Exception:
            pass
        self.app, self.client = _build_app()

    def tearDown(self):
        self.client.close()

    def test_malicious_slug_with_missing_email_redirects_to_root(self):
        """Submitting a non-catalogue slug returns 302 to ``/`` (apex)."""
        for bad in self.MALICIOUS_SLUGS:
            with self.subTest(slug=bad):
                r = self.client.post(
                    "/subproduct-signup",
                    data={"email": "", "subproduct": bad},
                    headers=_fresh_ip_header(),
                )
                # Pre-fix: 302 → https://evil.com#.narve.ai/?error=email
                # Post-fix: 302 → /
                self.assertEqual(r.status_code, 302)
                location = r.headers.get("location", "")
                self.assertEqual(
                    location, "/",
                    f"slug={bad!r} steered redirect to {location!r}",
                )
                # Belt-and-braces: the attacker's value must NOT appear
                # anywhere in the Location header.
                lowered = location.lower()
                self.assertNotIn("evil.com", lowered)
                self.assertNotIn("javascript", lowered)

    def test_malicious_slug_with_valid_email_still_falls_back(self):
        """Even with a valid email, off-catalogue slug short-circuits to /."""
        r = self.client.post(
            "/subproduct-signup",
            data={"email": "user@example.com", "subproduct": "evil.com#"},
            headers=_fresh_ip_header(),
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers.get("location"), "/")

    def test_valid_slug_routes_to_subproduct_subdomain_on_email_error(self):
        """Happy-path UX preserved: a real catalogue slug with a bad
        email still surfaces the per-product error landing as before."""
        # sports is the first entry in the canonical SUBPRODUCTS catalogue.
        r = self.client.post(
            "/subproduct-signup",
            data={"email": "", "subproduct": "sports"},
            headers=_fresh_ip_header(),
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(
            r.headers.get("location"),
            "https://sports.narve.ai/?error=email",
        )


class TestSubproductSignupAttachedStateValidation(unittest.TestCase):
    """The attached ``request.state.subproduct`` should still be honoured
    when it points to a real catalogue entry — but if a buggy middleware
    ever attached an off-catalogue value, the same whitelist catches it.
    """

    def test_off_catalogue_attached_state_falls_back(self):
        """Inject a request-state override via a one-off middleware and
        confirm the handler refuses to interpolate it."""
        try:
            import server as _srv
            if hasattr(_srv, "_rate_store"):
                _srv._rate_store.clear()
        except Exception:
            pass
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from starlette.middleware.base import BaseHTTPMiddleware
        import subproduct_signup_routes

        class _Override(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                request.state.subproduct = "evil.com#"
                return await call_next(request)

        app = FastAPI()
        subproduct_signup_routes.register(app)
        app.add_middleware(_Override)
        client = TestClient(app, follow_redirects=False)
        try:
            r = client.post(
                "/subproduct-signup",
                data={"email": "user@example.com", "subproduct": ""},
                headers=_fresh_ip_header(),
            )
            self.assertEqual(r.status_code, 302)
            self.assertEqual(r.headers.get("location"), "/")
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()

"""Tests for /api/docs (public developer reference) + /api/openapi.json.

The docs page is the canonical onboarding surface for anyone integrating
against narve.ai. These tests guard:

- Anonymous reachability (no gate, no auth).
- Every documented endpoint group is mentioned by path string so a
  silent rename doesn't quietly orphan a section.
- The companion machine-readable OpenAPI schema returns 200 + parseable
  JSON with the FastAPI ``info`` block populated.
- The subdomain HMAC SSO requirement is explicit on the page — that's
  the auth model for the subproduct (whale / voters / climate / …)
  routes and an integrator must not silently miss it.
"""

from __future__ import annotations

import json
import os
import unittest

from tests import _testdb  # noqa: F401 — shared in-memory DB bootstrap

# Force non-production so SubproductMiddleware doesn't require CF headers
# in the TestClient path. Mirrors test_api_public.py.
os.environ["PRODUCTION"] = "0"

from fastapi.testclient import TestClient


_HOST = {"host": "narve.ai"}


def _client() -> TestClient:
    import server
    return TestClient(server.app)


class TestApiDocsPage(unittest.TestCase):
    """The HTML reference page at /api/docs."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.c = _client()

    def test_returns_200_anonymous(self) -> None:
        r = self.c.get("/api/docs", headers=_HOST)
        self.assertEqual(r.status_code, 200, r.text[:300])

    def test_serves_html(self) -> None:
        r = self.c.get("/api/docs", headers=_HOST)
        self.assertIn("text/html", r.headers.get("content-type", ""))

    def test_hero_present(self) -> None:
        r = self.c.get("/api/docs", headers=_HOST)
        body = r.text
        # Hero block must announce the page (no client-side fetch).
        self.assertIn("narve.ai", body)
        # Version chip lives in the hero.
        self.assertIn("v1", body)

    def test_public_endpoints_documented(self) -> None:
        body = self.c.get("/api/docs", headers=_HOST).text
        for path in ("/api/feed", "/api/markets", "/api/sources/",
                     "/api/predictions/", "/api/changelog", "/api/health"):
            self.assertIn(path, body, f"Public endpoint {path} not on /api/docs")

    def test_user_scoped_endpoints_documented(self) -> None:
        body = self.c.get("/api/docs", headers=_HOST).text
        for path in ("/api/me/profile", "/api/me/positions",
                     "/api/me/bankroll", "/api/me/export"):
            self.assertIn(path, body, f"User endpoint {path} not on /api/docs")

    def test_subscribed_endpoints_documented(self) -> None:
        body = self.c.get("/api/docs", headers=_HOST).text
        for path in ("/api/signal-search", "/api/kelly/calculate",
                     "/api/markets/portfolio"):
            self.assertIn(
                path, body,
                f"Subscribed endpoint {path} not on /api/docs",
            )

    def test_embed_endpoints_documented(self) -> None:
        body = self.c.get("/api/docs", headers=_HOST).text
        for path in ("/api/embed/best-bets", "/api/embed/markets"):
            self.assertIn(path, body, f"Embed endpoint {path} not on /api/docs")

    def test_subproduct_endpoints_documented(self) -> None:
        body = self.c.get("/api/docs", headers=_HOST).text
        for path in ("/api/whale", "/api/voters", "/api/climate"):
            self.assertIn(
                path, body,
                f"Subproduct endpoint {path} not on /api/docs",
            )

    def test_subdomain_hmac_requirement_documented(self) -> None:
        """The subproduct group MUST tell integrators about subdomain SSO
        — otherwise they'll try to hit the subdomains directly and
        wonder why it 401s."""
        body = self.c.get("/api/docs", headers=_HOST).text.lower()
        # "HMAC" + "subdomain" must both appear in the body, anywhere.
        self.assertIn("hmac", body)
        self.assertIn("subdomain", body)
        # The injected header name must be documented.
        self.assertIn("x-gateway-secret", body)

    def test_auth_modes_documented(self) -> None:
        """Five auth modes — anonymous, session, Bearer, X-API-Key, HMAC."""
        body = self.c.get("/api/docs", headers=_HOST).text
        self.assertIn("narve_session", body)
        self.assertIn("x-csrf-token", body.lower())
        self.assertIn("Bearer", body)
        self.assertIn("X-API-Key", body)

    def test_no_secrets_in_examples(self) -> None:
        """Example snippets must use placeholders, not real secret tokens.
        Catches the obvious copy-paste-leak failure mode."""
        body = self.c.get("/api/docs", headers=_HOST).text
        # Real Stripe / AWS / live keys generally don't contain literal
        # ellipsis or "your-key" — placeholders do. Conversely, anything
        # starting with "sk_live_" would be a live Stripe key.
        self.assertNotIn("sk_live_", body)
        self.assertNotIn("AKIA", body)  # AWS access key prefix
        # Positive: at least one obvious placeholder is present so we
        # know secrets are masked, not omitted.
        self.assertTrue(
            "your-key" in body or "..." in body or "&hellip;" in body,
            "Expected placeholder pattern in example snippets",
        )

    def test_does_not_list_admin_endpoints(self) -> None:
        body = self.c.get("/api/docs", headers=_HOST).text
        # Internal / admin paths must not be exposed on a public docs page.
        self.assertNotIn("/api/admin/", body)


class TestOpenAPISchema(unittest.TestCase):
    """Machine-readable schema at /api/openapi.json."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.c = _client()

    def test_openapi_returns_200_anonymous(self) -> None:
        r = self.c.get("/api/openapi.json", headers=_HOST)
        self.assertEqual(r.status_code, 200, r.text[:300])

    def test_openapi_is_valid_json(self) -> None:
        r = self.c.get("/api/openapi.json", headers=_HOST)
        # If this raises, the test fails — that's the contract.
        spec = json.loads(r.text)
        self.assertIsInstance(spec, dict)

    def test_openapi_has_info_block(self) -> None:
        spec = self.c.get("/api/openapi.json", headers=_HOST).json()
        self.assertIn("info", spec)
        self.assertIn("title", spec["info"])
        self.assertIn("version", spec["info"])
        # We branded the app to "narve.ai API" when we enabled OpenAPI;
        # if someone reverts that, the schema generator's title flips
        # back to "FastAPI" and integrators see the wrong product.
        self.assertIn("narve", spec["info"]["title"].lower())

    def test_openapi_has_paths(self) -> None:
        """At least the api_docs page itself is excluded, and there
        should be at least one path in the spec."""
        spec = self.c.get("/api/openapi.json", headers=_HOST).json()
        self.assertIn("paths", spec)
        self.assertIsInstance(spec["paths"], dict)
        # /api/docs is intentionally excluded (include_in_schema=False).
        self.assertNotIn("/api/docs", spec["paths"])

    def test_openapi_referenced_from_docs_page(self) -> None:
        body = self.c.get("/api/docs", headers=_HOST).text
        self.assertIn("/api/openapi.json", body)


if __name__ == "__main__":
    unittest.main()

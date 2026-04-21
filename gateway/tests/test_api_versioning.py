"""Tests for the API URL versioning scheme (`/api/v1/`).

Verifies:

1. `/api/version` returns the expected metadata payload.
2. Legacy `/api/...` GETs 301-redirect to `/api/v1/...` with the required
   deprecation headers (`X-API-Deprecated`, `Sunset`, `Deprecation: true`).
3. Legacy `/api/...` POSTs 308-redirect (preserves method) to the v1 path.
4. Query strings survive the redirect.
5. `/api/v1/...` reaches the same handler as the redirected legacy call —
   parity of behaviour, not just URL cosmetics.
6. The `api_v1.py` developer-API routes (bearer-auth, `/api/v1/sources`
   etc.) are still reachable natively without being stripped to `/api/`.
7. FastAPI OpenAPI docs are served at `/api/v1/openapi.json` and list the
   migrated paths under their canonical `/api/v1/` URLs.

Uses the same in-memory-sqlite + db.conn re-bind trick the other HTTP
test files rely on.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import unittest

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("GLOBAL_RATE_LIMIT_PER_MIN", "10000")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402

_conn = sqlite3.connect(":memory:", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA foreign_keys = ON")


@contextlib.contextmanager
def _fake_conn():
    try:
        yield _conn
        _conn.commit()
    except Exception:
        _conn.rollback()
        raise


db.conn = _fake_conn
db.init_db()

import migrations  # noqa: E402
migrations.upgrade_to_head()

import server  # noqa: E402
import server_features  # noqa: F401,E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app, follow_redirects=False)


class _RebindMixin:
    @classmethod
    def setUpClass(cls):
        cls._prev_conn = db.conn
        db.conn = _fake_conn

    @classmethod
    def tearDownClass(cls):
        db.conn = cls._prev_conn

    def setUp(self):
        db.conn = _fake_conn
        # Reset per-IP rate-limit counters so back-to-back tests don't trip
        # the global limit (TestClient always reports host=testclient).
        if hasattr(server, "_rate_store"):
            server._rate_store.clear()


# ── /api/version ─────────────────────────────────────────────────────────────


class TestVersionEndpoint(_RebindMixin, unittest.TestCase):
    """`/api/version` and `/api/v1/version` both return the metadata payload."""

    def test_version_at_legacy_path(self):
        r = client.get("/api/version")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["current"], "v1")
        self.assertIn("v1", body["supported"])
        self.assertIsInstance(body["deprecated"], list)
        self.assertIn("docs_url", body)
        self.assertTrue(body["docs_url"].endswith("/api/v1/docs"))

    def test_version_at_v1_path(self):
        r = client.get("/api/v1/version")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["current"], "v1")

    def test_version_is_not_redirected(self):
        """`/api/version` must not 301 to `/api/v1/version` — it's the meta endpoint."""
        r = client.get("/api/version")
        self.assertNotIn(r.status_code, (301, 302, 303, 307, 308))


# ── Legacy /api/ redirects ──────────────────────────────────────────────────


class TestLegacyRedirects(_RebindMixin, unittest.TestCase):
    """Unversioned `/api/...` requests 301/308 to `/api/v1/...` with deprecation headers."""

    def test_legacy_get_returns_301(self):
        r = client.get("/api/newsletter/position?email=someone@example.com")
        self.assertEqual(r.status_code, 301)

    def test_legacy_get_location_header(self):
        r = client.get("/api/newsletter/position")
        self.assertEqual(
            r.headers.get("location"),
            "/api/v1/newsletter/position",
        )

    def test_legacy_get_preserves_query_string(self):
        r = client.get("/api/newsletter/position?email=ref@example.com&ref=xyz")
        self.assertEqual(
            r.headers.get("location"),
            "/api/v1/newsletter/position?email=ref@example.com&ref=xyz",
        )

    def test_legacy_post_returns_308_preserving_method(self):
        """A 308 on POST means clients do NOT silently rewrite to GET on retry."""
        r = client.post(
            "/api/newsletter",
            data={"email": "someone@example.com"},
        )
        self.assertEqual(r.status_code, 308)
        self.assertEqual(r.headers.get("location"), "/api/v1/newsletter")

    def test_deprecation_headers_present(self):
        r = client.get("/api/newsletter/position")
        self.assertEqual(r.headers.get("Deprecation"), "true")
        self.assertTrue(
            r.headers.get("X-API-Deprecated", "").startswith(
                "This endpoint will be removed on"
            )
        )
        # Sunset should be a valid HTTP-date string
        sunset = r.headers.get("Sunset", "")
        self.assertTrue(sunset.startswith("Thu, 31 Dec 2026"))

    def test_successor_version_link_header(self):
        r = client.get("/api/newsletter/position")
        link = r.headers.get("Link", "")
        self.assertIn("/api/v1/newsletter/position", link)
        self.assertIn('rel="successor-version"', link)

    def test_docs_url_path_also_redirects(self):
        """/api/docs → /api/v1/docs as a convenience."""
        r = client.get("/api/docs")
        self.assertEqual(r.status_code, 301)
        self.assertEqual(r.headers.get("location"), "/api/v1/docs")


# ── /api/v1/ parity ─────────────────────────────────────────────────────────


class TestV1Parity(_RebindMixin, unittest.TestCase):
    """A v1 request should produce the exact same body/status as its legacy twin."""

    def test_version_endpoint_parity(self):
        """/api/version (no redirect) and /api/v1/version (via rewrite) must
        produce byte-for-byte identical JSON — proves both routes reach the
        same handler with the same arguments."""
        a = client.get("/api/version")
        b = client.get("/api/v1/version")
        self.assertEqual(a.status_code, 200)
        self.assertEqual(b.status_code, 200)
        self.assertEqual(a.json(), b.json())

    def test_redirect_follows_to_same_handler(self):
        """GET /api/newsletter/position (legacy) followed by redirect should
        reach the same handler as a direct GET to /api/v1/newsletter/position.
        We only assert status-code parity — the body shape varies based on
        whether the email query param is supplied, which isn't the point of
        this test."""
        direct = client.get("/api/v1/newsletter/position")
        followed = client.get(
            "/api/newsletter/position",
            follow_redirects=True,
        )
        self.assertEqual(direct.status_code, followed.status_code)

    def test_version_endpoint_shape(self):
        """The version payload's schema is stable."""
        r = client.get("/api/v1/version")
        body = r.json()
        self.assertEqual(
            set(body.keys()) & {"current", "supported", "deprecated", "docs_url"},
            {"current", "supported", "deprecated", "docs_url"},
        )


# ── Native /api/v1/ routes (api_v1.py developer API) ────────────────────────


class TestNativeV1Routes(_RebindMixin, unittest.TestCase):
    """Routes registered natively at `/api/v1/...` must NOT be rewritten to
    `/api/...` (which has no handler for those paths)."""

    def test_v1_sources_requires_api_key_not_404(self):
        """`/api/v1/sources` is owned by api_v1.py. Without a Bearer token
        it should return 401, never 404 (which would mean the middleware
        wrongly stripped the /v1 prefix)."""
        r = client.get("/api/v1/sources")
        # api_v1.py raises HTTPException(401, "API key required. ...")
        self.assertIn(r.status_code, (401, 403))
        self.assertNotEqual(r.status_code, 404)

    def test_v1_predictions_requires_api_key_not_404(self):
        r = client.get("/api/v1/predictions")
        self.assertIn(r.status_code, (401, 403))
        self.assertNotEqual(r.status_code, 404)


# ── OpenAPI docs ────────────────────────────────────────────────────────────


class TestOpenAPIDocs(_RebindMixin, unittest.TestCase):
    """FastAPI docs served at /api/v1/{docs,openapi.json}."""

    def test_openapi_spec_served_at_v1_path(self):
        r = client.get("/api/v1/openapi.json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"].split(";")[0], "application/json")

    def test_openapi_spec_title_references_v1(self):
        r = client.get("/api/v1/openapi.json")
        spec = r.json()
        self.assertIn("v1", spec["info"]["version"])

    def test_openapi_paths_are_v1_prefixed(self):
        """Routes registered internally at `/api/...` must appear in the
        schema under `/api/v1/...` so published docs match the client
        contract."""
        r = client.get("/api/v1/openapi.json")
        spec = r.json()
        api_paths = [p for p in spec.get("paths", {}) if p.startswith("/api/")]
        # At least one migrated path should be shown as v1
        self.assertTrue(any(p.startswith("/api/v1/") for p in api_paths))
        # And nothing should be exposed as unversioned /api/foo (with the
        # sole exception of /api/version which intentionally ships both)
        non_v1 = [p for p in api_paths if not p.startswith("/api/v1/")]
        for p in non_v1:
            self.assertEqual(p, "/api/version", f"Unexpected unversioned path: {p}")


if __name__ == "__main__":
    unittest.main()

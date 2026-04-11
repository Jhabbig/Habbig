"""Tests for the /health endpoint and static-asset cache-busting."""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Ensure a gate token is set before importing server.py — server.py raises
# a startup error in production mode without one.
os.environ.setdefault("SITE_ACCESS_TOKEN", "test_token_48_chars_aaaaaaaaaaaaaaaaaaaaaaaaaaaa")

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402


class TestHealthEndpoint(unittest.TestCase):
    """Verify /health returns the documented structure and status codes."""

    def setUp(self):
        self.client = TestClient(server.app)

    def test_health_returns_200_when_ok(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("status", body)
        self.assertIn(body["status"], ("ok", "degraded"))

    def test_health_has_all_required_fields(self):
        r = self.client.get("/health")
        body = r.json()
        for key in ("status", "version", "environment", "timestamp",
                    "uptime_seconds", "checks"):
            self.assertIn(key, body, f"missing field: {key}")

    def test_health_checks_includes_database(self):
        r = self.client.get("/health")
        body = r.json()
        self.assertIn("database", body["checks"])
        self.assertIn(body["checks"]["database"], ("ok", "error"))

    def test_health_uptime_is_non_negative(self):
        r = self.client.get("/health")
        body = r.json()
        self.assertIsInstance(body["uptime_seconds"], int)
        self.assertGreaterEqual(body["uptime_seconds"], 0)

    def test_health_is_never_cached(self):
        r = self.client.get("/health")
        cache_control = r.headers.get("cache-control", "")
        self.assertIn("no-store", cache_control.lower(),
                      f"expected no-store in Cache-Control, got: {cache_control}")

    def test_health_bypasses_gate_middleware(self):
        """/health must work even without the site access cookie."""
        # Don't set any cookie; the gate middleware should let /health through.
        r = self.client.get("/health", cookies={})
        # 200 or 503 both mean the endpoint ran. A 302 would mean the gate
        # redirected us, which is what we're guarding against.
        self.assertIn(r.status_code, (200, 503))

    def test_health_503_when_database_unreachable(self):
        """Force a DB failure and verify HTTP 503."""
        with patch.object(server, "_check_database",
                          return_value=("error", "simulated")):
            r = self.client.get("/health")
            self.assertEqual(r.status_code, 503)
            body = r.json()
            self.assertEqual(body["status"], "error")
            self.assertEqual(body["checks"]["database"], "error")

    def test_health_degraded_when_non_critical_check_fails(self):
        """Non-critical failure should keep HTTP 200 but flip status to degraded."""
        # The static dir check is non-critical. Simulate its failure.
        with patch.object(server, "_check_static_dir",
                          return_value=("error", "simulated")):
            r = self.client.get("/health")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["status"], "degraded")
            self.assertEqual(body["checks"]["static"], "error")

    def test_health_environment_field(self):
        r = self.client.get("/health")
        body = r.json()
        self.assertIn(body["environment"], ("production", "staging", "dev", "test"))


class TestStaticCacheBusting(unittest.TestCase):
    """Verify static_url() produces content-hashed URLs."""

    def test_static_url_returns_unhashed_for_missing_file(self):
        url = server.static_url("nonexistent_file_zzz.css")
        # Falls back to unhashed path so the page still renders
        self.assertEqual(url, "/_gateway_static/nonexistent_file_zzz.css")

    def test_static_url_hashes_existing_file(self):
        # gateway.css definitely exists in the static dir
        url = server.static_url("gateway.css")
        self.assertIn("/_gateway_static/gateway.css", url)
        self.assertIn("?v=", url)
        hash_part = url.split("?v=")[1]
        self.assertEqual(len(hash_part), 8)

    def test_static_url_hash_is_deterministic(self):
        url1 = server.static_url("gateway.css")
        url2 = server.static_url("gateway.css")
        self.assertEqual(url1, url2)

    def test_static_url_different_files_different_hashes(self):
        # trade.js and gateway.css are different files, hashes should differ.
        js_url = server.static_url("trade.js")
        css_url = server.static_url("gateway.css")
        js_hash = js_url.split("?v=")[1] if "?v=" in js_url else ""
        css_hash = css_url.split("?v=")[1] if "?v=" in css_url else ""
        if js_hash and css_hash:
            self.assertNotEqual(js_hash, css_hash)


class TestStaticCacheHeaders(unittest.TestCase):
    """Verify the custom StaticFiles subclass attaches Cache-Control."""

    def setUp(self):
        self.client = TestClient(server.app)

    def test_static_css_has_immutable_cache_header(self):
        r = self.client.get("/_gateway_static/gateway.css")
        if r.status_code == 200:
            cache_control = r.headers.get("cache-control", "")
            self.assertIn("public", cache_control.lower())
            self.assertIn("max-age=2592000", cache_control.lower())
            self.assertIn("immutable", cache_control.lower())

    def test_static_vary_accept_encoding(self):
        r = self.client.get("/_gateway_static/gateway.css")
        if r.status_code == 200:
            vary = r.headers.get("vary", "")
            self.assertIn("accept-encoding", vary.lower())


if __name__ == "__main__":
    unittest.main()

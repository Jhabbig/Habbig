"""Unit tests for the health-monitor probe (commits 176c613 + 4b1553e).

Two bugs fixed in those commits:

  1. Probe used HEAD against /health, which 404s on @app.get-only routes
     and so reported every service DOWN even when they were healthy.
     Switched to GET. We assert this at the call-site level by patching
     ``httpx.Client`` and checking ``.get`` is the method invoked.

  2. /health on the subproduct backends isn't a uniform contract:
     several return 404 (no route), 401/403 (auth-gated), 5xx (real
     failure). The new probe treats 2xx/3xx/401/403 as alive, falls
     back to GET / when /health is 404, and reports DOWN only on
     5xx or socket-level failures.

Plus a small data-shape test that the SERVICES list reflects today's
catalog rename ("Top Traders" → "Traders", "World Health" → "Health").

These are pure unit tests — they patch ``httpx.Client`` so no real
sockets are opened. No DB needed, no FastAPI TestClient.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402

import admin_health_monitor_routes as ahm  # noqa: E402
from admin_health_monitor_routes import SERVICES, _probe  # noqa: E402


def _reset_ring() -> None:
    """Probe writes a sample to the 24h uptime ring on every call.
    Clear it between tests so ``uptime_24h`` doesn't bleed across tests."""
    with ahm._ring_lock:
        ahm._ring.clear()


class _FakeResponse:
    """Minimal stand-in for httpx.Response — only ``.status_code`` is read."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeClient:
    """Records calls to ``.get`` / ``.head`` and returns scripted responses.

    ``responses`` maps URL → response (or callable returning response /
    raising). If a URL isn't in the map, raises so the test fails loudly
    rather than silently coercing to ``down``.
    """

    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.get_calls: list[tuple[str, dict]] = []
        self.head_calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self._resolve(url)

    def head(self, url, **kwargs):  # pragma: no cover — would indicate regression
        self.head_calls.append((url, kwargs))
        return self._resolve(url)

    def _resolve(self, url: str):
        if url not in self.responses:
            raise AssertionError(f"FakeClient: no scripted response for {url!r}")
        entry = self.responses[url]
        if callable(entry):
            return entry()
        return entry

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


_SVC = {"name": "Test", "slug": "test-svc", "port": 9999}


class ProbeMethodTests(unittest.TestCase):
    """The HEAD→GET switch is the headline fix from commit 176c613."""

    def setUp(self):
        _reset_ring()

    def test_probe_uses_get_not_head(self):
        client = _FakeClient({"http://localhost:9999/health": _FakeResponse(200)})
        result = _probe(_SVC, client)
        self.assertEqual(len(client.get_calls), 1,
                         "probe must issue exactly one GET on the happy path")
        self.assertEqual(client.get_calls[0][0], "http://localhost:9999/health")
        self.assertEqual(client.head_calls, [],
                         "probe must NOT call client.head (regression from HEAD era)")
        self.assertEqual(result["status"], "up")


class ProbeStatusCodeTests(unittest.TestCase):
    """One test per branch of the 4b1553e status-code matrix."""

    def setUp(self):
        _reset_ring()

    def test_2xx_response_is_up(self):
        client = _FakeClient({"http://localhost:9999/health": _FakeResponse(200)})
        result = _probe(_SVC, client)
        self.assertEqual(result["status"], "up")
        self.assertIsNotNone(result["latency_ms"])

    def test_401_treated_as_up(self):
        """Auth-gated /health (Traders, Disasters) — process is alive."""
        client = _FakeClient({"http://localhost:9999/health": _FakeResponse(401)})
        result = _probe(_SVC, client)
        self.assertEqual(result["status"], "up")

    def test_403_treated_as_up(self):
        """Same rationale as 401 — auth wall, but the process is up."""
        client = _FakeClient({"http://localhost:9999/health": _FakeResponse(403)})
        result = _probe(_SVC, client)
        self.assertEqual(result["status"], "up")

    def test_404_falls_back_to_root(self):
        """No /health route (Sports, Weather, Crypto, Climate) → GET /."""
        client = _FakeClient({
            "http://localhost:9999/health": _FakeResponse(404),
            "http://localhost:9999/": _FakeResponse(200),
        })
        result = _probe(_SVC, client)
        self.assertEqual(result["status"], "up")
        # Two GETs: /health (404) then / (200). Still no HEAD.
        self.assertEqual(len(client.get_calls), 2)
        self.assertEqual(client.get_calls[0][0], "http://localhost:9999/health")
        self.assertEqual(client.get_calls[1][0], "http://localhost:9999/")
        self.assertEqual(client.head_calls, [])

    def test_404_with_no_root_is_down(self):
        """/health 404 and / unreachable → process not serving HTTP."""
        def boom():
            raise httpx.ConnectError("connection refused")

        client = _FakeClient({
            "http://localhost:9999/health": _FakeResponse(404),
            "http://localhost:9999/": boom,
        })
        result = _probe(_SVC, client)
        self.assertEqual(result["status"], "down")

    def test_5xx_is_down(self):
        """500 is a real failure — not 'alive but auth-gated'."""
        client = _FakeClient({"http://localhost:9999/health": _FakeResponse(500)})
        result = _probe(_SVC, client)
        self.assertEqual(result["status"], "down")

    def test_timeout_is_down(self):
        """Timeout → status=down, latency_ms pinned at the 2000ms cap."""
        def boom():
            raise httpx.TimeoutException("read timeout")

        client = _FakeClient({"http://localhost:9999/health": boom})
        result = _probe(_SVC, client)
        self.assertEqual(result["status"], "down")
        self.assertEqual(result["latency_ms"], 2000)


class ServiceCatalogTests(unittest.TestCase):
    """Catalog renames from commit 176c613."""

    def test_slug_renames_traders_health(self):
        names = {svc["name"] for svc in SERVICES}
        # Renames: Top Traders → Traders, World Health → Health.
        self.assertIn("Traders", names,
                      "SERVICES must contain 'Traders' (was 'Top Traders')")
        self.assertIn("Health", names,
                      "SERVICES must contain 'Health' (was 'World Health')")
        self.assertNotIn("Top Traders", names,
                         "'Top Traders' should have been renamed to 'Traders'")
        self.assertNotIn("World Health", names,
                         "'World Health' should have been renamed to 'Health'")

        # Slugs are untouched (URL-stable). Verify the rename only hit
        # the display name and the slug column still routes to the same
        # backend port.
        by_name = {svc["name"]: svc for svc in SERVICES}
        self.assertEqual(by_name["Traders"]["slug"], "top-traders")
        self.assertEqual(by_name["Health"]["slug"], "world-health")


if __name__ == "__main__":
    unittest.main()

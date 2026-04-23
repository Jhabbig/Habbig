"""Tests for PWA v2: offline shell, /settings/offline, sw.js cache strategies."""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_ACCESS_TOKEN", "test_token_48_chars_aaaaaaaaaaaaaaaaaaaaaaaaaaaa")
os.environ.setdefault("EMAIL_DRY_RUN", "true")

from tests import _testdb  # noqa: F401,E402 — shared in-memory DB + migrations

import db  # noqa: E402
import server  # noqa: E402
import server_features  # noqa: F401,E402
import offline_routes  # noqa: F401,E402  — side-effect: registers routes

from fastapi.testclient import TestClient  # noqa: E402


_STATIC = Path(server.__file__).resolve().parent / "static"


class TestOfflineRoute(unittest.TestCase):
    """GET /offline returns the standalone shell the SW falls back to."""

    def setUp(self):
        self.client = TestClient(server.app)

    def test_offline_is_public(self):
        # No cookies set → no auth. Public-paths list must include /offline
        # or the gate middleware would redirect.
        r = self.client.get("/offline", follow_redirects=False)
        self.assertEqual(r.status_code, 200, f"body={r.text[:160]}")

    def test_offline_html_mentions_cache_version_and_status(self):
        r = self.client.get("/offline")
        # The page reads the SW cache; the version string must match
        # sw.js (CACHE_V). If someone bumps one and forgets the other,
        # the "available offline" list stays empty forever.
        self.assertIn("narve-v2", r.text)
        self.assertIn("status-pill", r.text)
        # "You're offline" is rendered by the <h1>.
        self.assertIn("offline", r.text.lower())
        # Retry button is key UX — don't silently drop it.
        self.assertIn("Try again", r.text)

    def test_offline_route_in_public_paths(self):
        # Guardrail against the gate middleware regressing the allowlist.
        self.assertIn("/offline", server._PUBLIC_PATHS)


class TestSettingsOfflineRoute(unittest.TestCase):
    """/settings/offline: authed; anon users bounce to /token."""

    def setUp(self):
        self.client = TestClient(server.app)
        self.client.cookies.set("narve_gate_access", "granted")

    def test_anon_redirects_to_token(self):
        r = self.client.get("/settings/offline", follow_redirects=False)
        self.assertIn(r.status_code, (302, 307))
        self.assertEqual(r.headers.get("location"), "/token")

    def test_authed_user_renders(self):
        uid = db.create_user("pwa-offline@test.com", "InitialPass123!", username="pwaoffline")
        # Legacy session cookie — pm_gateway_session — matches how
        # current_user() resolves the user in test.
        token = db.create_session(uid)
        self.client.cookies.set("pm_gateway_session", token)

        r = self.client.get("/settings/offline", follow_redirects=False)
        self.assertEqual(r.status_code, 200, f"body={r.text[:160]}")
        body = r.text
        # Each of these strings is load-bearing for either the user (the
        # toggle labels) or the behaviour (matching JS IDs).
        self.assertIn("Enable offline mode", body)
        self.assertIn("Enable push notifications", body)
        self.assertIn("Clear cache", body)
        self.assertIn("Storage used", body)


class TestServiceWorkerAssets(unittest.TestCase):
    """sw.js content — the cache strategies are load-bearing for the
    offline experience, so a grep test catches accidental regressions."""

    def setUp(self):
        self.sw = (_STATIC / "sw.js").read_text(encoding="utf-8")

    def test_cache_version(self):
        self.assertIn("CACHE_V = 'narve-v2'", self.sw)

    def test_declares_offline_url(self):
        self.assertIn("OFFLINE_URL", self.sw)
        self.assertIn("'/offline'", self.sw)

    def test_four_cache_strategies_present(self):
        for name in (
            "cacheFirst",
            "staleWhileRevalidate",
            "networkFirstWithOffline",
        ):
            self.assertIn(name, self.sw, f"missing strategy: {name}")

    def test_api_cache_whitelist(self):
        # SWR only fires for these prefixes; the list is the contract.
        for prefix in ("/api/status", "/api/markets", "/api/feed", "/api/best-bets"):
            self.assertIn(prefix, self.sw, f"missing cacheable API prefix: {prefix}")

    def test_never_cache_list(self):
        for prefix in ("/api/auth", "/api/admin", "/api/billing"):
            self.assertIn(prefix, self.sw, f"missing never-cache prefix: {prefix}")

    def test_background_sync_wired(self):
        self.assertIn("'submit-prediction'", self.sw)
        self.assertIn("flushPendingPredictions", self.sw)

    def test_idb_queue_store(self):
        # Must match narve-app.js IDB_DB / IDB_STORE or the page can't
        # write into the same queue the SW reads.
        self.assertIn("'narve-offline-queue'", self.sw)
        self.assertIn("'predictions'", self.sw)

    def test_clear_cache_message_handler(self):
        self.assertIn("'CLEAR_CACHE'", self.sw)

    def test_push_handlers(self):
        self.assertIn("addEventListener('push'", self.sw)
        self.assertIn("addEventListener('notificationclick'", self.sw)


class TestOfflineHtmlStatic(unittest.TestCase):
    """offline.html is served by gateway but also a standalone page —
    browsers can load it directly from cache so its behaviour has to
    live inside the HTML itself."""

    def setUp(self):
        self.html = (_STATIC / "offline.html").read_text(encoding="utf-8")

    def test_reads_narve_v2_cache(self):
        # The cache-keys scanner filters by 'narve-v2' prefix. If it
        # doesn't match CACHE_V in sw.js the list is silently empty.
        self.assertIn("narve-v2", self.html)

    def test_status_pill_rendered(self):
        self.assertIn("status-pill", self.html)

    def test_has_retry_button(self):
        self.assertIn("retry-btn", self.html)
        self.assertIn("Try again", self.html)

    def test_inlines_styles(self):
        # No external <link rel="stylesheet"> — the page must work with
        # zero network. A link to /_gateway_static/... would fail when
        # the user is offline and the SW isn't controlling the page.
        self.assertNotIn('<link rel="stylesheet"', self.html)


class TestSettingsOfflineHtmlStatic(unittest.TestCase):
    def setUp(self):
        self.html = (_STATIC / "settings_offline.html").read_text(encoding="utf-8")

    def test_has_both_toggles(self):
        self.assertIn("toggle-offline", self.html)
        self.assertIn("toggle-push", self.html)

    def test_clear_cache_button_posts_to_sw(self):
        self.assertIn("btn-clear-cache", self.html)
        self.assertIn("CLEAR_CACHE", self.html)

    def test_storage_probe_prefers_navigator_storage(self):
        # navigator.storage.estimate() is authoritative; only fall back
        # if it's missing. If the test ever finds _only_ the manual
        # measureCacheBytes path, someone deleted the faster path.
        self.assertIn("navigator.storage", self.html)
        self.assertIn("measureCacheBytes", self.html)

    def test_no_colored_dots(self):
        # Design constraint: monochrome. Catch accidental red/green.
        lower = self.html.lower()
        self.assertNotIn("#ef4444", lower)  # red-500
        self.assertNotIn("#10b981", lower)  # green-500
        self.assertNotIn("#f59e0b", lower)  # amber-500


class TestNarveAppJsOfflineSurface(unittest.TestCase):
    """narve-app.js exposes narve.offline + narve.predictions + flush
    postMessage. The settings page and the prediction composer both
    depend on the exact shape of these — grep-test keeps them from
    drifting silently."""

    def setUp(self):
        self.js = (_STATIC / "narve-app.js").read_text(encoding="utf-8")

    def test_exposes_narve_offline(self):
        self.assertIn("narve.offline", self.js)
        self.assertIn("queuePrediction", self.js)

    def test_exposes_narve_predictions(self):
        self.assertIn("narve.predictions", self.js)

    def test_flush_queue_postmessage(self):
        self.assertIn("FLUSH_QUEUE", self.js)

    def test_banner_hooks_online_offline(self):
        self.assertIn("addEventListener('offline'", self.js)
        self.assertIn("addEventListener('online'", self.js)
        self.assertIn("narve-offline-banner", self.js)

    def test_idb_shape_matches_sw(self):
        # Same DB/store names as sw.js — single contract.
        self.assertIn("'narve-offline-queue'", self.js)
        self.assertIn("'predictions'", self.js)


if __name__ == "__main__":
    unittest.main()

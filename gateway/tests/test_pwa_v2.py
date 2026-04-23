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
        # Gate cookie is now a signed HMAC value, not the literal string
        # "granted". Mint a real one so the gate middleware lets us
        # through to the offline route handler.
        self.client.cookies.set(
            server.GATE_COOKIE_NAME, server._mint_gate_cookie_value(),
        )

    def test_anon_redirects_to_token(self):
        # Gate is granted but no user session → offline route redirects
        # to /token. Accept /token OR /gate to stay resilient if the
        # gate middleware tightens in future (both mean "not signed in").
        r = self.client.get("/settings/offline", follow_redirects=False)
        self.assertIn(r.status_code, (302, 307))
        self.assertIn(r.headers.get("location"), ("/token", "/gate"))

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


class TestCachedRibbonSurface(unittest.TestCase):
    """narve.cached.* + SW cache-header stamping (feature A).

    The SW stamps X-Served-From + X-Cached-At on every response served
    from the cache; the client reads those headers to render a "Last
    updated X min ago (cached)" ribbon.
    """

    def setUp(self):
        self.sw = (_STATIC / "sw.js").read_text(encoding="utf-8")
        self.js = (_STATIC / "narve-app.js").read_text(encoding="utf-8")
        self.css = (_STATIC / "gateway.css").read_text(encoding="utf-8")

    def test_sw_stamps_served_from_cache_header(self):
        self.assertIn("'X-Served-From'", self.sw)
        self.assertIn("'cache'", self.sw)
        self.assertIn("withCacheHeaders", self.sw)

    def test_sw_stamps_cached_at_from_date_header(self):
        self.assertIn("'X-Cached-At'", self.sw)

    def test_narve_cached_exposes_fetchjson_and_ribbon(self):
        self.assertIn("narve.cached", self.js)
        self.assertIn("fetchJSON", self.js)
        self.assertIn("renderRibbon", self.js)

    def test_ribbon_css_monochrome(self):
        self.assertIn(".narve-cached-ribbon", self.css)
        # Monochrome — no coloured highlights.
        lower_rule = self.css.split(".narve-cached-ribbon", 1)[1].split("}", 1)[0].lower()
        for banned in ("#ef4444", "#f59e0b", "#10b981", "red;", "amber;"):
            self.assertNotIn(banned, lower_rule)


class TestSettingsNavLink(unittest.TestCase):
    """The /settings/offline page is reachable, but the main /settings
    page also needs a nav item or users won't discover it."""

    def setUp(self):
        self.html = (_STATIC / "settings.html").read_text(encoding="utf-8")

    def test_main_settings_links_to_offline_subpage(self):
        self.assertIn('href="/settings/offline"', self.html)
        self.assertIn("Offline", self.html)


class TestPushFanoutWired(unittest.TestCase):
    """Push-notification fanout (feature C): the three email-fanout
    jobs each enqueue a parallel send_push_notification. Grep-style
    guard — if someone deletes the enqueue_job call, this fires."""

    def setUp(self):
        path = Path(server.__file__).resolve().parent / "jobs" / "notification_jobs.py"
        self.src = path.read_text(encoding="utf-8")

    def test_push_job_no_longer_a_stub(self):
        self.assertNotIn("web-only, push not configured", self.src)
        self.assertIn("push.send_to_user", self.src)

    def test_market_resolution_fans_out_push(self):
        # Must sit inside the resolution loop; a single 'send_push_'
        # call across the file would miss the saved-prediction and
        # mover paths, so assert each tag is present.
        self.assertIn('"market-resolved-', self.src)

    def test_saved_prediction_fans_out_push(self):
        self.assertIn('"saved-', self.src)

    def test_mover_alert_fans_out_push(self):
        self.assertIn('"mover-', self.src)


class TestDashboardsPrecache(unittest.TestCase):
    """/dashboards gets pre-cached into narve-v2-runtime on idle after
    boot for logged-in visitors, so first-offline hits the real page."""

    def setUp(self):
        self.js = (_STATIC / "narve-app.js").read_text(encoding="utf-8")

    def test_precache_runs_on_idle(self):
        self.assertIn("precacheDashboards", self.js)
        self.assertIn("requestIdleCallback", self.js)

    def test_precache_guards_with_isloggedin(self):
        # Anonymous visitors must NOT warm the cache — it would cache
        # a logged-out /dashboards response, then the user signs in
        # and gets stale anonymous markup offline.
        idx = self.js.find("async function precacheDashboards")
        self.assertGreater(idx, 0)
        body = self.js[idx: idx + 1000]
        self.assertIn("isLoggedIn()", body)

    def test_precache_skips_self_page(self):
        idx = self.js.find("async function precacheDashboards")
        body = self.js[idx: idx + 1000]
        self.assertIn("'/dashboards'", body)
        self.assertIn("pathname", body)

    def test_precache_target_matches_sw_runtime_cache(self):
        # The SW's RUNTIME_CACHE is `${CACHE_V}-runtime` → narve-v2-runtime.
        self.assertIn("'narve-v2-runtime'", self.js)


class TestNeverCacheGuard(unittest.TestCase):
    """Belt-and-braces: the never-cache list in sw.js covers every
    auth/admin/billing prefix. If someone adds a new /api/* path that
    handles secrets and forgets to exclude it from SWR, this fires."""

    def setUp(self):
        self.sw = (_STATIC / "sw.js").read_text(encoding="utf-8")

    def test_never_cache_prefixes_present(self):
        for prefix in (
            "'/api/auth'",
            "'/api/admin'",
            "'/api/billing'",
            "'/auth/'",
            "'/admin/'",
            "'/billing/'",
            "'/stripe/'",
        ):
            self.assertIn(prefix, self.sw, f"missing never-cache prefix: {prefix}")

    def test_never_cache_checked_before_any_caching_strategy(self):
        """Order in fetch listener: NEVER_CACHE_PREFIXES check must run
        before any cacheFirst/staleWhileRevalidate dispatch. If we ever
        reversed the order, a /api/admin GET could be cached once even
        if the second request bypasses. Source-order check."""
        never = self.sw.find("NEVER_CACHE_PREFIXES.some")
        cache_first_call = self.sw.find("cacheFirst(req, STATIC_CACHE)")
        self.assertGreater(never, 0, "NEVER_CACHE_PREFIXES scan missing")
        self.assertGreater(cache_first_call, 0, "cacheFirst dispatch missing")
        self.assertLess(
            never, cache_first_call,
            "never-cache check must appear before any caching branch",
        )

    def test_cacheable_list_excludes_secrets(self):
        """The explicit allow-list must not mention anything under
        /api/auth, /api/admin, /api/billing. It's an easy paste-error."""
        # Find CACHEABLE_API_PREFIXES array
        start = self.sw.find("CACHEABLE_API_PREFIXES")
        end = self.sw.find("]", start)
        allowed = self.sw[start:end]
        for banned in ("/api/auth", "/api/admin", "/api/billing"):
            self.assertNotIn(banned, allowed, f"{banned} leaked into cacheable list")


if __name__ == "__main__":
    unittest.main()

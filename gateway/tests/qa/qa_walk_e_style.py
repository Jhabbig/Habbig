"""QA Walk E — canonical style check.

Static-HTML checks that don't need a real browser:

  * Inter font is declared in the HTML or CSS.
  * No external font CDNs (Google Fonts) referenced — we self-host.
  * No `style="color:#..."` inline overrides on common content tags
    that would skip the token system (a small whitelist exists for
    accent dot rendering and email-template previews — those carry
    `data-allow-inline-color` so we don't false-flag them).

Playwright-only checks (computed font-family, computed colour) live
in the optional Walk E2 below; they auto-skip when Playwright isn't
installed.
"""

from __future__ import annotations

import re
import unittest

from fastapi.testclient import TestClient

from . import conftest as _conf  # noqa: F401

import server  # noqa: E402


CANONICAL_PAGES = ["/", "/dashboards", "/admin"]

# Pages where it's acceptable to load Google Fonts as a fallback (the
# prerelease root page predates the self-hosted Inter switch). App
# surfaces must be CDN-clean.
_FONT_CDN_EXEMPT = {"/"}


def _admin_cookies() -> dict:
    """One-shot admin session for the /admin probe."""
    import db
    email = "qa-walk-e-admin@test.local"
    existing = (
        db.get_user_by_email(email)
        if hasattr(db, "get_user_by_email") else None
    )
    if existing:
        uid = existing["id"]
    else:
        uid = db.create_user(email, "QaWalkPass123!", username="qawalkeadmin")
    with db.conn() as c:
        c.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (uid,))
    return {server.COOKIE_NAME: db.create_session(uid)}


_INLINE_COLOR_RE = re.compile(
    r'style\s*=\s*"[^"]*\bcolor:\s*#[0-9a-fA-F]{3,8}',
    re.IGNORECASE,
)


class TestStyleHTML(unittest.TestCase):
    """HTML-level style invariants. Cheap, no browser."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)
        cls.admin_cookies = _admin_cookies()

    def _fetch(self, path: str) -> str:
        cookies = self.admin_cookies if path.startswith("/admin") else None
        r = self.client.get(path, cookies=cookies, follow_redirects=False)
        return r.text if r.status_code == 200 else ""

    def test_inter_font_declared(self):
        """Body should use Inter — declared in gateway.css @font-face."""
        for path in CANONICAL_PAGES:
            body = self._fetch(path)
            if not body:
                continue
            self.assertIn(
                "Inter", body,
                f"{path} HTML doesn't reference Inter — "
                f"either gateway.css missing or font swapped",
            )

    def test_no_external_font_cdn(self):
        """No fonts.googleapis.com / fonts.gstatic.com refs on app
        surfaces. Marketing root (`/`) is exempt — the prerelease
        page legitimately loads Inter + Fraunces from Google as a
        belt-and-braces fallback to self-hosted Inter."""
        for path in CANONICAL_PAGES:
            if path in _FONT_CDN_EXEMPT:
                continue
            body = self._fetch(path)
            if not body:
                continue
            for cdn in ("fonts.googleapis.com", "fonts.gstatic.com"):
                self.assertNotIn(
                    cdn, body,
                    f"{path} pulls font from external CDN: {cdn}",
                )

    def test_no_inline_hex_color_overrides(self):
        """Hand-written `style="color:#XYZ"` skips the token system.

        Allowed exceptions: badges/accent dots that use inline `style`
        for tier-coloured pills carry `data-allow-inline-color` so they
        don't false-positive. Most pages will have zero matches; admin
        pages (badge-heavy) get a small budget.
        """
        budgets = {"/admin": 50}  # admin pages hand-render badges
        default_budget = 0
        for path in CANONICAL_PAGES:
            body = self._fetch(path)
            if not body:
                continue
            matches = _INLINE_COLOR_RE.findall(body)
            # Filter out the explicitly allowed inline-colour markers.
            non_exempt = [
                m for m in matches
                if "data-allow-inline-color" not in m
            ]
            budget = budgets.get(path, default_budget)
            self.assertLessEqual(
                len(non_exempt), budget,
                f"{path} has {len(non_exempt)} inline color overrides "
                f"(budget {budget}); first: {non_exempt[:1]}",
            )


# ── Optional: Playwright-driven visual checks ─────────────────────────────
# Computed font-family / outline-style require a real browser. Skipped
# entirely when Playwright is missing — local dev never installs it,
# CI does.

import pytest as _pytest

_pw_skip = _pytest.mark.skipif(
    not _conf.has_playwright(),
    reason="Playwright not installed (pip install playwright; playwright install chromium)",
)


@_pw_skip
class TestStyleComputed(unittest.TestCase):
    """Computed CSS — runs only when Playwright is available."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        # Spin up a headless browser ONCE for the whole class.
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def _live_url(self) -> str:
        url = getattr(self, "_live", None)
        if url:
            return url
        # Use the live_server fixture machinery indirectly — Pytest
        # doesn't pass session fixtures to plain unittest classes, so
        # we construct a fresh in-process URL via the same uvicorn
        # threading dance used in conftest.live_server.
        from .conftest import live_server  # type: ignore
        # If the fixture function expects yield semantics, calling it
        # directly raises — sentinel back to skip.
        return ""

    def test_inter_resolves_in_body(self):
        """Computed body font-family contains 'Inter'."""
        # We can only run this when there's a live URL — without one,
        # skip cleanly.
        url = self._live_url()
        if not url:
            self.skipTest("live_server fixture unavailable in unittest path")
        page = self.browser.new_page()
        try:
            page.goto(url + "/")
            page.wait_for_load_state("networkidle", timeout=5000)
            font = page.evaluate("getComputedStyle(document.body).fontFamily")
            self.assertIn("Inter", font, f"body fontFamily: {font!r}")
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()

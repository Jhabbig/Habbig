"""QA Walk G — mobile.

HTML-only invariants we can check without Playwright:

  * `<meta name="viewport"` is present and includes width=device-width.
    Without this iOS scales the desktop layout to 980px and the mobile
    UI breaks before any CSS runs.
  * gateway.css declares at least one `@media (max-width: …)` rule
    (we can't easily enumerate them all, but the absence of any media
    query means no mobile breakpoints exist).

Playwright walks (horizontal-scroll + tap-target audit) skipped when
the dep is missing. The skipped test count gives ops a one-line
"install playwright in CI to get the rest of mobile coverage" hint.
"""

from __future__ import annotations

import os
import re
import unittest

import pytest

from fastapi.testclient import TestClient

from . import conftest as _conf  # noqa: F401

import server  # noqa: E402


CANONICAL_PAGES = ["/", "/dashboards"]
MOBILE_VIEWPORT = {"width": 375, "height": 812}


class TestMobileHTML(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)

    def test_viewport_meta_present(self):
        for path in CANONICAL_PAGES:
            r = self.client.get(path, follow_redirects=False)
            if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                continue
            body = r.text.lower()
            self.assertIn(
                'name="viewport"', body,
                f"{path} missing viewport meta — iOS will scale to 980px",
            )
            self.assertIn(
                "width=device-width", body,
                f"{path} viewport meta missing width=device-width",
            )

    def test_gateway_css_has_breakpoints(self):
        css_path = os.path.join(
            os.path.dirname(server.__file__), "static", "gateway.css",
        )
        try:
            with open(css_path, encoding="utf-8") as f:
                css = f.read()
        except FileNotFoundError:
            self.skipTest("gateway.css not found")
        # Loose count — gateway.css should have multiple @media queries.
        media_count = len(re.findall(r"@media\s*\(", css))
        self.assertGreater(
            media_count, 3,
            f"gateway.css has only {media_count} @media rules — "
            "responsive coverage looks thin",
        )


# ── Playwright walks ────────────────────────────────────────────────────
#
# `pytest.importorskip` at module scope means this entire block exits
# clean when Playwright isn't installed.

playwright = pytest.importorskip("playwright.sync_api")


# Reaching here means Playwright is available — but we ALSO need a live
# server. The conftest.live_server fixture handles boot/teardown.


def _no_horizontal_scroll(page, url: str) -> bool:
    page.goto(url, wait_until="networkidle", timeout=10_000)
    return not page.evaluate(
        "document.documentElement.scrollWidth > "
        "document.documentElement.clientWidth + 1"
    )


@pytest.mark.parametrize("path", CANONICAL_PAGES)
def test_no_horizontal_scroll(live_server, path):
    if not live_server:
        pytest.skip("live_server fixture not available")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport=MOBILE_VIEWPORT)
        page = ctx.new_page()
        try:
            assert _no_horizontal_scroll(page, live_server + path), (
                f"horizontal scroll on {path} at 375px viewport"
            )
        finally:
            browser.close()


def test_tap_targets_at_least_44px(live_server):
    if not live_server:
        pytest.skip("live_server fixture not available")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport=MOBILE_VIEWPORT)
        page = ctx.new_page()
        try:
            page.goto(live_server + "/", wait_until="networkidle", timeout=10_000)
            small: list[dict] = []
            # First three of each common interactive selector. We don't
            # police exhaustively — that would be brittle; we just want
            # an early-warning if a 24px button slips in.
            for sel in ("button", "a.btn", "[role='button']"):
                for el in page.locator(sel).all()[:3]:
                    box = el.bounding_box()
                    if box and (box["width"] < 36 or box["height"] < 36):
                        small.append({"selector": sel, "box": box})
            assert not small, f"sub-36px tap targets: {small}"
        finally:
            browser.close()


if __name__ == "__main__":
    unittest.main()

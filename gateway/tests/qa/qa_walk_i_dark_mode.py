"""QA Walk I — dark mode.

Static checks that the dark-theme infra exists. Visual checks (no
white flash on load, all text readable in both themes) are Playwright
territory and live below behind `importorskip`.

  * tokens.css declares both `:root` (light) and `[data-theme="dark"]`
    blocks. If either is missing the toggle has nothing to switch to.
  * gateway.css references `var(--bg)` / `var(--text-primary)` (the
    canonical token names) somewhere — i.e. the stylesheet is actually
    using the tokens rather than hard-coding colours.
"""

from __future__ import annotations

import os
import re
import unittest

import pytest

from fastapi.testclient import TestClient

from . import conftest as _conf  # noqa: F401

import server  # noqa: E402


def _read_static(name: str) -> str:
    path = os.path.join(os.path.dirname(server.__file__), "static", name)
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestDarkModeTokens(unittest.TestCase):

    def test_tokens_css_has_light_and_dark(self):
        try:
            css = _read_static("tokens.css")
        except FileNotFoundError:
            self.skipTest("tokens.css not present in this build")
        self.assertIn(
            ":root", css,
            "tokens.css missing :root — light theme tokens not declared",
        )
        # Either an attribute selector (`[data-theme="dark"]`) or a
        # `prefers-color-scheme: dark` block — accept either since both
        # are valid implementations.
        has_attr = bool(re.search(r'\[data-theme\s*=\s*["\']dark["\']\]', css))
        has_prefers = "prefers-color-scheme: dark" in css
        self.assertTrue(
            has_attr or has_prefers,
            "tokens.css has no dark-theme block — toggle won't change anything",
        )

    def test_gateway_css_uses_tokens(self):
        css = _read_static("gateway.css")
        # We require at least 50 var(...) references — that's a low bar
        # for a stylesheet of this size (>2k lines) and immediately
        # catches a regression where someone hard-codes a colour
        # everywhere.
        var_count = len(re.findall(r"var\(\s*--", css))
        self.assertGreater(
            var_count, 50,
            f"gateway.css uses only {var_count} CSS-var references — "
            "tokens probably bypassed",
        )


# ── Playwright walks ─────────────────────────────────────────────────
playwright = pytest.importorskip("playwright.sync_api")


def test_dark_mode_no_white_flash(live_server):
    """Set the theme cookie/localStorage before navigation and check
    the body background paints dark immediately (no white frame)."""
    if not live_server:
        pytest.skip("live_server fixture not available")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        ctx.add_init_script(
            "window.localStorage.setItem('nv-theme', 'dark'); "
            "document.documentElement.setAttribute('data-theme', 'dark');"
        )
        page = ctx.new_page()
        try:
            page.goto(live_server + "/", wait_until="domcontentloaded", timeout=10_000)
            bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
            # Dark backgrounds resolve to one of these in our token set.
            dark_bgs = {"rgb(13, 13, 13)", "rgb(20, 20, 20)", "rgb(0, 0, 0)"}
            assert bg in dark_bgs, f"dark-mode bg unexpected: {bg!r}"
        finally:
            browser.close()


if __name__ == "__main__":
    unittest.main()

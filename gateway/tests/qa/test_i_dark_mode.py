"""Walk I — dark mode renders without a flash.

The inline theme-init script in _base.html runs synchronously after
the doctype + before the stylesheet load so the data-theme attribute
is set before the browser paints. This walk seeds the theme cookie,
loads the page, and asserts:

  1. data-theme="dark" is on <html>
  2. body background is the dark token, not the light one
  3. Foreground text isn't dark-on-dark (a regression where the
     theme attr applies but the colour tokens didn't switch)
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from .pages import CANONICAL_PAGES  # noqa: E402


# Permissible dark-mode body backgrounds. Multiple are listed because
# different routes use slightly different surface tokens (#0d0d0d for
# bg-base, #141414 for bg-surface, etc.). Anything in this set is
# accepted; anything outside it is suspicious.
DARK_BG_VALUES = {
    "rgb(13, 13, 13)",
    "rgb(20, 20, 20)",
    "rgb(0, 0, 0)",
    "rgb(15, 15, 15)",
    "rgb(18, 18, 18)",
}


def _seed_dark_theme(page) -> None:
    """Drop the dark-theme cookie + localStorage entry the inline
    init script reads. Runs as an init script so it's set BEFORE the
    page loads (otherwise the first paint has the default theme)."""
    page.add_init_script("""
        try {
          localStorage.setItem('narve-theme', 'dark');
          localStorage.setItem('betyc-theme', 'dark');
          document.cookie = 'narve-theme=dark;path=/;max-age=31536000';
        } catch (e) {}
    """)


@pytest.mark.parametrize("path", CANONICAL_PAGES)
def test_dark_theme_attr_applied(page, browser_server, path):
    _seed_dark_theme(page)
    page.goto(f"{browser_server}{path}", wait_until="domcontentloaded")
    theme = page.get_attribute("html", "data-theme")
    assert theme == "dark", f"{path}: <html data-theme> = {theme!r}, expected 'dark'"


@pytest.mark.parametrize("path", CANONICAL_PAGES)
def test_body_bg_resolves_dark(page, browser_server, path):
    _seed_dark_theme(page)
    page.goto(f"{browser_server}{path}", wait_until="networkidle")
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    # Some pages render `transparent` (rgba(0,0,0,0)) on body and the
    # dark surface lives on a wrapper. Accept transparent + check
    # the wrapper as a fallback.
    if bg in {"rgba(0, 0, 0, 0)", "transparent"}:
        wrapper_bg = page.evaluate("""
            () => {
              const w = document.querySelector('.app-shell, main, .landing-body, .imp-wrap, body > div');
              return w ? getComputedStyle(w).backgroundColor : null;
            }
        """)
        bg = wrapper_bg or bg
    assert bg in DARK_BG_VALUES, (
        f"{path}: body/wrapper bg = {bg!r}, expected one of {DARK_BG_VALUES}"
    )


@pytest.mark.parametrize("path", CANONICAL_PAGES)
def test_text_not_dark_on_dark(page, browser_server, path):
    """The most common dark-mode regression: data-theme flipped but a
    component hard-coded `color: #0d0d0d`. Read the computed colour
    of the first heading and assert it isn't the same shade family
    as the background."""
    _seed_dark_theme(page)
    page.goto(f"{browser_server}{path}", wait_until="networkidle")
    heading = page.locator("h1, h2, .page-title").first
    if heading.count() == 0:
        pytest.skip(f"{path}: no heading element to sample")
    text_color = heading.evaluate("el => getComputedStyle(el).color")
    # Parse "rgb(r, g, b)" or "rgba(r, g, b, a)".
    import re
    m = re.search(r"\d+,\s*\d+,\s*\d+", text_color)
    if not m:
        pytest.skip(f"{path}: unparseable colour {text_color!r}")
    r, g, b = (int(x) for x in m.group(0).split(","))
    # Heuristic: if the heading is "near black" (each channel < 60),
    # it's invisible on a dark background — flag it.
    near_black = r < 60 and g < 60 and b < 60
    assert not near_black, (
        f"{path}: heading text color {text_color} is near-black on dark theme"
    )

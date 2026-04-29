"""Walk G — mobile-viewport sanity at 375 px.

Three checks that catch the regressions a desktop QA pass misses:

  1. No horizontal scrollbar on the canonical pages — the most common
     mobile regression is a fixed-width element overflowing the
     viewport, and the only way to catch it is to actually load the
     page at 375 px wide.
  2. Tap targets ≥ 44×44 px on the marketing landing — Apple HIG /
     WCAG 2.5.5 floor.
  3. Form inputs ≥ 16 px on /login — iOS Safari auto-zooms when
     inputs are smaller, which breaks the auth flow on phones.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from .pages import CANONICAL_PAGES  # noqa: E402


@pytest.mark.parametrize("path", CANONICAL_PAGES)
def test_no_horizontal_scroll(mobile_page, browser_server, path):
    mobile_page.goto(f"{browser_server}{path}", wait_until="networkidle")
    mobile_page.wait_for_timeout(300)
    has_hscroll = mobile_page.evaluate(
        # +1 fudge: hairline differences between scrollWidth and
        # clientWidth happen with sub-pixel rounding on retina contexts.
        "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
    )
    assert not has_hscroll, f"{path}: horizontal scroll at 375 px"


def test_tap_targets_meet_min_size(mobile_page, browser_server):
    """First few interactive elements on the homepage are at least
    44×44 px. We don't enforce this on every button because some
    legitimate icon-only buttons live inside a larger row that's
    actually the tap target — but the canonical CTAs on the landing
    page must hit the floor."""
    mobile_page.goto(f"{browser_server}/", wait_until="networkidle")
    too_small = []
    selectors = ["a.landing-primary-cta", "a.landing-secondary-cta", "button.btn-primary"]
    for sel in selectors:
        for el in mobile_page.locator(sel).all()[:3]:
            try:
                box = el.bounding_box()
                if box and (box["width"] < 44 or box["height"] < 44):
                    too_small.append((sel, box))
            except Exception:
                continue
    assert not too_small, f"tap targets too small: {too_small}"


def test_login_inputs_min_16px_to_prevent_ios_zoom(mobile_page, browser_server):
    """iOS Safari auto-zooms on focus when the input's font-size is
    < 16 px — that breaks the layout mid-flow. /login must keep all
    inputs at 16 px minimum on a mobile viewport."""
    mobile_page.goto(f"{browser_server}/login", wait_until="domcontentloaded")
    inputs = mobile_page.locator("input:not([type='hidden'])").all()
    if not inputs:
        pytest.skip("/login has no inputs in this build")
    too_small = []
    for inp in inputs:
        try:
            size = inp.evaluate("el => parseFloat(getComputedStyle(el).fontSize)")
            if size and size < 16:
                too_small.append(size)
        except Exception:
            continue
    assert not too_small, (
        f"/login inputs with font-size < 16 px (iOS zoom trigger): {too_small}"
    )

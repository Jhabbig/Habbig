"""Visual regression + mobile-ergonomics test sweep.

Loads each public page at every target viewport and asserts:

  * HTTP 200 (we can actually render)
  * No horizontal scroll on any mobile-class viewport
  * Every ``<input>`` has font-size ≥ 16px on mobile (prevents iOS auto-zoom)
  * Every ``<button>`` / ``<a role="button">`` has width & height ≥ 32px
    (Apple HIG: 44×44; we use 32 as the lower bound so existing dense UIs
     aren't gratuitously failed, and the manual QA checklist picks up
     the stricter 44px bar)
  * A screenshot is persisted under tests/browser/screenshots/<viewport>_<path>.png

Public pages only — authed dashboards need a separate fixture stack
(gate + login + session cookie) that the e2e tests already own. Those
get their own browser sweep in test_critical_flows.py.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

import pytest


PUBLIC_PAGES = [
    "/",
    "/pricing",
    "/about",
    "/how-it-works",
    "/methodology",
    "/faq",
    "/privacy",
    "/terms",
]


def _safe_path(url_path: str) -> str:
    return url_path.strip("/").replace("/", "_") or "home"


@pytest.fixture
def chromium_page(browser_factory):
    browser = browser_factory("chromium")
    try:
        yield browser
    finally:
        browser.close()


@pytest.mark.parametrize("viewport", [v for v in [
    ("desktop_16",  1440,   900,   False),
    ("laptop_13",   1280,   800,   False),
    ("tablet_10",   1024,   768,   False),
    ("mobile_plus",  414,   896,   True),
    ("mobile_std",   375,   812,   True),
    ("mobile_sm",    360,   780,   True),
]], ids=lambda v: v[0])
@pytest.mark.parametrize("url_path", PUBLIC_PAGES)
def test_public_page_renders(
    chromium_page,
    viewport,
    url_path,
    live_server,
    screenshot_dir,
):
    vp_name, w, h, is_mobile = viewport
    context = chromium_page.new_context(
        viewport={"width": w, "height": h},
        device_scale_factor=2 if is_mobile else 1,
        is_mobile=is_mobile,
        has_touch=is_mobile,
    )
    try:
        page = context.new_page()
        response = page.goto(f"{live_server}{url_path}", wait_until="networkidle", timeout=20_000)
        assert response is not None, f"no response from {url_path}"
        assert response.ok, f"{url_path} returned HTTP {response.status}"

        # 1. Screenshot for visual-diff review.
        shot = screenshot_dir / f"{vp_name}_{_safe_path(url_path)}.png"
        page.screenshot(path=str(shot), full_page=True)

        # 2. No horizontal scroll on any viewport (desktop gets enough room;
        #    mobile is the stricter target but the rule is universal).
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 1, (
            f"{url_path} @ {vp_name}: horizontal scroll of {overflow}px"
        )

        # 3. Inputs ≥ 16px on mobile (iOS auto-zoom trigger).
        if is_mobile:
            undersized = page.evaluate("""
                () => Array.from(document.querySelectorAll('input, textarea, select'))
                  .map(el => ({
                    type: el.type || el.tagName,
                    size: parseFloat(getComputedStyle(el).fontSize) || 0,
                    visible: el.offsetParent !== null,
                  }))
                  .filter(r => r.visible && r.size > 0 && r.size < 16)
            """)
            assert not undersized, (
                f"{url_path} @ {vp_name}: inputs smaller than 16px trigger iOS zoom: {undersized[:5]}"
            )

        # 4. Touch targets (min 32px bounding box on mobile).
        if is_mobile:
            tiny_targets = page.evaluate("""
                () => Array.from(document.querySelectorAll('button, a[role=button], [role=button]'))
                  .map(el => {
                    const r = el.getBoundingClientRect();
                    return {tag: el.tagName, text: (el.textContent||'').slice(0, 40), w: r.width, h: r.height};
                  })
                  .filter(t => (t.w > 0 && t.h > 0) && (t.w < 32 || t.h < 32))
            """)
            assert not tiny_targets, (
                f"{url_path} @ {vp_name}: buttons under 32px hit-target: {tiny_targets[:5]}"
            )
    finally:
        context.close()

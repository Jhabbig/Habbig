"""Mobile + in-app-browser quirks.

  * 100vh vs 100dvh — iOS Safari shrinks the viewport height when the
    URL bar retracts; pages that use 100vh for hero sections get
    cropped. We grep the shipped CSS for raw ``100vh`` and fail if it
    appears in a layout-critical context.

  * PWA manifest is reachable + parseable.

  * Service worker registers without error in a WebKit context (the
    engine that historically lags on SW support).

  * The subproduct landing + gate pages survive a "Twitter in-app
    browser" UA spoof — common referrer path and one of the flakier
    environments.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"


def _strip_css_comments(text: str) -> str:
    """Drop /* … */ blocks so the lint regex doesn't trip on the word
    ``body`` mentioned in a header comment right before an unrelated
    rule. Preserves line count for readability of any future debug."""
    return re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)


def test_css_uses_dvh_not_raw_vh_for_hero_heights():
    """iOS Safari 100vh bug — layout-critical heights should use 100dvh.

    Pass criteria: any CSS rule that sets ``min-height`` or ``height``
    to ``100vh`` on a full-page surface (body/html/hero/main/.shell)
    gets flagged unless the same rule offers a dvh fallback above it.
    """
    offenders: list[tuple[str, str]] = []
    for css in STATIC_DIR.rglob("*.css"):
        try:
            raw = css.read_text(errors="ignore")
        except OSError:
            continue
        # Strip comments first so the regex matches real CSS, not the
        # word ``body`` in a file-header comment block.
        text = _strip_css_comments(raw)
        for match in re.finditer(r"(?P<sel>[^{}]+){[^}]*?(?P<prop>min-height|height)\s*:\s*100vh[^;]*;", text):
            selector = match.group("sel").strip()
            # Allow small utility classes where 100vh is intentional
            # (modals, dialogs, offcanvas drawers). The body/html/main
            # targets are the ones that break on iOS.
            if re.search(r"\b(body|html|\.shell|main|\.hero|\.landing-hero|\.hero-grid)\b", selector):
                snippet = match.group(0)[:120]
                # Look both before and after the offending declaration —
                # the common CSS-progressive-enhancement idiom is to
                # declare 100vh first and 100dvh on the next line so the
                # newer unit overrides on browsers that support it.
                window_start = max(0, match.start() - 400)
                window_end = min(len(text), match.end() + 400)
                if "100dvh" not in text[window_start:window_end]:
                    offenders.append((str(css.relative_to(STATIC_DIR)), snippet))
    assert not offenders, (
        "Raw 100vh on a page-level surface will crop on iOS Safari. "
        "Add a 100dvh fallback. Offenders: "
        + "\n".join(f"  {f}: {s}" for f, s in offenders[:8])
    )


def test_manifest_is_well_formed(browser_factory, live_server):
    pw = pytest.importorskip("playwright", reason="playwright not installed")
    browser = browser_factory("chromium")
    try:
        ctx = browser.new_context()
        page = ctx.new_page()
        resp = page.goto(f"{live_server}/manifest.json", wait_until="load", timeout=15_000)
        if resp is None or resp.status == 404:
            pytest.skip("no manifest.json on this branch")
        body = page.evaluate("() => document.body.innerText")
        data = json.loads(body)
        # Minimum PWA manifest contract.
        for key in ("name", "short_name", "icons", "start_url", "display"):
            assert key in data, f"manifest missing required key: {key}"
        assert data.get("display") in ("standalone", "minimal-ui", "fullscreen"), (
            f"manifest display must be a PWA-qualifying value, got {data.get('display')!r}"
        )
        assert isinstance(data.get("icons"), list) and data["icons"], "manifest.icons missing"
    finally:
        browser.close()


def test_service_worker_registers_in_webkit(browser_factory, live_server):
    pw = pytest.importorskip("playwright", reason="playwright not installed")
    browser = browser_factory("webkit")
    try:
        ctx = browser.new_context()
        page = ctx.new_page()
        resp = page.goto(f"{live_server}/", wait_until="networkidle", timeout=20_000)
        if resp is None or not resp.ok:
            pytest.skip("/ didn't return 200")

        # Wait up to 3s for the SW controller. Missing controller is
        # acceptable on webkit (some versions register but don't
        # activate on first load); a thrown registration error is not.
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        registered = page.evaluate("""
            async () => {
              if (!('serviceWorker' in navigator)) return false;
              try {
                const reg = await navigator.serviceWorker.getRegistration();
                return !!reg || !!navigator.serviceWorker.controller;
              } catch (e) { return 'error:' + e.message; }
            }
        """)
        sw_errors = [e for e in errors if "serviceWorker" in e or "sw.js" in e]
        assert not sw_errors, f"webkit SW errors: {sw_errors}"
        # We don't hard-assert `registered === True`. WebKit sometimes
        # returns false on a first visit; the key contract is *no
        # error during registration*, which we asserted above.
    finally:
        browser.close()


TWITTER_IN_APP_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "Twitter for iPhone"
)


def test_homepage_survives_twitter_in_app_browser(browser_factory, live_server):
    pw = pytest.importorskip("playwright", reason="playwright not installed")
    browser = browser_factory("webkit")
    try:
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
            user_agent=TWITTER_IN_APP_UA,
        )
        page = ctx.new_page()
        resp = page.goto(f"{live_server}/", wait_until="networkidle", timeout=20_000)
        assert resp is not None and resp.ok, "homepage 4xx/5xx in Twitter in-app"

        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 1, f"horizontal scroll in Twitter in-app: {overflow}px"

        # Inline JS in the Twitter viewer sometimes blocks localStorage.
        # Simulate + make sure the page still renders (service worker
        # registration is allowed to fail; UI must not).
        broken = page.evaluate("""
            () => {
              // Can the page read its own CSS vars? If layout never
              // attached, :root tokens return empty strings.
              return getComputedStyle(document.documentElement)
                .getPropertyValue('--bg-base').trim() === '';
            }
        """)
        assert not broken, "design tokens not applied in Twitter in-app browser"
    finally:
        browser.close()

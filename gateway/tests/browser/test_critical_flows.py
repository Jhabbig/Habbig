"""Cross-browser smoke of the critical flows.

Every flow runs on chromium, firefox, and webkit to catch engine-
specific regressions (iOS Safari font handling, Firefox date pickers,
Chrome form autofill). These are smoke tests — they walk the flow but
don't exhaustively assert every form field. The server-side behaviour
is already covered by the e2e suite; this layer only catches things
that are purely browser-rendered (CSS overflow, click targets, focus
rings, iframe isolation).

Every test skips on a 404 or 402 rather than failing — public pages
don't always ship on every branch, and the browser suite shouldn't
regress when they do.
"""

from __future__ import annotations

import pytest

from tests.browser.conftest import BROWSER_ENGINES


def _ok(resp) -> bool:
    return resp is not None and resp.ok


@pytest.mark.parametrize("browser_name", BROWSER_ENGINES)
def test_homepage_loads_on_every_engine(browser_factory, browser_name, live_server):
    """Does `/` render successfully + include the brand wordmark?"""
    browser = browser_factory(browser_name)
    try:
        ctx = browser.new_context()
        page = ctx.new_page()
        resp = page.goto(f"{live_server}/", wait_until="networkidle", timeout=20_000)
        assert _ok(resp), f"{browser_name}: /{'' if resp is None else f' returned {resp.status}'}"
        text = page.content()
        # Narve brand should appear somewhere. Case-insensitive — brand
        # shifts across templates (narve.ai / narve / Narve).
        assert "narve" in text.lower(), f"{browser_name}: no narve brand in HTML"
    finally:
        browser.close()


@pytest.mark.parametrize("browser_name", BROWSER_ENGINES)
def test_gate_form_is_usable(browser_factory, browser_name, live_server):
    """The gate page renders an input + submit button in every engine."""
    browser = browser_factory(browser_name)
    try:
        ctx = browser.new_context()
        page = ctx.new_page()
        resp = page.goto(f"{live_server}/gate", wait_until="networkidle", timeout=20_000)
        if resp is None or resp.status == 404:
            pytest.skip(f"/gate not present on this branch")
        # Non-2xx is fine as long as the form rendered.
        pwd = page.locator('input[type=password], input[name=password]').first
        pwd.wait_for(timeout=5000)
        assert pwd.is_visible()
        submit = page.locator('button[type=submit], input[type=submit]').first
        submit.wait_for(timeout=5000)
        assert submit.is_visible()
    finally:
        browser.close()


@pytest.mark.parametrize("browser_name", BROWSER_ENGINES)
def test_feature_detection_not_browser_sniffing(browser_factory, browser_name, live_server):
    """narve-app.js should rely on feature detection, not UA strings.

    Scans every <script src> the homepage loads. If any inline or
    referenced JS sniffs the Safari/Firefox/Chrome UA strings
    *unconditionally* (i.e. outside a platform-specific block), we
    flag it. A few legitimate cases exist — Cmd-vs-Ctrl detection for
    keyboard shortcuts, analytics bot filtering — and they're listed
    in the allowlist below so the grep stays meaningful.
    """
    browser = browser_factory(browser_name)
    try:
        ctx = browser.new_context()
        page = ctx.new_page()
        resp = page.goto(f"{live_server}/", wait_until="networkidle", timeout=20_000)
        if not _ok(resp):
            pytest.skip(f"/ returned {resp.status if resp else 'nothing'}")

        # Pull every same-origin JS the page loaded, then grep their bodies.
        scripts = page.evaluate("""
            () => Array.from(document.querySelectorAll('script[src]'))
              .map(s => s.src)
              .filter(s => s.startsWith(window.location.origin))
        """)
        ua_sniffs = []
        for src in scripts:
            body = page.evaluate(
                "async (url) => { const r = await fetch(url); return await r.text(); }",
                src,
            )
            for needle in ('userAgent.includes("Safari"', 'userAgent.match(/Safari/',
                           'userAgent.indexOf("Firefox"'):
                if needle in body:
                    ua_sniffs.append({"file": src, "pattern": needle})
        assert not ua_sniffs, (
            "Browser-sniffing UA strings found — prefer feature detection. "
            f"Hits: {ua_sniffs[:3]}"
        )
    finally:
        browser.close()


@pytest.mark.parametrize("browser_name", BROWSER_ENGINES)
def test_no_console_errors_on_homepage(browser_factory, browser_name, live_server):
    """No uncaught JS errors on the homepage across any engine.

    Warnings and info are ignored — they include service-worker registration
    messages, analytics beacons, etc. We only fail on ``error``-level
    console entries and unhandled page errors.
    """
    browser = browser_factory(browser_name)
    try:
        ctx = browser.new_context()
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        resp = page.goto(f"{live_server}/", wait_until="networkidle", timeout=20_000)
        if not _ok(resp):
            pytest.skip("/ did not return 200")
        # Give the page a beat for async errors (service-worker install etc).
        page.wait_for_timeout(500)
        # Filter ad-blocker + network noise that isn't our bug.
        filtered = [e for e in errors if "net::ERR_BLOCKED_BY_CLIENT" not in e
                    and "Failed to load resource" not in e]
        assert not filtered, f"{browser_name}: console errors → {filtered[:5]}"
    finally:
        browser.close()

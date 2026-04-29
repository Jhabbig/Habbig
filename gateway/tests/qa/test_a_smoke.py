"""Walk A — boot smoke (browser-driven companion to qa_walk_a_smoke.py).

The TestClient version asserts headers + log silence. This version goes
through a real browser to confirm the response also makes it through
gzip/middleware/CSP without breaking before paint. Useful for catching
regressions that only surface when the response is actually rendered
(e.g. CSP blocking inline theme-init).
"""

from __future__ import annotations

import pytest

# Skip the whole module cleanly when Playwright isn't installed — the
# TestClient walk in qa_walk_a_smoke.py covers the same ground.
pytest.importorskip("playwright.sync_api")


def test_browser_loads_homepage(page, browser_server):
    """Headless Chromium can fetch / and the document title is set."""
    response = page.goto(f"{browser_server}/", wait_until="networkidle")
    assert response is not None
    assert response.status == 200, f"/ returned {response.status}"
    assert page.title(), "homepage has empty <title>"


def test_response_time_header_visible_to_browser(page, browser_server):
    """X-Response-Time-ms must reach the browser, not be stripped by
    middleware or by Cloudflare's static-asset rewriter."""
    response = page.goto(f"{browser_server}/health", wait_until="domcontentloaded")
    assert response is not None
    headers = {k.lower(): v for k, v in response.headers.items()}
    assert "x-response-time-ms" in headers, (
        f"X-Response-Time-ms header missing; got: {sorted(headers.keys())}"
    )
    rt_ms = int(headers["x-response-time-ms"])
    # Generous bound — CI is slow. Ops dashboards alert on 500 ms; we
    # only catch a runaway like 5 s here.
    assert rt_ms < 5000, f"runaway response time: {rt_ms} ms"


def test_no_console_errors_on_homepage(page, browser_server):
    """Loading / shouldn't throw a JS exception or a console.error.

    The page-level error capture in conftest collects them; we read
    after a settle delay so async error paths land in the list.
    """
    page.goto(f"{browser_server}/", wait_until="networkidle")
    # Allow any deferred analytics / theme JS to fire.
    page.wait_for_timeout(500)
    errors = getattr(page, "_nv_errors", [])
    # Drop benign signatures — the existing app yields a "browser
    # extension" error from share-button.js auto-rescan when no
    # share buttons are on the page; that's harmless and out of scope
    # for the smoke walk.
    significant = [
        e for e in errors
        if "rescan-share-buttons" not in e
        and "404" not in e          # missing-asset noise tracked by walk H
        and "favicon" not in e
    ]
    assert not significant, f"console errors on /: {significant[:5]}"

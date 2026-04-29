"""Walk F — empty / loading / error states render real UX.

The foundation bundle introduced .nv-empty, narveSkel, and the toast
region. These tests ensure those primitives are actually present at
runtime on the pages users see, not just defined in CSS.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")


def test_toast_region_present_on_authed_pages(authed_browser_page, browser_server):
    """The base template's #nv-toast-region must mount on every authed
    page so window.narveToast(...) has somewhere to inject."""
    authed_browser_page.goto(
        f"{browser_server}/dashboards", wait_until="domcontentloaded"
    )
    region = authed_browser_page.locator("#nv-toast-region")
    assert region.count() == 1, (
        "#nv-toast-region missing — toast() calls will silently no-op"
    )


def test_toast_can_be_invoked_from_console(authed_browser_page, browser_server):
    """Drive narveToast directly to confirm the JS surface is wired,
    not just the region."""
    authed_browser_page.goto(
        f"{browser_server}/dashboards", wait_until="networkidle"
    )
    # Wait briefly for deferred toast.js to load.
    authed_browser_page.wait_for_function(
        "typeof window.narveToast === 'function'", timeout=5000,
    )
    authed_browser_page.evaluate(
        "window.narveToast('e2e test toast', { duration: 5000 })"
    )
    toast = authed_browser_page.locator("[data-testid='nv-toast']").first
    toast.wait_for(state="visible", timeout=2000)
    assert "e2e test toast" in (toast.inner_text() or "")


def test_saved_page_renders_either_content_or_empty_state(
    authed_browser_page, browser_server,
):
    """A page that lists user-specific data must show either real
    content OR the .nv-empty partial — never a bare 'Loading…' that
    never resolves."""
    authed_browser_page.goto(
        f"{browser_server}/saved", wait_until="networkidle"
    )
    # Allow async fetches to settle.
    authed_browser_page.wait_for_timeout(750)
    has_empty = authed_browser_page.locator(".nv-empty, [data-testid='nv-empty']").count()
    has_table = authed_browser_page.locator("table, .saved-list, .prediction-card").count()
    assert (has_empty + has_table) > 0, (
        "/saved: neither empty state nor content rendered "
        "(a stuck 'Loading…' is a UX regression)"
    )


def test_dashboards_page_no_stuck_loading(authed_browser_page, browser_server):
    """After the network is idle, no element should still read the
    bare placeholder string 'Loading…' — that means a fetch failed
    silently and the skeleton never swapped."""
    authed_browser_page.goto(
        f"{browser_server}/dashboards", wait_until="networkidle"
    )
    authed_browser_page.wait_for_timeout(1500)
    body_text = authed_browser_page.locator("body").inner_text()
    # Allow at most one occurrence — sometimes a deferred lazy-loaded
    # widget is intentionally still "Loading…" when the page is idle
    # (e.g. an off-screen feed). Two or more is a regression.
    n_stuck = body_text.count("Loading…")
    assert n_stuck < 2, f"/dashboards: 'Loading…' appears {n_stuck}x after settle"

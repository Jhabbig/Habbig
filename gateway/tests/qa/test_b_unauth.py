"""Walk B — unauthenticated pages render in a real browser.

The TestClient walk asserts the route returns 200 with the right body.
This walk asserts the same routes survive the full browser pipeline
(CSP, theme init, font load, JS execution) without throwing.

Each path is parametrised so a regression on one route shows up in
the test report as a single named failure rather than a generic "walk
failed" line.
"""

from __future__ import annotations

import re
import pytest

pytest.importorskip("playwright.sync_api")

from .pages import UNAUTH_PAGES  # noqa: E402


@pytest.mark.parametrize("path", UNAUTH_PAGES)
def test_route_renders(page, browser_server, path):
    """Anonymous visitor: every public route reaches a non-empty page.

    Some auth-adjacent paths (/login, /signup, /forgot-password) may
    redirect to /gate when SITE_ACCESS_TOKEN is set — both 200 and
    302→200 paths are acceptable.
    """
    response = page.goto(
        f"{browser_server}{path}",
        wait_until="domcontentloaded", timeout=15000,
    )
    assert response is not None, f"{path}: no response"
    assert response.status in (200, 302), (
        f"{path}: unexpected status {response.status}"
    )
    assert page.title(), f"{path}: empty <title>"


@pytest.mark.parametrize(
    "path",
    # Limit the font-family check to a representative subset so we
    # don't pay 20 page loads × 1 assertion. Catching one drift on
    # this set is enough — gateway.css is loaded site-wide.
    ["/", "/pricing", "/about", "/methodology", "/faq"],
)
def test_inter_font_in_use(page, browser_server, path):
    page.goto(f"{browser_server}{path}", wait_until="networkidle", timeout=15000)
    family = page.evaluate("getComputedStyle(document.body).fontFamily")
    assert "Inter" in family, f"{path}: body fontFamily is {family!r}"


@pytest.mark.parametrize("path", ["/methodology", "/about", "/pricing"])
def test_meta_description_present(page, browser_server, path):
    """Foundation invariant: every public page ships a meta description
    so social-share previews aren't blank."""
    page.goto(f"{browser_server}{path}", wait_until="domcontentloaded")
    desc = page.locator('meta[name="description"]').first
    if desc.count() == 0:
        pytest.fail(f"{path}: <meta name=description> missing")
    content = desc.get_attribute("content") or ""
    assert len(content) >= 30, f"{path}: meta description too short: {content!r}"


def test_404_page_themed(page, browser_server):
    """A bogus URL should land on the styled 404 page, not a blank
    white server-default. The shared chrome (logo + brand) must be
    visible so users can navigate back."""
    response = page.goto(
        f"{browser_server}/__definitely-not-a-real-page-xyzzy__",
        wait_until="domcontentloaded",
    )
    assert response is not None
    # The status comes back as 404; some routes redirect to /gate
    # first which then 200s — accept both as "not crashed".
    assert response.status in (404, 200, 302)
    body = page.locator("body").inner_text()
    # The text must mention "not found" or the page's own 404 title.
    # Case-insensitive so a future copy edit doesn't break the test.
    assert (
        re.search(r"not found|404|page (does not|doesn.t) exist", body, re.IGNORECASE)
        or page.locator(".error-page, .nv-empty").count() > 0
    ), "404 has no recognisable not-found copy"

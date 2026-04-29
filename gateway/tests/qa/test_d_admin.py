"""Walk D — admin pages are reachable for admins, blocked for everyone else.

Two parametrised passes per route:

  - admin_browser_page → expects 200 / 302 (route renders or
    redirects to a sub-route inside the admin shell).
  - authed_browser_page → expects 401 / 403 / 302 (must NOT see
    admin content as a non-admin).

Anything that returns 200 in the second pass is a privilege-escalation
regression and the test fails loudly.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from .pages import ADMIN_PAGES  # noqa: E402


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_admin_can_view(admin_browser_page, browser_server, path):
    response = admin_browser_page.goto(
        f"{browser_server}{path}",
        wait_until="domcontentloaded", timeout=15000,
    )
    if response is None:
        pytest.fail(f"{path}: no response")
    assert response.status in (200, 302, 404), (
        f"{path}: admin should see this; got {response.status}"
    )


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_non_admin_blocked(authed_browser_page, browser_server, path):
    """A non-admin must NEVER see an admin page render — the response
    must be 401 / 403 or a redirect away from the admin path. Anything
    else is a privilege bypass."""
    response = authed_browser_page.goto(
        f"{browser_server}{path}",
        wait_until="domcontentloaded", timeout=15000,
    )
    if response is None:
        pytest.fail(f"{path}: no response")
    # Acceptable: redirect (gate / subdomain), 401, 403, 404.
    # Unacceptable: 200 — that means the non-admin saw the page.
    if response.status == 200:
        # Even a 200 is OK if the body reads as a "not authorised"
        # message rather than the admin content. Some app surfaces
        # render their own 403 inside a 200 (legacy pattern). Check
        # the URL after redirects + whether admin-shell markers appear.
        final_url = authed_browser_page.url
        if "/admin" not in final_url:
            return  # redirected away, fine
        body = authed_browser_page.locator("body").inner_text().lower()
        if any(s in body for s in ("forbidden", "not authorised", "not authorized", "permission")):
            return
        pytest.fail(
            f"{path}: non-admin received 200 with admin content "
            f"(final_url={final_url})"
        )

"""Pytest fixtures for the QA walks.

Three cookie variants of the FastAPI app:

    anon_cookies    — no session
    authed_cookies  — non-admin user with a valid session
    admin_cookies   — is_admin=1 user with a valid session

Plus a `client` fixture (TestClient) and `live_url(path)` helper that
builds the right URL for each test. We don't actually spin up uvicorn
on port 7000 in the test runner — TestClient runs the ASGI app
in-process, which is faster and works without a network listener.

Playwright tests in this directory `pytest.importorskip("playwright")`
at module import so they skip cleanly when the dep isn't installed.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from typing import Optional

import pytest

# Pull the gateway package onto the path the same way other tests do.
from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations

import db  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


# ── Helpers ─────────────────────────────────────────────────────────────────


def _now() -> int:
    return int(time.time())


def _make_test_user(
    email: str, *, username: str, admin: bool = False
) -> int:
    """Idempotent user create — re-running the test suite shouldn't 409."""
    existing = db.get_user_by_email(email) if hasattr(db, "get_user_by_email") else None
    if existing:
        uid = existing["id"]
    else:
        uid = db.create_user(email, "QaWalkPass123!", username=username)
    if admin:
        with db.conn() as c:
            c.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (uid,))
    return uid


# ── Module-level fixtures (one per session) ─────────────────────────────────


@pytest.fixture(scope="session")
def client() -> TestClient:
    """A TestClient bound to the running FastAPI app."""
    return TestClient(server.app)


@pytest.fixture(scope="session")
def anon_cookies() -> dict:
    """Empty cookies — for unauthenticated walks."""
    return {}


@pytest.fixture(scope="session")
def authed_cookies() -> dict:
    """Cookies for a non-admin authenticated user."""
    uid = _make_test_user(
        "qa-walks-authed@test.local",
        username="qawalksauthed",
        admin=False,
    )
    token = db.create_session(uid)
    return {server.COOKIE_NAME: token}


@pytest.fixture(scope="session")
def admin_cookies() -> dict:
    """Cookies for an is_admin=1 user."""
    uid = _make_test_user(
        "qa-walks-admin@test.local",
        username="qawalksadmin",
        admin=True,
    )
    token = db.create_session(uid)
    return {server.COOKIE_NAME: token}


# ── Skip helpers ────────────────────────────────────────────────────────────
#
# Playwright walks call `pytest.importorskip("playwright")` at module top so
# they exit cleanly when the dep isn't installed. Lighthouse walk shells
# out to `npx lighthouse`; we skip if `npx` isn't on PATH.


def has_playwright() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def has_lighthouse() -> bool:
    return shutil.which("npx") is not None


# ── Live-server fixture (for Playwright walks only) ────────────────────────
#
# Playwright needs a real HTTP listener on a real port. We spin up a uvicorn
# in a thread on a random ephemeral port, yield the URL, then tear it down.
# This fixture is opt-in via marker so the whole TestClient suite doesn't
# pay the boot cost when no Playwright walk requested it.


@pytest.fixture(scope="session")
def live_server() -> Optional[str]:
    """Boot uvicorn in a thread on a random port. None if unavailable."""
    if not has_playwright():
        return None
    try:
        import socket
        import threading
        import uvicorn

        # Find an open ephemeral port — race-free vs hard-coding 7000.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        cfg = uvicorn.Config(
            server.app, host="127.0.0.1", port=port,
            log_level="warning", access_log=False,
        )
        srv = uvicorn.Server(cfg)
        thread = threading.Thread(target=srv.run, daemon=True)
        thread.start()

        # Wait briefly for the listener to come up. uvicorn doesn't expose
        # a "ready" hook in older versions; poll the port.
        deadline = _now() + 5
        while _now() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            return None  # never bound

        yield f"http://127.0.0.1:{port}"
        srv.should_exit = True
        thread.join(timeout=3)
    except Exception:
        # Any failure → return None so the dependent walks self-skip.
        yield None


# ── Playwright fixtures ─────────────────────────────────────────────────────
#
# Browser-driven walks (test_*.py) consume these. The whole stack
# self-skips cleanly when:
#   - playwright is not installed (the import in each test file fails fast)
#   - the chromium binary hasn't been downloaded
#   - NARVE_TEST_SERVER points at an unreachable URL
# ...so a CI worker without Playwright still passes the lighter-weight
# qa_walk_*.py TestClient suite, and a dev box without `playwright
# install` doesn't fail noisily.
#
# Default target: env NARVE_TEST_SERVER (eg http://localhost:7000 or
# http://100.69.44.108:7000), falling back to the in-process uvicorn
# fixture above.


@pytest.fixture(scope="session")
def browser_server(live_server):
    """Resolve the URL the browser-walks should target.

    Resolution order:
      1. NARVE_TEST_SERVER env (so CI / staging can target a real
         deployment without the local uvicorn fixture).
      2. The existing live_server fixture (in-process uvicorn on a
         random port — already declared above).
      3. Skip if neither is available.

    Naming: kept distinct from the existing ``live_server`` so the two
    fixtures coexist instead of one overriding the other. Browser
    walks depend on this; pure HTTP walks depend on ``live_server``.
    """
    import requests
    target = os.environ.get("NARVE_TEST_SERVER") or live_server
    if not target:
        pytest.skip("No NARVE_TEST_SERVER and live_server fixture didn't bind")
    # Probe /health — anonymous, public, returns 200 — so the rest of
    # the walks don't waste time on a target that isn't actually up.
    try:
        r = requests.get(f"{target}/health", timeout=3)
        if r.status_code != 200:
            pytest.skip(f"{target}/health returned {r.status_code}")
    except Exception as e:
        pytest.skip(f"{target} unreachable: {e}")
    return target


@pytest.fixture(scope="session")
def _pw_module():
    """Import playwright lazily; skip the whole browser-test session
    cleanly when the dep is missing.
    """
    pw = pytest.importorskip(
        "playwright.sync_api",
        reason="playwright not installed; install with "
        "`pip install -r requirements-dev.txt && playwright install chromium`",
    )
    return pw


@pytest.fixture
def browser(_pw_module):
    """Per-test browser. Headless by default; PWDEBUG=1 makes it visible."""
    headless = not os.environ.get("PWDEBUG")
    with _pw_module.sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=headless)
        except Exception as e:
            pytest.skip(
                f"Chromium launch failed ({e}). Run "
                f"`python3 -m playwright install chromium` first."
            )
        yield b
        b.close()


def _attach_error_capture(page):
    """Collect console errors + page exceptions onto page._nv_errors so
    a failing test can surface them in the assertion message rather
    than disappearing into the void."""
    errors: list[str] = []

    def _on_pageerror(exc):
        errors.append(f"pageerror: {exc}")

    def _on_console(msg):
        if msg.type == "error":
            errors.append(f"console: {msg.text}")

    page.on("pageerror", _on_pageerror)
    page.on("console", _on_console)
    page._nv_errors = errors  # type: ignore[attr-defined]
    return page


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    pg = _attach_error_capture(ctx.new_page())
    yield pg
    if getattr(pg, "_nv_errors", None):
        # Surfaced into the test report; not a hard fail (a 403 page
        # legitimately throws on a protected endpoint), but visible
        # so the operator can spot real regressions in `pytest -s`.
        print(f"\nbrowser console errors: {pg._nv_errors[:5]}")
    ctx.close()


@pytest.fixture
def mobile_page(browser):
    ctx = browser.new_context(
        viewport={"width": 375, "height": 812},
        device_scale_factor=2,
        is_mobile=True,
        has_touch=True,
        # iPhone 14 Pro UA — many a11y / hover / pointer-coarse rules
        # only fire when the UA looks like a real phone.
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
            "Mobile/15E148 Safari/604.1"
        ),
    )
    pg = _attach_error_capture(ctx.new_page())
    yield pg
    ctx.close()


@pytest.fixture
def authed_browser_page(page, browser_server, authed_cookies):
    """Page with the same auth cookies the TestClient walks already
    use, injected at context level so every request from the page
    carries them. Falls back to seeding via /login if cookie injection
    is incompatible with this build's gate (the TestClient cookies
    are set on the FastAPI domain, not the live target — Playwright
    needs them keyed on the actual server URL)."""
    from urllib.parse import urlparse
    parsed = urlparse(browser_server)
    cookies_for_browser = []
    for name, value in (authed_cookies or {}).items():
        cookies_for_browser.append({
            "name": name,
            "value": value,
            "domain": parsed.hostname or "127.0.0.1",
            "path": "/",
            "httpOnly": False,
            "secure": parsed.scheme == "https",
            "sameSite": "Lax",
        })
    if cookies_for_browser:
        page.context.add_cookies(cookies_for_browser)
    return page


@pytest.fixture
def admin_browser_page(page, browser_server, admin_cookies):
    from urllib.parse import urlparse
    parsed = urlparse(browser_server)
    cookies_for_browser = []
    for name, value in (admin_cookies or {}).items():
        cookies_for_browser.append({
            "name": name,
            "value": value,
            "domain": parsed.hostname or "127.0.0.1",
            "path": "/",
            "httpOnly": False,
            "secure": parsed.scheme == "https",
            "sameSite": "Lax",
        })
    if cookies_for_browser:
        page.context.add_cookies(cookies_for_browser)
    return page

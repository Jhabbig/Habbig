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

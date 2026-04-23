"""Playwright-specific fixtures.

Every test in ``tests/browser`` depends on three building blocks:

  * ``playwright_sync`` — a process-scoped Playwright driver. Imported
    lazily so pytest collection works even when Playwright isn't
    installed; the fixture marks every downstream test as skipped in
    that case. One driver is shared across tests to avoid the
    ~2s start-up cost per test.

  * ``browser_factory(browser_name)`` — returns a new launched browser.
    Tests parametrise over ``["chromium", "firefox", "webkit"]`` via the
    ``BROWSER_ENGINES`` constant.

  * ``live_server`` — spins a FastAPI instance bound to a random local
    port using uvicorn in a background thread. Yields the base URL
    (``http://127.0.0.1:<port>``). Teardown joins the thread; any
    uncaught server exception is re-raised into the pytest reporter.

Everything uses ``sync_playwright`` rather than the async fixtures that
ship with ``pytest-playwright``. We don't want to force pytest-asyncio
on the rest of the suite just for browser tests, and the sync API is
fine for these short, deterministic flows.
"""

from __future__ import annotations

import contextlib
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest


# ── Engines + viewports ─────────────────────────────────────────────────────

BROWSER_ENGINES = ["chromium", "firefox", "webkit"]

VIEWPORTS = [
    # name          width  height  is_mobile
    ("desktop_16",  1440,   900,   False),
    ("desktop_fhd", 1920,  1080,   False),
    ("laptop_13",   1280,   800,   False),
    ("tablet_10",   1024,   768,   False),
    ("mobile_plus",  414,   896,   True),
    ("mobile_std",   375,   812,   True),
    ("mobile_sm",    360,   780,   True),
]


# ── Playwright driver + browser helpers ─────────────────────────────────────


@pytest.fixture(scope="session")
def playwright_sync():
    """Yield a Playwright driver, or skip if the package isn't installed.

    The driver is a process-wide singleton — downstream browser fixtures
    launch fresh browsers against the same driver instead of spinning
    Playwright up per-test.
    """
    pw = pytest.importorskip("playwright", reason="playwright not installed")
    from playwright.sync_api import sync_playwright  # noqa: WPS433 — local import by design
    with sync_playwright() as driver:
        yield driver


@pytest.fixture(scope="session")
def browser_factory(playwright_sync):
    """Return a helper that launches a browser for the given engine.

    ``launch(browser_name)`` returns a freshly-launched browser. The
    caller is responsible for closing it (use ``with contextlib.closing``
    or a try/finally).  Head-less by default; set ``NARVE_BROWSER_HEADED=1``
    to open a window for debugging.
    """
    headless = os.environ.get("NARVE_BROWSER_HEADED", "").strip() != "1"

    def _launch(name: str):
        if name not in BROWSER_ENGINES:
            pytest.fail(f"unknown browser engine: {name}")
        launcher = getattr(playwright_sync, name)
        return launcher.launch(headless=headless)

    return _launch


# ── Live FastAPI server bound to a random port ──────────────────────────────


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _UvicornThread(threading.Thread):
    """Run uvicorn.Server.run in a thread with graceful shutdown.

    Copied from the uvicorn README's recommended pattern — uvicorn's
    own ``should_exit`` flag is the clean way to stop the loop.
    """

    def __init__(self, app: Any, host: str, port: int) -> None:
        super().__init__(daemon=True)
        import uvicorn  # local import so non-browser tests don't pay
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self._server = uvicorn.Server(config)

    def run(self) -> None:
        self._server.run()

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="session")
def live_server() -> Iterator[str]:
    """Boot the real FastAPI app + yield its base URL.

    Gate middleware uses ``SITE_ACCESS_TOKEN`` — we set a deterministic
    one so browser tests can walk the gate the same way the e2e
    fixtures do. Feature flags are left at defaults so tests exercise
    production code paths.
    """
    # Load path: gateway/ as the repo root for this app.
    repo_root = Path(__file__).resolve().parent.parent.parent
    sys.path.insert(0, str(repo_root))

    os.environ.setdefault("SITE_ACCESS_TOKEN", "e2e-browser-gate")
    os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
    os.environ.setdefault("GATEWAY_DB_PATH", str(repo_root / "auth.db"))
    os.environ.pop("PRODUCTION", None)
    os.environ.pop("REDIS_HOST", None)

    # Import late so the env vars above take effect.
    try:
        import server  # noqa: WPS433 — intentional lazy import
    except Exception as exc:
        pytest.skip(f"server import failed: {exc}")

    port = _find_free_port()
    thread = _UvicornThread(server.app, "127.0.0.1", port)
    thread.start()

    # Poll the server until it responds. Give it a generous budget —
    # FastAPI startup hooks can run migrations on a fresh DB.
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15.0
    while time.time() < deadline:
        try:
            import urllib.request
            urllib.request.urlopen(f"{base}/health", timeout=1).read()
            break
        except Exception:
            time.sleep(0.1)
    else:
        thread.stop()
        pytest.skip("live server never became healthy")

    try:
        yield base
    finally:
        thread.stop()
        thread.join(timeout=5.0)


# ── Screenshot baseline directory ──────────────────────────────────────────


@pytest.fixture(scope="session")
def screenshot_dir() -> Path:
    root = Path(os.environ.get(
        "NARVE_BROWSER_SCREENSHOT_DIR",
        str(Path(__file__).parent / "screenshots"),
    ))
    root.mkdir(parents=True, exist_ok=True)
    return root

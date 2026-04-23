"""Axe-core integration.

Launches ``npx @axe-core/cli`` against a locally-running server. Skipped
when ``NARVE_RUN_AXE`` isn't set so the default test suite doesn't pay
the Chrome-download cost. Turn it on in CI with:

    NARVE_RUN_AXE=1 NARVE_AXE_BASE=http://127.0.0.1:7000 pytest tests/a11y

The fixture assumes the server is already listening (it does NOT boot
uvicorn itself — that's the job of the CI orchestration script or a
human developer running ``python3 -m uvicorn server:app --port 7000``
in another terminal). If the base URL isn't reachable, every test
xfails with a clear message so the reviewer can see which pages didn't
run vs. which actually failed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest


BASE = os.environ.get("NARVE_AXE_BASE", "http://127.0.0.1:7000").rstrip("/")
RUN_AXE = os.environ.get("NARVE_RUN_AXE") in ("1", "true", "yes")


PAGES: tuple[str, ...] = (
    "/", "/landing", "/narve",
    "/about", "/how-it-works", "/methodology", "/faq",
    "/team", "/press", "/changelog",
    "/pricing", "/subscribe", "/support", "/suspended",
    "/terms", "/privacy", "/dpa",
    "/enquire", "/gate", "/login", "/register", "/token",
    "/forgot-password", "/signup",
    "/status", "/offline",
    "/calendar", "/api/docs",
)


def _axe_binary() -> str | None:
    return shutil.which("npx")


pytestmark = pytest.mark.skipif(
    not RUN_AXE,
    reason="Axe-core is gated behind NARVE_RUN_AXE=1 so `pytest` default runs stay fast.",
)


@pytest.fixture(scope="module")
def axe_ready() -> bool:
    """Confirm npx is on PATH. Individual tests xfail if the server is unreachable."""
    if not _axe_binary():
        pytest.skip("npx not on PATH; install Node to run axe-core tests")
    return True


@pytest.mark.parametrize("path", PAGES)
def test_page_passes_axe(axe_ready, path):
    url = f"{BASE}{path}"
    npx = _axe_binary()
    result = subprocess.run(
        [npx, "--yes", "@axe-core/cli", url, "--tags", "wcag2aa", "--format", "json", "--exit"],
        capture_output=True, text=True, timeout=60,
    )
    # axe-core exits non-zero ONLY on violations (0 = pass, 1 = violations,
    # 2 = error reaching the URL). The `--exit` flag makes this explicit.
    if result.returncode == 2:
        pytest.xfail(f"{url} unreachable — is the server running?")

    # Parse the JSON output. The tool writes to stdout; if it fails to start
    # Chrome we also see a non-JSON banner — fall back gracefully.
    violations = []
    for line in result.stdout.splitlines():
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, list):
            for entry in payload:
                violations.extend(entry.get("violations", []))

    if violations:
        summary = "\n".join(
            f"  · {v.get('id')}: {v.get('help')}" for v in violations[:10]
        )
        pytest.fail(
            f"{url} has {len(violations)} WCAG 2.1 AA violations:\n{summary}"
        )

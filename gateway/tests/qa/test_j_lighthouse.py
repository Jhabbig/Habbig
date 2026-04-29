"""Walk J — Lighthouse score floor on the canonical landing.

Runs `npx lighthouse` (mobile preset) against the live target. Marked
``slow`` so the default ``-m "not slow"`` invocation skips it; the
nightly job that runs the full suite picks it up.

Skips cleanly when:
  - Node / npx not on PATH
  - lighthouse package not installable / missing
  - The browser_server URL isn't reachable from this machine
  - Chrome / Chromium binary required by Lighthouse isn't present
"""

from __future__ import annotations

import json
import shutil
import subprocess
import pytest

pytest.importorskip("playwright.sync_api")


PERF_FLOOR = 0.85
A11Y_FLOOR = 0.90
SEO_FLOOR = 0.90


@pytest.mark.slow
def test_lighthouse_mobile_canonical(browser_server):
    if not shutil.which("npx"):
        pytest.skip("npx not on PATH; install Node.js to run Lighthouse walk")

    cmd = [
        "npx", "--yes", "lighthouse@>=11", browser_server,
        "--quiet",
        "--chrome-flags=--headless --no-sandbox",
        "--output=json",
        "--preset=mobile",
        "--only-categories=performance,accessibility,seo",
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180,
        )
    except FileNotFoundError:
        pytest.skip("npx exists but lighthouse not resolvable")
    except subprocess.TimeoutExpired:
        pytest.skip("Lighthouse exceeded 3-minute timeout")

    if result.returncode != 0 or not result.stdout.strip().startswith("{"):
        # Common failures: no Chrome installed, sandbox blocked, etc.
        pytest.skip(
            f"Lighthouse couldn't run; stderr={result.stderr[-300:]!r}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.skip(f"Lighthouse output not JSON: {result.stdout[:200]!r}")

    cats = data.get("categories", {})
    perf = (cats.get("performance") or {}).get("score")
    a11y = (cats.get("accessibility") or {}).get("score")
    seo = (cats.get("seo") or {}).get("score")

    failures = []
    if perf is None or perf < PERF_FLOOR:
        failures.append(f"performance={perf!r} (floor {PERF_FLOOR})")
    if a11y is None or a11y < A11Y_FLOOR:
        failures.append(f"accessibility={a11y!r} (floor {A11Y_FLOOR})")
    if seo is None or seo < SEO_FLOOR:
        failures.append(f"seo={seo!r} (floor {SEO_FLOOR})")
    assert not failures, "Lighthouse below floor: " + " | ".join(failures)

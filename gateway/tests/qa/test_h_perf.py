"""Walk H — perf at the network level.

These run through a real browser so we measure what users measure:

  - Total transfer size on the homepage stays under a budget. A
    regression here usually means someone re-introduced a 200 KB
    image or stopped subsetting the font.
  - X-Response-Time-ms makes it through (already covered by walk A,
    repeated at /pricing here as a smoke for the second-most-loaded
    public route).
  - Text responses come back gzipped when the client offers it — both
    GZipMiddleware itself works AND the route doesn't accidentally
    bypass it via a custom Response that drops the encoding.
"""

from __future__ import annotations

import pytest
import requests

pytest.importorskip("playwright.sync_api")


# Soft cap at 1.5 MB — well above the actual budget but catches the
# obvious regressions (10 MB hero image, etc.). Tighter budgets live
# in PERFORMANCE_BASELINE.md for the operator to track week-over-week.
HOME_TRANSFER_BUDGET_BYTES = 1_500_000


def test_homepage_transfer_under_budget(page, browser_server):
    """Total bytes received from / + every sub-resource under the cap."""
    total = {"bytes": 0, "by_type": {}}

    def _on_response(response):
        try:
            ct = (response.headers or {}).get("content-type", "?").split(";")[0]
            body = response.body() or b""
            total["bytes"] += len(body)
            total["by_type"][ct] = total["by_type"].get(ct, 0) + len(body)
        except Exception:
            # response.body() can fail for some redirected requests —
            # skip rather than fail the whole walk.
            pass

    page.on("response", _on_response)
    page.goto(f"{browser_server}/", wait_until="networkidle", timeout=20000)
    bytes_received = total["bytes"]
    assert bytes_received < HOME_TRANSFER_BUDGET_BYTES, (
        f"homepage transfer {bytes_received:,} bytes > budget "
        f"{HOME_TRANSFER_BUDGET_BYTES:,}. "
        f"Top types: {sorted(total['by_type'].items(), key=lambda x: -x[1])[:5]}"
    )


def test_response_time_header_set_on_pricing(browser_server):
    r = requests.get(f"{browser_server}/pricing", timeout=8)
    headers = {k.lower(): v for k, v in r.headers.items()}
    assert "x-response-time-ms" in headers, (
        f"X-Response-Time-ms missing on /pricing; headers: {sorted(headers)}"
    )


def test_gzip_on_html_responses(browser_server):
    """A reasonably-sized text response (>1 KB) must be gzipped when
    the client says it accepts gzip. GZipMiddleware threshold is
    1 KB; below that the bytes wouldn't be encoded."""
    r = requests.get(
        f"{browser_server}/methodology",
        headers={"Accept-Encoding": "gzip"},
        timeout=8,
    )
    assert r.status_code == 200
    # /methodology is a long page (>>1 KB) so encoding should kick in.
    enc = r.headers.get("content-encoding", "")
    assert enc == "gzip", (
        f"/methodology not gzipped (got content-encoding: {enc!r})"
    )

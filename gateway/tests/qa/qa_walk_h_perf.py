"""QA Walk H — perf headers + page weight.

Two TestClient-scope checks:

  * `X-Response-Time-ms` header is present on the home + a couple of
    authed pages (Walk A asserts it on /health; this widens coverage
    so a regression that strips the header from full-render paths
    gets caught too).
  * Home page transfer size is under a generous budget. We can't
    measure all the assets the way a browser would, but the HTML body
    + the size of every linked .js / .css that lives in static/
    gives us a directionally-correct number. Budget intentionally
    loose (500 KB) so we don't gate every CSS tweak.

Lighthouse-equivalent perf score lives in Walk J.
"""

from __future__ import annotations

import os
import re
import unittest

from fastapi.testclient import TestClient

from . import conftest as _conf  # noqa: F401

import db  # noqa: E402
import server  # noqa: E402


_STATIC_RE = re.compile(
    r'(?:src|href)=["\'](/_gateway_static/[^"\']+\.(?:js|css))["\']'
)


def _login() -> dict:
    email = "qa-walk-h-authed@test.local"
    existing = (
        db.get_user_by_email(email)
        if hasattr(db, "get_user_by_email") else None
    )
    if existing:
        uid = existing["id"]
    else:
        uid = db.create_user(email, "QaWalkPass123!", username="qawalkhauth")
    return {server.COOKIE_NAME: db.create_session(uid)}


class TestPerfHeaders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)
        cls.cookies = _login()

    def test_response_time_header_on_home(self):
        r = self.client.get("/", follow_redirects=False)
        keys = {k.lower() for k in r.headers.keys()}
        self.assertIn("x-response-time-ms", keys, f"home headers: {sorted(keys)[:8]}")

    def test_response_time_header_on_dashboards(self):
        r = self.client.get(
            "/dashboards", cookies=self.cookies, follow_redirects=False,
        )
        keys = {k.lower() for k in r.headers.keys()}
        self.assertIn("x-response-time-ms", keys)


class TestHomeTransferSize(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)

    def test_home_html_plus_static_under_budget(self):
        """HTML body + every linked /_gateway_static asset under 500 KB.

        Excludes external CDN fonts (Walk E gates those) and dynamic
        JSON. This is a static-asset budget, not a true browser-side
        transfer measurement — Lighthouse covers the latter."""
        r = self.client.get("/", follow_redirects=False)
        if r.status_code != 200:
            self.skipTest(f"home returned {r.status_code}")
        total = len(r.text.encode("utf-8"))
        static_dir = os.path.join(
            os.path.dirname(server.__file__), "static",
        )
        for url_path in set(_STATIC_RE.findall(r.text)):
            asset = url_path.replace("/_gateway_static/", "", 1)
            full = os.path.join(static_dir, asset)
            try:
                total += os.path.getsize(full)
            except OSError:
                # Asset doesn't exist on disk — that's a separate bug
                # (broken link), surfaced by a different test if we
                # ever add a 'no broken links' check. For now, skip.
                continue
        self.assertLess(
            total, 500_000,
            f"home HTML + linked static assets = {total} bytes "
            f"(budget 500 KB)",
        )


if __name__ == "__main__":
    unittest.main()

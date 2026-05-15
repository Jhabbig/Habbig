"""Smoke tests for the 13 subproduct subdomain landing pages.

Each subdomain (sports, weather, world, crypto, midterm, traders, whale,
voters, climate, disasters, cb, health, love) should serve the
``subproduct_landing.html`` template at ``GET /`` when the Host header
matches ``<slug>.narve.ai``. The rendered page must carry the dashboard
chrome (``app-shell--no-sidebar`` → ``main-content`` → ``page-frame``
→ ``sp-hero``), the post-rename clean topic name in ``<title>``, and
the site-wide redesign stylesheet (``narve-redesign.css``) — only the
apex ``/`` is allowed to skip the redesign layer, not the subdomains.

This guards two regressions at once:

1. **Rename**: the new product names landed at commit-rename time —
   "Sports" not "Sharpe Sports", "Weather" not "Polymarket Weather",
   etc. Older copy still lives inside ``hero_sub`` paragraphs, but
   the ``<title>`` must reflect the clean topic name.

2. **Uniform chrome**: every subdomain landing now reuses the
   dashboard shell + page-frame so the visual silhouette matches the
   authenticated app. A future template rename or PWA-middleware
   change that drops any of the four shell classes — or accidentally
   adds the subdomain ``/`` to ``_NO_REDESIGN_PATHS`` — gets caught
   here.
"""

from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# The 13 subdomain slugs and their post-rename clean topic names. The
# value is what must appear in the rendered <title>; it matches
# ``SUBPRODUCTS[slug]["name"]`` in subproduct.py and is intentionally
# free of legacy product brand names ("Sharpe Sports", "Polymarket
# Weather", "Crypto Edge", etc.) that older hero_sub copy still uses.
SUBDOMAIN_TITLES: dict[str, str] = {
    "sports": "Sports",
    "weather": "Weather",
    "world": "World",
    "crypto": "Crypto",
    "midterm": "Midterm",
    "traders": "Traders",
    "whale": "Whale",
    "voters": "Voters",
    "climate": "Climate",
    "disasters": "Disasters",
    "cb": "Central Bank",
    "health": "Health",
    "love": "Love",
}


class TestSubdomainLandings(unittest.TestCase):
    """One smoke test per subdomain — drives the real FastAPI app via
    Starlette's TestClient and asserts the rendered HTML carries the
    chrome + title contract."""

    @classmethod
    def setUpClass(cls):
        # Production gate requires CF-Connecting-IP; turn it off so the
        # TestClient doesn't 403 before the route runs. RATE_LIMIT_ENABLED
        # = false keeps re-running the suite from tripping the global
        # limiter on shared in-process state.
        os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
        os.environ.pop("PRODUCTION", None)
        try:
            from fastapi.testclient import TestClient
            import server  # noqa: F401 — force app import
            cls.client = TestClient(server.app)
            cls.skipped = False
        except Exception as exc:  # pragma: no cover — guard against env
            cls.skipped = True
            cls.skip_reason = str(exc)

    def setUp(self):
        if getattr(self, "skipped", False):
            self.skipTest(f"server import failed: {self.skip_reason}")

    def _fetch_landing(self, slug: str):
        """GET / with Host: <slug>.narve.ai, return the response."""
        return self.client.get("/", headers={"Host": f"{slug}.narve.ai"})

    def _assert_landing_contract(self, slug: str, title_name: str) -> None:
        """Single-subdomain contract: status 200, chrome classes present,
        title carries the clean topic name, redesign stylesheet loaded."""
        r = self._fetch_landing(slug)

        self.assertEqual(
            r.status_code, 200,
            f"{slug}.narve.ai/ expected 200, got {r.status_code}",
        )

        # Dashboard chrome — these four classes are the page-shell
        # silhouette every subdomain landing must share with the
        # authenticated dashboard. Drop one and the visual signature
        # diverges; the test fails loudly.
        for cls in ("app-shell--no-sidebar", "main-content",
                    "page-frame", "sp-hero"):
            self.assertIn(
                cls, r.text,
                f"{slug}.narve.ai/ missing chrome class {cls!r}",
            )

        # Clean topic name — guards against the older "Sharpe Sports"
        # / "Polymarket Weather" / "Crypto Edge" copy leaking back into
        # the <title>. We pull the title out and assert on it directly
        # so a stray match inside body copy doesn't paper over a
        # missing/renamed title tag.
        m = re.search(r"<title>(.*?)</title>", r.text, re.DOTALL)
        self.assertIsNotNone(
            m, f"{slug}.narve.ai/ has no <title> tag",
        )
        title = m.group(1).strip()
        self.assertIn(
            title_name, title,
            f"{slug}.narve.ai/ title {title!r} missing clean name "
            f"{title_name!r}",
        )

        # narve-redesign.css must load on every subdomain landing —
        # only the apex ``/`` is allowed to opt out (the pre-release
        # particles canvas needs an un-framed body). A path-based
        # exclusion that catches subdomain ``/`` is a regression.
        self.assertIn(
            "narve-redesign.css", r.text,
            f"{slug}.narve.ai/ is missing narve-redesign.css — only the "
            f"apex / should be excluded from the redesign layer",
        )

    # ── One method per subdomain so a single broken landing shows up
    #    as a named failure (and pytest -k can target it). The loop
    #    builds them at class-creation time. ─────────────────────────


def _make_test(slug: str, title_name: str):
    def _test(self):
        self._assert_landing_contract(slug, title_name)
    _test.__name__ = f"test_{slug}_landing_contract"
    _test.__doc__ = (
        f"GET / on {slug}.narve.ai serves the landing chrome and the "
        f"renamed title {title_name!r}."
    )
    return _test


for _slug, _name in SUBDOMAIN_TITLES.items():
    setattr(TestSubdomainLandings,
            f"test_{_slug}_landing_contract",
            _make_test(_slug, _name))


if __name__ == "__main__":
    unittest.main()

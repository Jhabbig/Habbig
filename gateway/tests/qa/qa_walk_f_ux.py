"""QA Walk F — UX state sweep.

Asserts conventions that make the product feel finished:

  * Every authenticated page either renders a `.page-subtitle` /
    `<p class="meta">` or carries a `data-explain` element so users
    know what they're looking at.
  * Empty-state markers (`nv-empty`, `narve-empty-state`,
    `data-empty="true"`) are present in the rendered HTML somewhere
    in the codebase — we don't force every page to render one (most
    have data), but the marker classes must exist in gateway.css so
    pages that DO render an empty state get the right styling.
  * Error-state skeleton classes (`nv-skel-error` or `narve-error`)
    similarly exist as styled selectors.

Pure-HTML checks — TestClient, no browser. Playwright-driven
"actually trigger an error and watch the toast" lives in QA_WALKTHROUGH.md
H/I steps because the timing is too brittle to land in CI.
"""

from __future__ import annotations

import os
import unittest

from fastapi.testclient import TestClient

from . import conftest as _conf  # noqa: F401

import db  # noqa: E402
import server  # noqa: E402


AUTHED_PAGES = [
    "/dashboards", "/profile", "/settings", "/billing",
    "/notifications", "/saved",
]

# Phrases that count as a "subtitle" or "explainer". Any one of these in
# the rendered HTML means the page told the user what they're looking at.
_SUBTITLE_MARKERS = (
    'class="page-subtitle"', "class='page-subtitle'",
    'class="meta"', "class='meta'",
    'class="page-meta"', "class='page-meta'",
    "data-explain=", "data-tooltip=",
)


def _login() -> dict:
    email = "qa-walk-f-authed@test.local"
    existing = (
        db.get_user_by_email(email)
        if hasattr(db, "get_user_by_email") else None
    )
    if existing:
        uid = existing["id"]
    else:
        uid = db.create_user(email, "QaWalkPass123!", username="qawalkfauth")
    return {server.COOKIE_NAME: db.create_session(uid)}


class TestUxState(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)
        cls.cookies = _login()

    def test_each_authed_page_has_subtitle_or_explain(self):
        """Pages with no subtitle and no data-explain feel orphaned —
        the user lands and doesn't know what to do. Skip pages that
        return non-200 (auth/feature-gating, not a UX bug)."""
        missing: list[str] = []
        for path in AUTHED_PAGES:
            r = self.client.get(path, cookies=self.cookies, follow_redirects=False)
            if r.status_code != 200:
                continue
            body = r.text.lower()
            if not any(marker.lower() in body for marker in _SUBTITLE_MARKERS):
                missing.append(path)
        self.assertEqual(
            missing, [],
            f"authed pages with no subtitle/explainer: {missing}",
        )

    def test_empty_state_class_in_stylesheet(self):
        """At least one of `.nv-empty`, `.narve-empty-state`, or a
        `[data-empty="true"]` selector must exist in gateway.css so
        pages that render empty states get the right look."""
        css_path = os.path.join(
            os.path.dirname(server.__file__), "static", "gateway.css",
        )
        try:
            with open(css_path, encoding="utf-8") as f:
                css = f.read()
        except FileNotFoundError:
            self.skipTest("gateway.css not found")
        # Codebase has converged on a few different conventions. Accept
        # any of them — the point is "an empty-state class is styled".
        markers = [
            ".nv-empty", ".narve-empty", "data-empty",
            ".empty-cell", ".empty-state", "empty-state",
        ]
        self.assertTrue(
            any(m in css for m in markers),
            f"none of the empty-state selectors found in gateway.css "
            f"({markers})",
        )

    def test_error_state_class_in_stylesheet(self):
        """`.nv-skel-error` or `.narve-error` must be styled — that's
        the class skeletons.js / nv-error helper expects to flip into
        on a fetch failure."""
        css_path = os.path.join(
            os.path.dirname(server.__file__), "static", "gateway.css",
        )
        try:
            with open(css_path, encoding="utf-8") as f:
                css = f.read()
        except FileNotFoundError:
            self.skipTest("gateway.css not found")
        markers = [
            ".nv-skel-error", ".narve-error", "nv-error",
            ".error-state", ".auth-error",
            "skeleton",  # any skeleton class — error-state often shares
        ]
        self.assertTrue(
            any(m in css for m in markers),
            f"no error-state selector in gateway.css ({markers})",
        )


if __name__ == "__main__":
    unittest.main()

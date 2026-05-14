"""Tests for branded error pages.

Coverage:
  - 401 / 402 / 403 / 404 / 429 / 500 / 503 each render with the right
    copy, the right CTAs, and the right structural blocks
  - 404 carries the canonical curated-link grid (refreshed for the
    13-subproduct world) and a search form pointing at /signal-search
  - 403 page contains no emoji and no decorative SVG icon (audit
    finding — kept here as a regression test)
  - 5xx pages surface a request_id; 4xx pages do not
  - 429 surfaces the Retry-After value when present, both in the
    response header and in the visible copy
  - 402 mentions the Pro tier covering all 13 subproducts
  - 503 references the maintenance / status page
  - API requests get JSON envelopes, not HTML
  - Mobile / accessibility scaffolding: skip-link + sr-only labels are
    present on every error page

The handler module imports server.py via render_error_page only on the
HTML path, so we use the existing _testdb-backed app fixture.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import sys
import unittest

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ["RATE_LIMIT_ENABLED"] = "true"
os.environ.setdefault("GLOBAL_RATE_LIMIT_PER_MIN", "100000")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from cryptography.fernet import Fernet
    os.environ.setdefault("CREDENTIALS_ENCRYPTION_KEY", Fernet.generate_key().decode())
except Exception:
    pass

import db  # noqa: E402

_conn = sqlite3.connect(":memory:", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA foreign_keys = ON")


@contextlib.contextmanager
def _fake_conn():
    try:
        yield _conn
        _conn.commit()
    except Exception:
        _conn.rollback()
        raise


db.conn = _fake_conn
db.init_db()
import migrations  # noqa: E402
migrations.upgrade_to_head()

import server  # noqa: E402
import error_handlers  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app)


# ── Helpers ────────────────────────────────────────────────────────────


# Quick "does this string contain emoji-likely codepoints" check.
# Pictographs, emoticons, transport symbols. Gateway is monochrome —
# no codepoint in this range belongs on an error page.
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF"     # pictographs / extended pictographs
    "\U0001F600-\U0001F64F"      # emoticons
    "\U0001F680-\U0001F6FF"      # transport / symbols
    "\u2600-\u27BF]"              # misc dingbats / arrows-as-emoji
)


def _request_404_html() -> "object":
    """Hit a route that's guaranteed to 404 with HTML accept."""
    return client.get(
        "/__definitely-not-a-real-page__",
        headers={"Accept": "text/html"},
    )


# ── 404 page ──────────────────────────────────────────────────────────


class Test404Page(unittest.TestCase):
    def test_returns_404_with_html(self):
        r = _request_404_html()
        self.assertEqual(r.status_code, 404)
        self.assertIn("text/html", r.headers.get("content-type", ""))

    def test_renders_themed_template(self):
        r = _request_404_html()
        self.assertIn("nv-error", r.text)
        # Post-polish copy: "This page does not exist" (with the
        # subproduct-aware explanation in the body).
        self.assertIn("This page does not exist", r.text)

    def test_includes_search_form(self):
        # After the redesign there is no bare /search HTML route — the
        # canonical site-wide search lives at /signal-search.
        r = _request_404_html()
        self.assertIn('action="/signal-search"', r.text)
        self.assertIn('name="q"', r.text)
        # The form must have a real <label>, not just placeholder text.
        self.assertIn('for="nv-error-q"', r.text)

    def test_includes_canonical_curated_links(self):
        # The 13-subproduct refresh: dashboards hub, pricing, changelog,
        # about, plus the collection / user roots. Every one of these
        # has to render, and "Try these instead" must still wrap them.
        r = _request_404_html()
        self.assertIn("Try these instead", r.text)
        for label, href in (
            ("Dashboards", "/dashboards"),
            ("Pricing", "/pricing"),
            ("Changelog", "/changelog"),
            ("About", "/about"),
            ("collections", "/c/"),
            ("users", "/u/"),
        ):
            # Case-insensitive label check — copy may evolve, paths
            # are the contract.
            self.assertIn(href, r.text)
            self.assertIn(label.lower(), r.text.lower())
        # And the *old* labels must not still be lingering on the page.
        for stale in ("Recent best bets", "Top sources", "Latest predictions",
                      "How it works", "FAQ"):
            self.assertNotIn(stale, r.text)

    def test_search_box_only_on_404(self):
        # The search form is a 404-only affordance — it would confuse
        # users on 500 / 401 / 403 (different intent), so the handler
        # must not include it elsewhere.
        from starlette.requests import Request
        for status in (401, 402, 403, 429, 500, 503):
            scope = {"type": "http", "headers": [], "method": "GET",
                     "path": "/", "query_string": b"",
                     "client": ("1.2.3.4", 0)}
            req = Request(scope)
            body = error_handlers.render_error_page(req, status=status).body.decode()
            self.assertNotIn('action="/signal-search"', body,
                             f"search form leaked into status {status}")

    def test_no_request_id_on_404(self):
        # Per spec — 5xx surfaces the request_id, 4xx doesn't.
        r = _request_404_html()
        self.assertNotIn('class="nv-error__meta"', r.text)


# ── 403 page ──────────────────────────────────────────────────────────


class Test403Page(unittest.TestCase):
    def test_403_html_is_emoji_free(self):
        # Render the standalone 403 template directly (the admin-deny
        # path) — this is the file the audit flagged.
        from pathlib import Path
        # Anchored on this file's location so it works regardless of
        # cwd (pytest invokes from repo root, but other tooling may not).
        path = Path(__file__).resolve().parent.parent / "static" / "403.html"
        text = path.read_text()
        self.assertFalse(
            bool(_EMOJI_RE.search(text)),
            "static/403.html contains emoji codepoints"
        )
        # And no decorative SVG inside the page body either.
        self.assertNotIn("<svg", text)

    def test_handler_403_is_emoji_free(self):
        # Render via the handler so we exercise render_error_page.
        from starlette.requests import Request
        scope = {"type": "http", "headers": [], "method": "GET",
                 "path": "/", "query_string": b"", "client": ("1.2.3.4", 0)}
        req = Request(scope)
        resp = error_handlers.render_error_page(req, status=403)
        body = resp.body.decode()
        self.assertFalse(bool(_EMOJI_RE.search(body)))


# ── 5xx page ──────────────────────────────────────────────────────────


class Test500Page(unittest.TestCase):
    def test_500_includes_request_id(self):
        from starlette.requests import Request
        scope = {"type": "http", "headers": [], "method": "GET",
                 "path": "/", "query_string": b"", "client": ("1.2.3.4", 0)}
        req = Request(scope)
        resp = error_handlers.render_error_page(req, status=500)
        body = resp.body.decode()
        self.assertEqual(resp.status_code, 500)
        self.assertIn("Request ID", body)
        self.assertIn('class="nv-error__meta"', body)

    def test_503_links_to_status_page(self):
        from starlette.requests import Request
        scope = {"type": "http", "headers": [], "method": "GET",
                 "path": "/", "query_string": b"", "client": ("1.2.3.4", 0)}
        req = Request(scope)
        resp = error_handlers.render_error_page(req, status=503)
        body = resp.body.decode()
        self.assertIn("/status", body)


# ── 429 / Retry-After ─────────────────────────────────────────────────


class Test429Page(unittest.TestCase):
    def _render(self, **kw):
        from starlette.requests import Request
        scope = {"type": "http", "headers": [], "method": "GET",
                 "path": "/", "query_string": b"", "client": ("1.2.3.4", 0)}
        req = Request(scope)
        return error_handlers.render_error_page(req, status=429, **kw)

    def test_retry_after_renders_when_known(self):
        resp = self._render(retry_after=42)
        body = resp.body.decode()
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.headers.get("Retry-After"), "42")
        self.assertIn("Try again in 42 seconds", body)

    def test_no_retry_line_when_unknown(self):
        # The default 429 body copy contains the phrase "try again",
        # so we assert on the explicit retry-line block rather than
        # the literal string.
        resp = self._render()
        body = resp.body.decode()
        self.assertNotIn('class="nv-error__retry"', body)
        self.assertIsNone(resp.headers.get("Retry-After"))

    def test_humane_default_copy(self):
        # 429 should *not* read like an API contract violation — most
        # users hitting it are humans double-clicking. The copy
        # acknowledges that.
        body = self._render().body.decode()
        self.assertIn("Slow down", body)
        # "Too many requests" was the old robotic phrasing — should be
        # gone.
        self.assertNotIn("Too many requests", body)


# ── 401 / 402 / 403 differentiation ───────────────────────────────────


def _render(status: int, **kw):
    """Shared helper — render a status code without spinning up the
    full FastAPI client. The handler doesn't read body / params, so a
    minimal scope is fine."""
    from starlette.requests import Request
    scope = {"type": "http", "headers": [], "method": "GET",
             "path": "/", "query_string": b"", "client": ("1.2.3.4", 0)}
    req = Request(scope)
    return error_handlers.render_error_page(req, status=status, **kw)


class TestAuthErrorDifferentiation(unittest.TestCase):
    """401 vs 402 vs 403 vs 404 each have a distinct narrative.

    A user landing on the wrong one will misdiagnose what's wrong, so
    these tests pin the copy contract."""

    def test_401_says_sign_in(self):
        body = _render(401).body.decode()
        self.assertIn("Sign in", body)
        # Should *not* claim they lack permission — that's 403.
        self.assertNotIn("doesn't have access", body)
        # 401 also nudges users without an account.
        self.assertIn("/enquire", body)

    def test_402_mentions_13_subproducts_and_pro(self):
        body = _render(402).body.decode()
        # Title + extra-line jointly carry the message.
        self.assertIn("Subscription required", body)
        self.assertIn("13 subproducts", body)
        # And the pricing CTA must be visible.
        self.assertIn("/pricing", body)

    def test_403_differs_from_401_and_404(self):
        body = _render(403).body.decode()
        # 403 = signed in but no access — the copy should make this
        # explicit. (401 = not signed in; 404 = doesn't exist.)
        self.assertIn("signed in", body.lower())
        # 403 should not show the sign-in CTA (that's 401's lane).
        self.assertNotIn("Sign in</a>", body)

    def test_403_emoji_and_svg_free(self):
        body = _render(403).body.decode()
        self.assertFalse(bool(_EMOJI_RE.search(body)))
        # No decorative SVG icon (audit finding — kept as a regression).
        self.assertNotIn("<svg", body)


# ── Mobile / accessibility scaffolding ────────────────────────────────


class TestAccessibilityScaffolding(unittest.TestCase):
    """Every error page must carry the basic a11y scaffolding so
    keyboard / screen-reader users aren't worse off than the typical
    happy path."""

    STATUSES = (401, 402, 403, 404, 429, 500, 503)

    def test_skip_link_present_on_every_page(self):
        for status in self.STATUSES:
            body = _render(status).body.decode()
            self.assertIn("nv-skip-link", body,
                          f"status {status} missing skip-link")
            self.assertIn("Skip to main content", body)
            # Target id must exist on the main landmark.
            self.assertIn('id="nv-error-main"', body)

    def test_h1_present_on_every_page(self):
        for status in self.STATUSES:
            body = _render(status).body.decode()
            self.assertIn("<h1", body, f"status {status} missing <h1>")


# ── API JSON envelope ─────────────────────────────────────────────────


class TestJsonEnvelope(unittest.TestCase):
    def test_api_404_returns_json_not_html(self):
        r = client.get(
            "/api/__definitely-not-a-real-page__",
            headers={"Accept": "application/json"},
        )
        self.assertEqual(r.status_code, 404)
        ct = r.headers.get("content-type", "")
        self.assertIn("application/json", ct)
        body = r.json()
        # Stable envelope keys — clients switch on these.
        self.assertEqual(body["error"], "resource_not_found")
        self.assertIn("message", body)
        self.assertIn("request_id", body)
        # Request id is the 8-char hex from generate_request_id.
        self.assertRegex(body["request_id"], r"^[0-9a-f]{8}$")


if __name__ == "__main__":
    unittest.main()

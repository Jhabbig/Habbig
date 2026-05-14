"""Tests for branded error pages.

Coverage:
  - 404 page renders with the search box + curated top-link grid
  - 403 page contains no emoji and no decorative SVG icon
  - 5xx pages surface a request_id; 4xx pages do not
  - API requests get JSON envelopes, not HTML
  - 429 surfaces the Retry-After value when present

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
        self.assertIn("Not found", r.text)

    def test_includes_search_form(self):
        r = _request_404_html()
        self.assertIn('action="/search"', r.text)
        self.assertIn('name="q"', r.text)

    def test_includes_curated_top_links(self):
        r = _request_404_html()
        self.assertIn("Try these instead", r.text)
        # Spot-check the curated list — at least three of the labels.
        for label in ("Recent best bets", "Top sources", "Pricing"):
            self.assertIn(label, r.text)

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
    def test_retry_after_renders_when_known(self):
        from starlette.requests import Request
        scope = {"type": "http", "headers": [], "method": "GET",
                 "path": "/", "query_string": b"", "client": ("1.2.3.4", 0)}
        req = Request(scope)
        resp = error_handlers.render_error_page(req, status=429, retry_after=42)
        body = resp.body.decode()
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.headers.get("Retry-After"), "42")
        self.assertIn("Try again in 42 seconds", body)

    def test_no_retry_line_when_unknown(self):
        # The default 429 body copy contains the phrase "try again",
        # so we assert on the explicit retry-line block rather than
        # the literal string.
        from starlette.requests import Request
        scope = {"type": "http", "headers": [], "method": "GET",
                 "path": "/", "query_string": b"", "client": ("1.2.3.4", 0)}
        req = Request(scope)
        resp = error_handlers.render_error_page(req, status=429)
        body = resp.body.decode()
        self.assertNotIn('class="nv-error__retry"', body)
        self.assertIsNone(resp.headers.get("Retry-After"))


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

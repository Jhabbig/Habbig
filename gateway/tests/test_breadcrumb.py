"""Tests for the shared breadcrumb helper + render_page integration.

Covers:
  * render_breadcrumb([(label, href)]) → HTML shape (last item gets
    aria-current=page regardless of href).
  * render_breadcrumb_schema() emits valid schema.org BreadcrumbList
    JSON-LD with absolute URLs; items without href are skipped.
  * render_page auto-fills `raw_breadcrumb` + `raw_breadcrumb_schema`
    when the caller passes `breadcrumb=[...]`.
  * The schema is auto-injected into <head> when the template didn't
    interpolate `{{ raw_breadcrumb_schema }}` directly.
  * Empty / missing breadcrumb does NOT break legacy pages — both
    placeholders fall back to "" so 404 / error pages stay clean.
"""

from __future__ import annotations

USES_TESTDB = True

import json
import os
import unittest

os.environ.setdefault("EMAIL_DRY_RUN", "true")

from tests import _testdb  # noqa: F401 — shared in-memory DB

import server


# ── render_breadcrumb HTML ─────────────────────────────────────────────────


class TestRenderBreadcrumb(unittest.TestCase):
    def test_empty_returns_empty_string(self):
        self.assertEqual(server.render_breadcrumb([]), "")
        self.assertEqual(server.render_breadcrumb(None), "")

    def test_single_item_is_aria_current(self):
        out = server.render_breadcrumb([("Home", None)])
        self.assertIn('<nav class="nv-breadcrumb"', out)
        self.assertIn('aria-label="Breadcrumb"', out)
        self.assertIn('aria-current="page"', out)
        self.assertIn(">Home<", out)
        self.assertNotIn("<a ", out)

    def test_multi_item_last_is_current(self):
        out = server.render_breadcrumb([
            ("Dashboard", "/dashboard"),
            ("Markets", "/dashboard/markets"),
            ("My Market", None),
        ])
        # First two items must be links.
        self.assertIn('<a href="/dashboard">Dashboard</a>', out)
        self.assertIn('<a href="/dashboard/markets">Markets</a>', out)
        # Last is aria-current and NOT a link.
        self.assertIn('aria-current="page">My Market<', out)
        self.assertEqual(out.count("aria-current"), 1)
        self.assertEqual(out.count("</a>"), 2)

    def test_last_item_with_href_is_still_aria_current(self):
        # Spec: trailing crumb is the current page even if a URL is given
        # (callers occasionally pass href for canonical / SEO reasons).
        out = server.render_breadcrumb([
            ("Section", "/section"),
            ("Current", "/section/current"),
        ])
        self.assertIn('aria-current="page">Current<', out)
        # The link form must NOT be used for the trailing crumb.
        self.assertNotIn('<a href="/section/current">Current</a>', out)

    def test_html_escapes_label_and_url(self):
        out = server.render_breadcrumb([
            ("Home", "/?q=<script>alert(1)</script>"),
            ("<b>danger</b>", None),
        ])
        # Both label and URL escape.
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("&lt;b&gt;danger&lt;/b&gt;", out)
        self.assertNotIn("<script>", out)
        self.assertNotIn("<b>danger</b>", out)

    def test_defensive_against_stray_strings(self):
        # A misshaped item must not 500 the page.
        out = server.render_breadcrumb([
            ("Section", "/section"),
            "lone-string",
        ])
        # The stray string is rendered as the current crumb.
        self.assertIn('aria-current="page">lone-string<', out)


# ── render_breadcrumb_schema JSON-LD ───────────────────────────────────────


class TestRenderBreadcrumbSchema(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(server.render_breadcrumb_schema([]), "")

    def test_no_qualifying_items_returns_empty(self):
        # Only items WITH an href are surfaced to crawlers.
        self.assertEqual(
            server.render_breadcrumb_schema([("Standalone", None)]),
            "",
        )

    def test_emits_valid_jsonld(self):
        out = server.render_breadcrumb_schema([
            ("Home", "/"),
            ("Markets", "/dashboard/markets"),
            ("My Market", None),
        ])
        self.assertIn('<script type="application/ld+json">', out)
        # Strip the wrapper and parse the JSON.
        inner = out.replace('<script type="application/ld+json">', "").replace("</script>", "")
        payload = json.loads(inner)
        self.assertEqual(payload["@context"], "https://schema.org")
        self.assertEqual(payload["@type"], "BreadcrumbList")
        # Only the two items with href ended up in the list.
        self.assertEqual(len(payload["itemListElement"]), 2)
        first = payload["itemListElement"][0]
        self.assertEqual(first["position"], 1)
        self.assertEqual(first["name"], "Home")
        self.assertEqual(first["item"], "https://narve.ai/")

    def test_relative_urls_become_absolute(self):
        out = server.render_breadcrumb_schema([
            ("Section", "section"),  # no leading slash
        ])
        payload = json.loads(
            out.replace('<script type="application/ld+json">', "").replace("</script>", "")
        )
        self.assertEqual(payload["itemListElement"][0]["item"], "https://narve.ai/section")

    def test_absolute_urls_pass_through(self):
        out = server.render_breadcrumb_schema([
            ("Outside", "https://example.com/page"),
        ])
        payload = json.loads(
            out.replace('<script type="application/ld+json">', "").replace("</script>", "")
        )
        self.assertEqual(payload["itemListElement"][0]["item"], "https://example.com/page")


# ── render_page integration ────────────────────────────────────────────────


def _write_template(tmp_path, name, body):
    p = tmp_path / f"{name}.html"
    p.write_text(body)
    return p


class TestRenderPageBreadcrumb(unittest.TestCase):
    """Smoke test that the helper hooks into render_page correctly."""

    def setUp(self):
        # Patch STATIC_DIR to a tmpdir for the duration of one test, then
        # restore. This avoids touching real templates AND keeps the test
        # cheap (no full FastAPI pipeline).
        import tempfile, pathlib
        self._orig_static = server.STATIC_DIR
        self._tmp = pathlib.Path(tempfile.mkdtemp(prefix="bc-test-"))
        server.STATIC_DIR = self._tmp

    def tearDown(self):
        import shutil
        server.STATIC_DIR = self._orig_static
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _render(self, body, **ctx):
        _write_template(self._tmp, "tpl_bc", body)
        # Bypass the i18n + css injection by passing a minimal request=None.
        resp = server.render_page("tpl_bc", request=None, **ctx)
        return resp.body.decode("utf-8")

    def test_breadcrumb_kwarg_fills_placeholder(self):
        body = (
            '<!DOCTYPE html><html><head></head>'
            '<body>{{ raw_breadcrumb }}<div>x</div></body></html>'
        )
        out = self._render(body, breadcrumb=[
            ("Home", "/"),
            ("Section", None),
        ])
        self.assertIn('<nav class="nv-breadcrumb"', out)
        self.assertIn('<a href="/">Home</a>', out)
        self.assertIn('aria-current="page">Section<', out)

    def test_no_breadcrumb_kwarg_keeps_template_safe(self):
        body = (
            '<!DOCTYPE html><html><head></head>'
            '<body>{{ raw_breadcrumb }}<div>x</div></body></html>'
        )
        out = self._render(body)
        # Placeholder collapses cleanly to empty — no leftover {{ }} text.
        self.assertNotIn("{{", out)
        self.assertNotIn("raw_breadcrumb", out)

    def test_schema_auto_injected_into_head(self):
        body = (
            '<!DOCTYPE html><html><head><title>x</title></head>'
            '<body><div>{{ raw_breadcrumb }}</div></body></html>'
        )
        out = self._render(body, breadcrumb=[
            ("Home", "/"),
            ("Section", "/section"),
        ])
        # Schema lands inside <head> even though the template didn't
        # interpolate {{ raw_breadcrumb_schema }} directly.
        head_idx = out.lower().find("</head>")
        self.assertGreater(head_idx, 0)
        head_section = out[:head_idx]
        self.assertIn('application/ld+json', head_section)
        self.assertIn('BreadcrumbList', head_section)

    def test_no_double_inject(self):
        body = (
            '<!DOCTYPE html><html><head>'
            '{{ raw_breadcrumb_schema }}</head>'
            '<body>{{ raw_breadcrumb }}</body></html>'
        )
        out = self._render(body, breadcrumb=[
            ("Home", "/"),
            ("Section", "/section"),
        ])
        # The schema was placed by the template; the auto-inject path must
        # NOT add a second copy.
        self.assertEqual(out.count("BreadcrumbList"), 1)


if __name__ == "__main__":
    unittest.main()

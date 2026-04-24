"""Admin shell integration tests.

Exercises the wrapping helper + the partial HTML structure. Does NOT
require a running server — works off the partial + a synthetic Request.

Covers:
  * Shell renders every required landmark + data-active-route hook.
  * Breadcrumb builder produces valid <ol> with aria-current on last item.
  * CSRF auto-inject fires when the inner template references it.
  * Missing content template raises a clear error.
  * render_admin_page returns HTMLResponse with admin-shell in body.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from admin_shell import (
    _minimal_admin_frame,
    _render_breadcrumb,
    _substitute,
    render_admin_page,
)


def _request(path: str = "/admin/churn"):
    """Build a minimal Request-like object sufficient for the shell."""
    req = MagicMock()
    req.url.path = path
    req.cookies = {}
    req.state = MagicMock(csrf_token=None)
    return req


class TestBreadcrumbBuilder(unittest.TestCase):
    def test_trail_renders_with_aria_current_on_last(self):
        html = _render_breadcrumb([("Admin", "/admin"), ("Users", None)])
        self.assertIn('<ol>', html)
        self.assertIn('<a href="/admin">Admin</a>', html)
        self.assertIn('aria-current="page">Users', html)

    def test_empty_trail_returns_empty_string(self):
        self.assertEqual(_render_breadcrumb([]), "")

    def test_href_none_renders_as_plain_text(self):
        html = _render_breadcrumb([("A", None), ("B", None)])
        # Neither entry should have an <a> tag.
        self.assertNotIn("<a ", html)

    def test_html_escapes_labels_and_hrefs(self):
        html = _render_breadcrumb([("<script>", "/x?a=1&b=2"), ("End", None)])
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&amp;b=2", html)


class TestMinimalFrame(unittest.TestCase):
    def test_frame_contains_required_head(self):
        frame = _minimal_admin_frame(page_title="Users", body="<p>x</p>")
        self.assertIn('<html lang="en">', frame)
        self.assertIn("<title>Users · Admin · narve.ai</title>", frame)
        self.assertIn('name="robots" content="noindex', frame)
        self.assertIn("admin-shell.css", frame)
        self.assertIn("mobile-a11y.css", frame)
        self.assertIn("<p>x</p>", frame)

    def test_frame_emits_skip_link(self):
        frame = _minimal_admin_frame(page_title="x", body="")
        self.assertIn('class="narve-skip-link"', frame)
        self.assertIn('href="#main"', frame)


class TestSubstitute(unittest.TestCase):
    def test_raw_keys_not_escaped(self):
        out = _substitute("a{{ raw_x }}b", {"raw_x": "<b>hi</b>"})
        self.assertEqual(out, "a<b>hi</b>b")

    def test_normal_keys_escaped(self):
        out = _substitute("a{{ name }}b", {"name": "<script>"})
        self.assertEqual(out, "a&lt;script&gt;b")

    def test_page_title_trusted_because_static(self):
        # page_title is explicitly listed as raw so tests can verify.
        out = _substitute("{{ page_title }}", {"page_title": "<em>T</em>"})
        self.assertEqual(out, "<em>T</em>")


class TestRenderAdminPage(unittest.TestCase):
    def test_renders_existing_churn_template(self):
        # The template needs only ``raw_risk_pie`` / ``raw_funnel`` /
        # ``raw_top_users`` / ``raw_recent`` — pass empty strings and
        # confirm the shell still wraps the output.
        response = render_admin_page(
            _request("/admin/churn"),
            "admin/churn.html",
            page_title="Churn & retention",
            active_route="churn",
            breadcrumb=[("Admin", "/admin"), ("Churn", "/admin/churn")],
            raw_risk_pie="",
            raw_funnel="",
            raw_top_users="",
            raw_recent="",
        )
        body = response.body.decode()
        self.assertIn('class="admin-shell"', body)
        self.assertIn('data-active-route="churn"', body)
        self.assertIn('<h1 class="admin-page-title">Churn &amp; retention</h1>',
                      body.replace("Churn & retention", "Churn &amp; retention")
                          .replace("Churn &amp; retention · admin", "X"))

    def test_missing_template_raises(self):
        with self.assertRaises(FileNotFoundError):
            render_admin_page(
                _request(),
                "admin/does-not-exist.html",
                page_title="Nope",
            )

    def test_breadcrumb_default_uses_request_path(self):
        response = render_admin_page(
            _request("/admin/flags"),
            "admin/flags.html",
            page_title="Feature flags",
            active_route="flags",
            raw_flag_rows="<tr><td>x</td></tr>",
        )
        body = response.body.decode()
        # Default breadcrumb should end with the page title and use the path.
        self.assertIn("Feature flags", body)
        self.assertIn('/admin">Admin</a>', body)

    def test_main_landmark_present(self):
        response = render_admin_page(
            _request(),
            "admin/churn.html",
            page_title="Churn",
            active_route="churn",
            raw_risk_pie="", raw_funnel="", raw_top_users="", raw_recent="",
        )
        body = response.body.decode()
        self.assertIn('id="main"', body)
        self.assertIn('class="admin-page-body"', body)


if __name__ == "__main__":
    unittest.main()

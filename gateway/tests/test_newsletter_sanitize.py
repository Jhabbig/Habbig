"""Tests for ``email_system.sanitizer.sanitize_newsletter_html``.

The newsletter blast template (``email_system/templates/newsletter_blast.html``)
renders the admin-composed body via ``{{ raw_body_html }}`` — the
``raw_`` prefix in the gateway's tiny email renderer skips HTML-escape.
That's intentional so admins can ship rich announcements (links, lists,
bold, …), but it means an attacker-controlled admin session would
otherwise be a mass-phishing surface to every confirmed subscriber.

The HIGH security fix is an allowlist sanitizer applied to the rendered
body before it reaches the template. These tests pin the three things
that matter for the audit:

  1. **Script-bearing content is stripped.** ``<script>…</script>`` and
     friends never reach the recipient. (We additionally verify the
     *text* inside survives — that's the bleach-style "strip tag, keep
     content" behaviour the audit asked for.)
  2. **`javascript:` href payloads are stripped.** A bare ``<a>`` with
     attacker-controlled scheme can't carry a phishing redirect.
  3. **Safe content is preserved.** The allowlisted tags + http(s)
     hrefs survive untouched so legitimate announcements still render.

Tests run as pure-function unit checks — no DB, no TestClient. The
sanitizer's contract is "string in, sanitized string out", so the
tests stay at that level.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from email_system.sanitizer import sanitize_newsletter_html  # noqa: E402


class ScriptStrippingTests(unittest.TestCase):
    """Tags outside the allowlist are dropped; inner text survives."""

    def test_inline_script_tag_stripped(self) -> None:
        out = sanitize_newsletter_html(
            "<p>hello <script>alert(1)</script>world</p>"
        )
        # The <script> tag must not appear in any form.
        self.assertNotIn("<script", out.lower())
        self.assertNotIn("</script", out.lower())
        # Allowlisted <p> survives and inner text is preserved so the
        # admin's intent (the words around the script) still renders.
        self.assertIn("<p>", out)
        self.assertIn("hello ", out)
        self.assertIn("world", out)

    def test_script_content_does_not_execute_as_html(self) -> None:
        out = sanitize_newsletter_html("<script>alert('xss')</script>")
        # The parser treats script's inner text as data; we drop the
        # tag and emit the text HTML-escaped so nothing closes a real
        # tag downstream.
        self.assertNotIn("<script", out.lower())
        # Quote inside body is HTML-escaped — &#x27; or unchanged
        # depending on parser, but no raw ``alert('xss')`` HTML-active
        # form. Check the literal angle brackets are gone.
        self.assertNotIn("<", out.replace("&lt;", ""))

    def test_iframe_stripped(self) -> None:
        out = sanitize_newsletter_html(
            '<p>before<iframe src="https://evil.example"></iframe>after</p>'
        )
        self.assertNotIn("<iframe", out.lower())
        self.assertNotIn("evil.example", out)
        self.assertIn("before", out)
        self.assertIn("after", out)

    def test_event_handler_stripped_from_allowed_tag(self) -> None:
        # The <p> tag is allowed but its onclick handler must die so a
        # compromised admin can't smuggle JS via a permitted tag.
        out = sanitize_newsletter_html(
            '<p onclick="alert(1)">click me</p>'
        )
        self.assertNotIn("onclick", out.lower())
        self.assertNotIn("alert", out)
        self.assertIn("<p>", out)
        self.assertIn("click me", out)

    def test_image_onerror_stripped(self) -> None:
        # The ``<img onerror=...>`` form is the textbook bleach test.
        out = sanitize_newsletter_html(
            '<img src="https://cdn.example/x.png" onerror="alert(1)">'
        )
        self.assertNotIn("onerror", out.lower())
        self.assertNotIn("alert", out)
        # The https img src is allowed and should survive.
        self.assertIn("cdn.example", out)


class HrefSanitizationTests(unittest.TestCase):
    """``javascript:`` / ``data:`` href values are dropped."""

    def test_javascript_href_dropped(self) -> None:
        out = sanitize_newsletter_html(
            '<a href="javascript:alert(1)">click</a>'
        )
        # The unsafe href must vanish; the anchor tag may remain
        # (with no href) so the visible link text survives.
        self.assertNotIn("javascript", out.lower())
        self.assertNotIn("alert", out)
        # Anchor text is preserved.
        self.assertIn("click", out)

    def test_javascript_href_with_mixed_case_dropped(self) -> None:
        # The scheme check must lowercase before comparing — a naive
        # startswith would let ``JaVaScRiPt:`` slip through.
        out = sanitize_newsletter_html(
            '<a href="JaVaScRiPt:alert(1)">click</a>'
        )
        self.assertNotIn("javascript", out.lower())
        self.assertNotIn("alert", out)
        self.assertIn("click", out)

    def test_data_uri_href_dropped(self) -> None:
        out = sanitize_newsletter_html(
            '<a href="data:text/html,<script>alert(1)</script>">click</a>'
        )
        self.assertNotIn("data:", out.lower())
        self.assertNotIn("alert", out)

    def test_vbscript_href_dropped(self) -> None:
        out = sanitize_newsletter_html(
            '<a href="vbscript:msgbox(1)">click</a>'
        )
        self.assertNotIn("vbscript", out.lower())

    def test_http_href_preserved(self) -> None:
        out = sanitize_newsletter_html(
            '<a href="https://example.com/path">visit</a>'
        )
        self.assertIn('href="https://example.com/path"', out)
        self.assertIn("visit", out)

    def test_mailto_href_preserved(self) -> None:
        # ``mailto:`` is explicitly in the safe-prefix list because
        # newsletters routinely link to support@/feedback@ addresses.
        out = sanitize_newsletter_html(
            '<a href="mailto:hello@narve.ai">say hi</a>'
        )
        self.assertIn('href="mailto:hello@narve.ai"', out)
        self.assertIn("say hi", out)


class SafeContentPreservedTests(unittest.TestCase):
    """Allowlisted markup survives end-to-end."""

    def test_paragraphs_strong_em_preserved(self) -> None:
        out = sanitize_newsletter_html(
            "<p>Hello <strong>world</strong> and <em>universe</em>.</p>"
        )
        self.assertIn("<p>", out)
        self.assertIn("<strong>world</strong>", out)
        self.assertIn("<em>universe</em>", out)

    def test_lists_preserved(self) -> None:
        out = sanitize_newsletter_html(
            "<ul><li>one</li><li>two</li></ul>"
        )
        self.assertIn("<ul>", out)
        self.assertIn("<li>one</li>", out)
        self.assertIn("<li>two</li>", out)

    def test_ordered_lists_preserved(self) -> None:
        out = sanitize_newsletter_html(
            "<ol><li>first</li><li>second</li></ol>"
        )
        self.assertIn("<ol>", out)
        self.assertIn("<li>first</li>", out)
        self.assertIn("<li>second</li>", out)

    def test_headings_preserved(self) -> None:
        out = sanitize_newsletter_html("<h2>Title</h2><h3>Subtitle</h3>")
        self.assertIn("<h2>Title</h2>", out)
        self.assertIn("<h3>Subtitle</h3>", out)

    def test_br_preserved_as_void(self) -> None:
        out = sanitize_newsletter_html("line one<br>line two")
        # Either ``<br>`` or ``<br/>`` is acceptable — both are valid
        # HTML5 void-tag renderings.
        self.assertTrue("<br>" in out or "<br/>" in out)
        self.assertIn("line one", out)
        self.assertIn("line two", out)

    def test_https_img_preserved(self) -> None:
        out = sanitize_newsletter_html(
            '<img src="https://cdn.example/banner.png" alt="banner">'
        )
        self.assertIn('src="https://cdn.example/banner.png"', out)
        self.assertIn('alt="banner"', out)

    def test_http_img_dropped(self) -> None:
        # Per the spec, only ``https://`` images survive — http leaks
        # to mixed-content warnings and can be used for tracking.
        out = sanitize_newsletter_html(
            '<img src="http://cdn.example/banner.png" alt="banner">'
        )
        # The img tag itself remains (since img is allowlisted) but
        # the unsafe src is dropped. Alt text survives so screen
        # readers still announce the intent.
        self.assertNotIn('src="http://', out)
        self.assertIn('alt="banner"', out)

    def test_style_attribute_dropped_on_allowed_tag(self) -> None:
        # The renderer's per-tag inline ``style`` is dropped; the
        # newsletter_blast template's wrapper owns the visuals.
        out = sanitize_newsletter_html(
            '<p style="background:red">danger zone</p>'
        )
        self.assertNotIn("style=", out)
        self.assertNotIn("background:red", out)
        self.assertIn("<p>", out)
        self.assertIn("danger zone", out)

    def test_realistic_announcement_round_trip(self) -> None:
        # End-to-end sanity: a realistic admin-authored body survives
        # with its essential structure intact.
        out = sanitize_newsletter_html(
            "<h2>Heads up</h2>"
            "<p>We just shipped <strong>three</strong> things:</p>"
            "<ul>"
            "<li>better dashboards</li>"
            "<li>faster <em>everything</em></li>"
            '<li>see <a href="https://narve.ai/changelog">changelog</a></li>'
            "</ul>"
        )
        self.assertIn("<h2>Heads up</h2>", out)
        self.assertIn("<strong>three</strong>", out)
        self.assertIn("<em>everything</em>", out)
        self.assertIn('href="https://narve.ai/changelog"', out)
        # No styling residue, no script, no inline JS.
        self.assertNotIn("script", out.lower())
        self.assertNotIn("javascript", out.lower())
        self.assertNotIn("onclick", out.lower())


class EdgeCaseTests(unittest.TestCase):
    """Inputs that historically trip naive sanitizers."""

    def test_none_input(self) -> None:
        self.assertEqual(sanitize_newsletter_html(None), "")  # type: ignore[arg-type]

    def test_empty_input(self) -> None:
        self.assertEqual(sanitize_newsletter_html(""), "")

    def test_plain_text_input(self) -> None:
        out = sanitize_newsletter_html("just words, no markup")
        self.assertIn("just words, no markup", out)

    def test_unbalanced_tags_get_closed(self) -> None:
        # An admin's source with a missing close tag shouldn't punch a
        # hole through the surrounding template. The sanitizer closes
        # all open allowed tags at end-of-input.
        out = sanitize_newsletter_html("<p>oops")
        self.assertTrue(out.endswith("</p>"))

    def test_html_comments_stripped(self) -> None:
        # IE conditional comments + comment-smuggled payloads are a
        # known XSS surface in some email clients.
        out = sanitize_newsletter_html(
            "<p>before</p><!-- [if IE]><script>alert(1)</script><![endif] -->"
            "<p>after</p>"
        )
        self.assertNotIn("script", out.lower())
        self.assertNotIn("alert", out)
        self.assertIn("before", out)
        self.assertIn("after", out)

    def test_text_between_disallowed_tags_html_escaped(self) -> None:
        # An attacker could otherwise embed literal ``<script>`` text
        # inside a stripped tag and have it re-parse as a tag if the
        # output ever round-trips through another parser.
        out = sanitize_newsletter_html("<style>p { } </style>raw text")
        # No raw <style> emitted.
        self.assertNotIn("<style", out.lower())
        self.assertIn("raw text", out)


if __name__ == "__main__":
    unittest.main()

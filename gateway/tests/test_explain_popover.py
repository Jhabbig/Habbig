"""Tests for the "What is this page?" popover.

Stack constraint: pytest + the in-memory test DB. We don't have a
headless browser in CI, so the lifecycle tests work by parsing
explain_popover.js as text and asserting the relevant code paths
exist (open, escape, outside-click, multi-instance close), then
confirming the assets are auto-injected by render_page() and that
the static EXPLANATIONS table covers ≥ 25 routes.

A future Playwright suite can layer in real DOM-level interaction
tests; this is what's testable from `python -m pytest`.
"""

from __future__ import annotations

USES_TESTDB = True

import json
import re
import unittest
from pathlib import Path

from tests import _testdb  # noqa: F401  — shared in-memory DB


GATEWAY = Path(__file__).resolve().parent.parent
JS_PATH = GATEWAY / "static" / "explain_popover.js"
CSS_PATH = GATEWAY / "static" / "explain_popover.css"


# ── Asset existence ────────────────────────────────────────────────────


class TestAssetsExist(unittest.TestCase):
    def test_js_file_exists(self):
        self.assertTrue(JS_PATH.exists(), f"missing {JS_PATH}")

    def test_css_file_exists(self):
        self.assertTrue(CSS_PATH.exists(), f"missing {CSS_PATH}")


# ── EXPLANATIONS table — coverage + shape ─────────────────────────────


def _extract_explanations() -> dict:
    """Pull the EXPLANATIONS object out of the JS file by regex.

    The script is a self-invoking IIFE so we can't import it. Instead we
    grab the literal object text and parse it as relaxed JSON. Quoted
    keys, double-quoted string values, no trailing commas — close enough
    to JSON for `json.loads` once we strip the JS sugar.
    """
    src = JS_PATH.read_text(encoding="utf-8")
    # Find the opening brace after `var EXPLANATIONS = {` and capture
    # until its matching close brace at the same indent. The simplest
    # robust approach: scan and balance.
    start_marker = "var EXPLANATIONS = {"
    idx = src.find(start_marker)
    assert idx != -1, "EXPLANATIONS table not found in JS"
    body_start = idx + len(start_marker) - 1  # the `{`
    depth = 0
    end = None
    for i in range(body_start, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    assert end is not None, "EXPLANATIONS object not balanced"
    obj_text = src[body_start:end + 1]
    # Strip JS line-comments (// …) — there shouldn't be any inside the
    # data block but the dividers above each section are JS comments.
    obj_text = re.sub(r"//[^\n]*", "", obj_text)
    # Quote bare JS property names so json.loads can parse the literal.
    # We only quote the canonical keys we actually emit; safer than a
    # generic "any identifier followed by colon" regex which would also
    # rewrite URL-fragment lookalikes inside string values.
    for bare in ("title", "body", "link", "href", "label"):
        obj_text = re.sub(
            r"(?<=[{,\s])" + bare + r"\s*:",
            '"' + bare + '":',
            obj_text,
        )
    # Strip trailing commas — JSON doesn't allow them, JS does.
    obj_text = re.sub(r",(\s*[}\]])", r"\1", obj_text)
    return json.loads(obj_text)


class TestExplanationsTable(unittest.TestCase):
    def setUp(self):
        self.table = _extract_explanations()

    def test_at_least_25_entries(self):
        self.assertGreaterEqual(
            len(self.table), 25,
            f"EXPLANATIONS has {len(self.table)} entries; spec asks for 25+",
        )

    def test_every_entry_has_title_and_body(self):
        for path, entry in self.table.items():
            self.assertIsInstance(entry, dict, f"{path}: not a dict")
            self.assertIn("title", entry, f"{path}: missing title")
            self.assertIn("body", entry, f"{path}: missing body")
            self.assertTrue(entry["body"].strip(), f"{path}: empty body")
            self.assertGreater(
                len(entry["body"]), 30,
                f"{path}: body too short ({len(entry['body'])} chars)",
            )
            self.assertLess(
                len(entry["body"]), 600,
                f"{path}: body too long ({len(entry['body'])} chars)",
            )

    def test_paths_are_normalised(self):
        for path in self.table:
            self.assertTrue(path.startswith("/"), f"{path}: must start with /")
            if path != "/":
                self.assertFalse(
                    path.endswith("/"),
                    f"{path}: trailing slash will miss "
                    "window.location.pathname matches",
                )

    def test_required_dashboard_pages_present(self):
        required = [
            "/dashboard/feed",
            "/dashboard/best-bets",
            "/dashboard/markets",
            "/dashboard/sources",
            "/dashboard/intelligence",
            "/dashboard/predictions",
            "/dashboard/watchlist",
        ]
        for path in required:
            self.assertIn(path, self.table, f"{path} missing from spec")

    def test_required_admin_pages_present(self):
        required = [
            "/admin",
            "/admin/users",
            "/admin/impersonations",
            "/admin/flags",
            "/admin/emails",
            "/admin/incidents",
            "/admin/security/forensics",
            "/admin/cache",
            "/admin/ai-usage",
            "/admin/audit-log",
            "/admin/moderation",
        ]
        for path in required:
            self.assertIn(path, self.table, f"{path} missing from spec")

    def test_marketing_pages_present(self):
        for path in ("/pricing", "/methodology"):
            self.assertIn(path, self.table)


# ── JS lifecycle code paths — static-source assertions ────────────────


class TestJSLifecycle(unittest.TestCase):
    def setUp(self):
        self.src = JS_PATH.read_text(encoding="utf-8")

    def test_outside_click_handler_exists(self):
        self.assertIn("outsideClickHandler", self.src)
        self.assertIn(
            'document.addEventListener("click", outsideClickHandler', self.src
        )

    def test_escape_handler_exists(self):
        self.assertIn("escHandler", self.src)
        self.assertRegex(self.src, r'e\.key\s*===?\s*"Escape"')

    def test_close_returns_focus_to_trigger(self):
        # Without focus return, screen readers don't announce the close.
        self.assertIn(
            "activeTrigger.focus", self.src,
            "close() must return focus to the trigger",
        )

    def test_aria_attributes_used(self):
        for attr in ('role="dialog"', "aria-haspopup", "aria-expanded",
                     "aria-label", "aria-labelledby"):
            self.assertIn(attr, self.src, f"missing {attr} in JS")

    def test_multi_instance_safety(self):
        # Opening one popover must close any other before mounting the
        # new one — otherwise both stay on screen.
        self.assertRegex(
            self.src,
            r"if \(activePopover\)\s*close\(\)",
            "show() must close an active popover before opening another",
        )

    def test_html_escape_used(self):
        # Body text must never go into innerHTML unescaped.
        self.assertIn("escapeHtml(", self.src)
        # The escape helper must rewrite the dangerous characters. We
        # check by searching for the substitution targets (e.g. "&amp;"
        # and "&lt;") rather than the source character itself, which is
        # ambiguous in arbitrary positions of the source code.
        for replacement in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
            self.assertIn(replacement, self.src,
                          f"escape map missing {replacement}")

    def test_print_hides_widget(self):
        css = CSS_PATH.read_text(encoding="utf-8")
        self.assertIn("@media print", css)


# ── render_page asset injection ────────────────────────────────────────


class TestRenderPageInjection(unittest.TestCase):
    def test_skel_injection_includes_explain_assets(self):
        server_src = (GATEWAY / "server.py").read_text(encoding="utf-8")
        self.assertIn(
            "/_gateway_static/explain_popover.css",
            server_src,
            "render_page() should pull explain_popover.css alongside the "
            "rest of the auto-injected widgets",
        )
        self.assertIn(
            "/_gateway_static/explain_popover.js",
            server_src,
            "render_page() should pull explain_popover.js",
        )


# ── No regression on existing widgets ─────────────────────────────────


class TestRenderPageStillCarriesPriorWidgets(unittest.TestCase):
    """Belt-and-braces — adding the explain assets must not knock the
    other auto-injected widgets out of the same block."""

    def test_skeletons_states_lang_changelog_all_present(self):
        server_src = (GATEWAY / "server.py").read_text(encoding="utf-8")
        for path in (
            "/_gateway_static/skeletons.css",
            "/_gateway_static/states.css",
            "/_gateway_static/lang-switcher.css",
            "/_gateway_static/changelog_widget.css",
            "/_gateway_static/skeletons.js",
            "/_gateway_static/i18n-client.js",
            "/_gateway_static/lang-switcher.js",
            "/_gateway_static/changelog_widget.js",
        ):
            self.assertIn(path, server_src, f"render_page lost {path}")


if __name__ == "__main__":
    unittest.main()

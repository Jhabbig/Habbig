"""Static checks for the keyboard-shortcut + discovery surface.

We don't run a real browser here — the registry/help/discovery code is
verified by reading the JS files and asserting the contract:

  * shortcuts.js exposes ``register`` + ``list`` + ``showHelp`` on
    ``window.narve.shortcuts``.
  * Every navigation key the spec lists is registered with the right
    ``description`` text.
  * The help overlay binds to ``?`` and ``cmd+/`` (and Esc).
  * shortcuts-discovery.js dismisses-forever via the documented
    localStorage key, and the discovery toast script is wired into
    pwa_middleware.py so every render_page injects it.
  * The toast CSS exists so an open hint isn't unstyled.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text()


class TestShortcutsRegistry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _read("static/shortcuts.js")

    def test_namespace_exposes_register_list_showhelp(self):
        self.assertIn("narve.shortcuts = {", self.text)
        for fn in ("register,", "list:", "showHelp"):
            self.assertIn(fn, self.text)

    def test_help_overlay_bound_to_question_mark_and_cmd_slash(self):
        block = re.search(
            r"register\(\{\s*id:\s*'help'.*?\}\)", self.text, re.DOTALL,
        )
        self.assertIsNotNone(block, "help registration not found")
        snippet = block.group(0)
        self.assertIn("'cmd+/'", snippet)
        self.assertIn("'?'", snippet)

    def test_escape_closes_overlay(self):
        # Find the esc-help registration block.
        block = re.search(
            r"register\(\{\s*id:\s*'esc-help'.*?\}\)", self.text, re.DOTALL,
        )
        self.assertIsNotNone(block, "esc-help registration missing")
        self.assertIn("'esc'", block.group(0))

    def test_navigation_keys_match_spec(self):
        expected = {
            "go-feed":          ("g f", "Go to Feed"),
            "go-best-bets":     ("g b", "Go to Best Bets"),
            "go-markets":       ("g m", "Go to Markets"),
            "go-sources":       ("g s", "Go to Sources"),
            "go-intelligence":  ("g i", "Go to Intelligence"),
            "go-watchlist":     ("g w", "Go to Watchlist"),
            "go-predictions":   ("g p", "Go to Predictions"),
            "go-notifications": ("g n", "Go to Notifications"),
            "go-admin":         ("g a", "Go to Admin"),
        }
        for sid, (keys, desc) in expected.items():
            self.assertIn(f"id: '{sid}'", self.text, f"{sid} not registered")
            # Each registration has both the keys + description on the same line.
            line = next(
                line for line in self.text.splitlines() if f"id: '{sid}'" in line
            )
            self.assertIn(f"keys: '{keys}'", line, f"{sid} key mismatch")
            self.assertIn(desc, line, f"{sid} description mismatch")

    def test_admin_nav_gated_by_isAdmin(self):
        # The g-a registration must sit inside the `if (narve.isAdmin)` guard
        # so non-admin users don't see "Go to Admin" in the help overlay.
        guard = self.text.find("if (narve.isAdmin)")
        admin_reg = self.text.find("id: 'go-admin'")
        self.assertNotEqual(guard, -1)
        self.assertNotEqual(admin_reg, -1)
        self.assertLess(guard, admin_reg, "go-admin must be inside the isAdmin guard")

    def test_form_submit_shortcut_present(self):
        self.assertIn("id: 'submit-cmd-enter'", self.text)
        self.assertIn("'cmd+enter'", self.text)
        self.assertIn("'ctrl+enter'", self.text)

    def test_chat_edit_last_shortcut_present(self):
        self.assertIn("id: 'chat-edit-last'", self.text)
        self.assertIn("'cmd+up'", self.text)


class TestDiscoveryHint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _read("static/js/shortcuts-discovery.js")

    def test_localStorage_key_is_dismiss_forever(self):
        self.assertIn('"narve.shortcutHintDismissed"', self.text)

    def test_idle_threshold_is_30_seconds(self):
        self.assertIn("IDLE_MS = 30_000", self.text)

    def test_skips_modifier_combos(self):
        # Modifier-laced keys are real shortcuts, not discovery moments.
        self.assertIn("metaKey", self.text)
        self.assertIn("ctrlKey", self.text)
        self.assertIn("altKey", self.text)

    def test_question_mark_does_not_trigger_discovery(self):
        # `?` is the trigger to OPEN the help overlay — discovery hint
        # should defer to it.
        self.assertIn('event.key === "?"', self.text)

    def test_show_button_calls_showHelp(self):
        self.assertIn("window.narve.shortcuts.showHelp()", self.text)

    def test_dismiss_persists_to_localStorage(self):
        self.assertIn("localStorage.setItem(STORAGE_KEY", self.text)


class TestDiscoveryWiredGlobally(unittest.TestCase):
    """The discovery script has to load on every page or the hint never fires."""

    def test_pwa_middleware_injects_shortcuts_discovery(self):
        text = _read("pwa_middleware.py")
        self.assertIn("/_gateway_static/js/shortcuts-discovery.js", text)
        # Order matters — discovery must come AFTER shortcuts.js so the
        # registry exists when discovery init runs.
        idx_shortcuts = text.index("/_gateway_static/shortcuts.js")
        idx_discovery = text.index("/_gateway_static/js/shortcuts-discovery.js")
        self.assertLess(idx_shortcuts, idx_discovery)

    def test_toast_css_present_in_mobile_a11y(self):
        css = _read("static/mobile-a11y.css")
        self.assertIn(".narve-sc-hint", css)
        self.assertIn(".narve-sc-hint--open", css)
        self.assertIn(".narve-sc-hint__open", css)
        self.assertIn(".narve-sc-hint__close", css)


if __name__ == "__main__":
    unittest.main()

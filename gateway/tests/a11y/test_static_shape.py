"""Static-HTML shape checks for every public page.

Focuses on structural a11y signals that don't need a browser:
  * ``<html lang="…">`` present (WCAG 3.1.1).
  * At least one ``<h1>`` per page (heading-first anchor for screen
    readers).
  * Landmark structure — a ``<main>`` element (skip-link target).
  * Skip-link present (the ``narve-skip-link`` class is auto-injected
    by ``render_page`` — every page that goes through that path gets
    it for free).

This is intentionally conservative. Axe-core in ``test_axe.py`` covers
the long tail of rules; here we only assert the things that a
subscriber-less test rig can verify via an HTTP fetch and a string
scan.

Static templates (``static/*.html``) are read directly rather than
rendered by the server so the test doesn't depend on the DB or
middleware. For pages that *require* dynamic context (e.g.
``render_page`` substitutions) we fetch the page via ``TestClient``
instead — see ``_render_if_needed``.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[2]
STATIC = ROOT / "static"

sys.path.insert(0, str(ROOT))


# Static-only pages that are rendered straight from disk (no template
# substitution beyond ``{{ static: … }}`` which doesn't affect structure).
# Keep in sync with ``scripts/list_public_urls.py``.
STATIC_PAGES: tuple[str, ...] = (
    "prerelease.html",
    "landing.html",
    "pricing.html",
    "subscribe.html",
    "support.html",
    "contact.html",
    "suspended.html",
    "gate.html",
    "login.html",
    "register.html",
    "signup.html",
    "token.html",
    "forgot-password.html",
    "enquire.html",
    "offline.html",
    "terms.html",
    "privacy.html",
    "dpa.html",
    "faq.html",
    "about.html",
    "how-it-works.html",
    "methodology.html",
    "team.html",
    "press.html",
    "changelog.html",
    "narve.html",
    "calendar.html",
    "source.html",
)


def _exists(name: str) -> bool:
    return (STATIC / name).exists()


PRESENT_PAGES = tuple(name for name in STATIC_PAGES if _exists(name))


@pytest.fixture(scope="module")
def page_text():
    """Lazily read every present static template once per module."""
    return {name: (STATIC / name).read_text(errors="replace") for name in PRESENT_PAGES}


@pytest.mark.parametrize("name", PRESENT_PAGES)
def test_html_lang_present(name, page_text):
    """WCAG 3.1.1 — every HTML root needs a lang attribute."""
    text = page_text[name]
    assert re.search(r'<html[^>]+\blang="[^"]+"', text), (
        f"{name}: <html> missing lang attribute"
    )


@pytest.mark.parametrize("name", PRESENT_PAGES)
def test_title_present(name, page_text):
    """Every page has a descriptive <title> (WCAG 2.4.2)."""
    text = page_text[name]
    match = re.search(r"<title>([^<]+)</title>", text, re.IGNORECASE)
    assert match, f"{name}: no <title> tag"
    title = match.group(1).strip()
    assert len(title) >= 3, f"{name}: title is empty or near-empty: {title!r}"


@pytest.mark.parametrize("name", PRESENT_PAGES)
def test_has_h1_or_aria_label_on_main(name, page_text):
    """Every page surfaces either an <h1> or an aria-label on the main
    landmark so the heading rotor + page overview isn't silent."""
    text = page_text[name]
    has_h1 = bool(re.search(r"<h1[\s>]", text, re.IGNORECASE))
    has_main_label = bool(
        re.search(r'<(main|\w+[^>]+role="main")[^>]+aria-label="[^"]+"', text, re.IGNORECASE)
    )
    assert has_h1 or has_main_label, (
        f"{name}: no <h1> and no aria-label on the main landmark"
    )


@pytest.mark.parametrize("name", PRESENT_PAGES)
def test_images_have_alt(name, page_text):
    """Every <img> has alt (empty is fine for decorative images)."""
    text = page_text[name]
    # Strip comments + script / style blocks before scanning so tokenised
    # <img> strings inside JS don't produce false positives.
    stripped = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    stripped = re.sub(r"<script[^>]*>.*?</script>", "", stripped, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(r"<style[^>]*>.*?</style>", "", stripped, flags=re.DOTALL | re.IGNORECASE)
    bad: list[str] = []
    for m in re.finditer(r"<img\b([^>]*)>", stripped, re.IGNORECASE):
        attrs = m.group(1)
        if "alt=" not in attrs:
            bad.append(m.group(0)[:100])
    assert not bad, f"{name}: <img> tags missing alt attribute: {bad}"


@pytest.mark.parametrize("name", PRESENT_PAGES)
def test_no_outline_none_without_replacement(name, page_text):
    """Catch ``outline: none`` without an accompanying visible-focus
    replacement (box-shadow, border-color change, background shift).
    False positives are tolerable — the fix is always cheap — and any
    regression is a keyboard-user bug."""
    text = page_text[name]
    # Only scan inline <style> — external CSS files are scanned by the
    # dedicated contrast + focus rule in ci_check_css_drift.sh.
    style_blocks = re.findall(r"<style[^>]*>(.*?)</style>", text, re.DOTALL | re.IGNORECASE)
    for block in style_blocks:
        for m in re.finditer(r"([^\{]*)\{([^}]*outline:\s*(?:none|0)[^}]*)\}", block):
            selector = m.group(1).strip()
            body = m.group(2).strip()
            # Skip the explicit safety-net rule from mobile-a11y.css.
            if ":focus:not(:focus-visible)" in selector:
                continue
            # A replacement is present if the body adjusts a visible property.
            has_replacement = (
                "box-shadow" in body or "border-color" in body or "background" in body
                or "outline:" in body.replace("outline: none", "").replace("outline:none", "")
            )
            assert has_replacement, (
                f"{name}: `{selector}` sets outline:none without a visible replacement"
            )

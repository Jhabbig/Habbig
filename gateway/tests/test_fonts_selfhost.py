"""Self-hosted fonts contract — closes audit LOW #2 (Google Fonts gap).

Two layers:

1. **Asset presence + middleware/CSS contents** — fast static checks that
   the woff2 files exist, that `tokens.css` declares the required
   `@font-face` rules, and that `pwa_middleware._PWA_HEAD` no longer
   reaches out to ``fonts.googleapis.com`` / ``fonts.gstatic.com``.

2. **Rendered prerelease.html** — the prerelease page is the highest-
   traffic public entry point and the audit specifically flagged it.
   Render it through the same PWA-injection middleware path as
   production and assert the body contains zero references to the
   Google Fonts CDN.

These tests use no external network and no headless browser, so they
run anywhere CI can run pytest.
"""

from __future__ import annotations

import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO = pathlib.Path(__file__).resolve().parents[1]
STATIC = REPO / "static"
FONTS = STATIC / "fonts"


class TestFontFilesPresent(unittest.TestCase):
    """The self-hosted woff2 binaries exist and look like valid WOFF2."""

    EXPECTED = (
        "InstrumentSerif-Italic.woff2",
        "SourceSerif4-Variable.woff2",
        # Pre-existing fonts must not regress.
        "Inter-Variable-subset.woff2",
        "GeistMono-Variable.woff2",
    )

    def test_each_font_file_exists(self) -> None:
        for name in self.EXPECTED:
            with self.subTest(font=name):
                p = FONTS / name
                self.assertTrue(
                    p.is_file(),
                    f"missing self-hosted font: {p}",
                )

    def test_each_font_is_woff2(self) -> None:
        # WOFF2 magic number is 'wOF2' (0x77 0x4F 0x46 0x32). If a CDN
        # download silently returned an HTML error page, this catches it.
        for name in self.EXPECTED:
            with self.subTest(font=name):
                p = FONTS / name
                with p.open("rb") as fh:
                    head = fh.read(4)
                self.assertEqual(
                    head, b"wOF2",
                    f"{name}: not a WOFF2 file (got {head!r})",
                )

    def test_font_files_are_reasonably_sized(self) -> None:
        # Catches a 0-byte or HTML-error-page download that happened to
        # start with bytes that *look* like wOF2 magic. Variable fonts
        # for Source Serif 4 are ~120 KB; italic Instrument Serif latin
        # subset is ~15 KB. Anything under 5 KB is broken.
        for name in self.EXPECTED:
            with self.subTest(font=name):
                p = FONTS / name
                self.assertGreater(
                    p.stat().st_size, 5_000,
                    f"{name}: suspiciously small ({p.stat().st_size} bytes)",
                )


class TestTokensCSSDeclaresFontFaces(unittest.TestCase):
    """tokens.css contains the @font-face rules the design system needs."""

    def setUp(self) -> None:
        self.css = (STATIC / "tokens.css").read_text()

    def test_instrument_serif_face_declared(self) -> None:
        self.assertIn('font-family: "Instrument Serif"', self.css)
        self.assertIn("InstrumentSerif-Italic.woff2", self.css)
        # Italic 400 cut (only variant used by --font-display).
        self.assertIn("font-style: italic", self.css)
        self.assertIn("font-weight: 400", self.css)

    def test_source_serif_4_face_declared(self) -> None:
        self.assertIn('font-family: "Source Serif 4"', self.css)
        self.assertIn("SourceSerif4-Variable.woff2", self.css)
        # Variable axis covers 200–900.
        self.assertIn("font-weight: 200 900", self.css)

    def test_font_display_swap_on_every_face(self) -> None:
        # font-display: swap is the contract — never block paint on font.
        # Three webfont faces: Geist Mono, Instrument Serif, Source Serif.
        # All three must opt into swap.
        self.assertGreaterEqual(
            self.css.count("font-display: swap"), 3,
            "Expected at least 3 'font-display: swap' declarations "
            "(Geist Mono + Instrument Serif + Source Serif 4)",
        )

    def test_local_fallback_first(self) -> None:
        # local() before url() means a user with the font installed
        # skips the network fetch entirely — a privacy + perf win.
        self.assertIn('local("Instrument Serif Italic")', self.css)
        self.assertIn('local("Source Serif 4")', self.css)


class TestPWAMiddlewareNoGoogleFonts(unittest.TestCase):
    """The PWA head injection no longer reaches out to Google Fonts."""

    def test_no_google_fonts_strings_in_pwa_head(self) -> None:
        from pwa_middleware import _PWA_HEAD
        self.assertNotIn("fonts.googleapis.com", _PWA_HEAD)
        self.assertNotIn("fonts.gstatic.com", _PWA_HEAD)

    def test_no_google_fonts_strings_in_middleware_source(self) -> None:
        # Belt-and-suspenders: even constants that aren't in _PWA_HEAD
        # shouldn't reintroduce the CDN. (Catches a future "let's add
        # a preconnect for performance" regression.)
        src = (REPO / "pwa_middleware.py").read_text()
        self.assertNotIn("fonts.googleapis.com", src)
        self.assertNotIn("fonts.gstatic.com", src)

    def test_inter_preload_still_works(self) -> None:
        # The pre-existing Inter preload mechanism (via per-page
        # templates incl. prerelease.html) must not have been broken.
        prerelease = (STATIC / "prerelease.html").read_text()
        self.assertIn(
            "Inter-Variable-subset.woff2",
            prerelease,
            "prerelease.html lost its Inter preload",
        )

    def test_self_hosted_fonts_preloaded(self) -> None:
        # Now that we self-host, the middleware should preload both
        # files so the editorial cuts hit first paint.
        from pwa_middleware import _PWA_HEAD
        self.assertIn("InstrumentSerif-Italic.woff2", _PWA_HEAD)
        self.assertIn("SourceSerif4-Variable.woff2", _PWA_HEAD)


class TestRenderedPrereleaseHasNoGoogleFonts(unittest.TestCase):
    """End-to-end: simulate the PWA injection on prerelease.html and
    assert the resulting bytes contain no Google Fonts URLs."""

    def test_injected_prerelease_has_no_google_fonts(self) -> None:
        from pwa_middleware import _inject_into_html
        raw = (STATIC / "prerelease.html").read_bytes()
        injected = _inject_into_html(raw, host="narve.ai")
        self.assertNotIn(b"fonts.googleapis.com", injected)
        self.assertNotIn(b"fonts.gstatic.com", injected)


if __name__ == "__main__":
    unittest.main()

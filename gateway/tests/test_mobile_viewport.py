"""Mobile-viewport regression guards.

Two layers of coverage:

1. **Static checks** (always run) — assert that the CSS + middleware
   that drive the mobile layout are present and well-formed:
     * `mobile-a11y.css` declares the hamburger / drawer / backdrop
     * `pwa_middleware._BODY_INJECT` actually injects the hamburger
     * `narve-app.js` wires `initSidebarDrawer`
     * Every template referenced in the audit either has tables
       wrapped in ``.nv-table-wrap`` or has zero tables.

2. **Headless-browser checks** (skipped when `playwright` is not
   installed) — render every canonical page at 375×812 and assert
   ``documentElement.scrollWidth <= clientWidth`` so no horizontal
   scrollbar appears.

The static layer catches drift even on hosts that can't run a
headless Chromium (CI without browsers, the locked-down audit host).
The browser layer catches genuine layout regressions when available.
"""

from __future__ import annotations

import pathlib
import unittest

USES_TESTDB = True

REPO = pathlib.Path(__file__).resolve().parents[1]
STATIC = REPO / "static"


class TestMobileCSS(unittest.TestCase):
    """The mobile-a11y.css contract — anything below MUST stay present."""

    def setUp(self) -> None:
        self.css = (STATIC / "mobile-a11y.css").read_text()

    def test_hamburger_class_styled(self) -> None:
        # The button itself is injected by pwa_middleware; CSS must size
        # it to 44×44 and position it fixed top-left.
        self.assertIn(".narve-hamburger", self.css)
        self.assertIn("width: 44px", self.css)
        self.assertIn("height: 44px", self.css)

    def test_sidebar_drawer_translate(self) -> None:
        # The drawer animation is `transform: translateX`. If a future
        # refactor accidentally drops the rule, mobile users lose sidebar
        # access entirely.
        self.assertIn("translateX(-100%)", self.css)
        self.assertIn(".sidebar.open,", self.css)
        self.assertIn(".narve-sidebar-backdrop", self.css)

    def test_table_wrap_pattern(self) -> None:
        self.assertIn(".nv-table-wrap", self.css)
        self.assertIn("overflow-x: auto", self.css)

    def test_input_min_font_size(self) -> None:
        # Defeats iOS auto-zoom: every <input> on mobile renders at
        # ≥16px. If this breaks, inputs zoom on focus and break the
        # mobile layout.
        self.assertIn("max(16px,", self.css)
        self.assertIn("body input", self.css)

    def test_tap_target_min_44(self) -> None:
        # Buttons and link-styled buttons get 44×44 floor on mobile.
        self.assertIn("min-height: 44px", self.css)
        self.assertIn("min-width: 44px", self.css)

    def test_safe_area_inset_bottom(self) -> None:
        # iPhone home-indicator clearance.
        self.assertIn("env(safe-area-inset-bottom", self.css)

    def test_bottom_sheet_pattern(self) -> None:
        # Dropdowns become full-width sheets on phones.
        self.assertIn("border-radius: 14px 14px 0 0", self.css)


class TestPWAMiddlewareInjects(unittest.TestCase):
    def test_hamburger_button_injected(self) -> None:
        from pwa_middleware import _BODY_INJECT
        self.assertIn("data-narve-hamburger", _BODY_INJECT)
        self.assertIn("aria-label=\"Open menu\"", _BODY_INJECT)
        self.assertIn("aria-expanded=\"false\"", _BODY_INJECT)
        self.assertIn("aria-controls=\"narve-sidebar-drawer\"", _BODY_INJECT)

    def test_backdrop_injected(self) -> None:
        from pwa_middleware import _BODY_INJECT
        self.assertIn("data-narve-sidebar-backdrop", _BODY_INJECT)


class TestNarveAppDrawerWiring(unittest.TestCase):
    def test_init_sidebar_drawer_present(self) -> None:
        js = (STATIC / "narve-app.js").read_text()
        self.assertIn("function initSidebarDrawer()", js)
        # The toggle path must close on Escape, backdrop click, and
        # nav-link click — losing any of these is a UX regression.
        self.assertIn("data-narve-hamburger", js)
        self.assertIn("data-narve-sidebar-backdrop", js)
        self.assertIn("'Escape'", js)


class TestHTMLTablesWrapped(unittest.TestCase):
    """Pages flagged by the AUDIT keep their tables wrapped in nv-table-wrap.

    A regression here would let a wide table push the mobile body wider
    than the viewport and re-introduce horizontal scroll.
    """

    PAGES_WITH_WRAPPED_TABLES = (
        "privacy.html",
        "dpa.html",
        "pricing.html",
    )

    def test_each_page_wraps_every_table(self) -> None:
        # Allow either a bare wrapper or one that adds keyboard-scroll
        # affordances (e.g. tabindex="0"). The lint only cares that the
        # wrapper is there before each <table>.
        import re
        wrap_pat = re.compile(r'<div\s+class="nv-table-wrap"(?:\s[^>]*)?>')
        for fname in self.PAGES_WITH_WRAPPED_TABLES:
            with self.subTest(page=fname):
                src = (STATIC / fname).read_text()
                table_open_count = src.count("<table")
                wrap_count = len(wrap_pat.findall(src))
                self.assertEqual(
                    wrap_count, table_open_count,
                    f"{fname}: {table_open_count} <table>(s) but only "
                    f"{wrap_count} nv-table-wrap wrapper(s)",
                )


# ── Optional: live headless-browser test ──────────────────────────────
# Only runs if playwright is installed AND a server is reachable on
# localhost:7100 (set NARVE_MOBILE_TEST_BASE to override).

import os  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

try:
    from playwright.sync_api import sync_playwright  # type: ignore
    _HAVE_PLAYWRIGHT = True
except ImportError:  # pragma: no cover
    _HAVE_PLAYWRIGHT = False


_BASE = os.environ.get("NARVE_MOBILE_TEST_BASE", "http://127.0.0.1:7100")


def _server_reachable() -> bool:
    try:
        urllib.request.urlopen(_BASE + "/", timeout=2)  # noqa: S310
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


@unittest.skipUnless(
    _HAVE_PLAYWRIGHT and _server_reachable(),
    "playwright not installed or local server not running",
)
class TestNoHorizontalScroll(unittest.TestCase):
    PAGES = (
        "/", "/gate", "/landing", "/pricing", "/terms", "/privacy", "/dpa",
        "/status", "/dashboards", "/billing", "/profile", "/settings",
        "/saved", "/feedback", "/admin", "/admin/jobs",
    )

    def test_no_hscroll_at_375(self) -> None:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(
                viewport={"width": 375, "height": 812},
                reduced_motion="reduce",
                service_workers="block",
            )
            for path in self.PAGES:
                with self.subTest(path=path):
                    page = context.new_page()
                    try:
                        resp = page.goto(_BASE + path, wait_until="networkidle", timeout=8000)
                    except Exception as exc:
                        self.skipTest(f"network error on {path}: {exc}")
                    if not resp or resp.status >= 400:
                        page.close()
                        continue
                    page.wait_for_timeout(120)
                    metrics = page.evaluate(
                        "() => ({"
                        " vw: window.innerWidth,"
                        " docW: document.documentElement.scrollWidth,"
                        " clientW: document.documentElement.clientWidth"
                        "})"
                    )
                    self.assertLessEqual(
                        metrics["docW"], metrics["clientW"] + 1,
                        f"{path}: documentElement.scrollWidth "
                        f"{metrics['docW']} > clientWidth {metrics['clientW']}",
                    )
                    page.close()
            browser.close()


if __name__ == "__main__":
    unittest.main()

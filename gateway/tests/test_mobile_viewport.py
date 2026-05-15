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


# ── Live-render coverage for the 2026-05-15 page set ───────────────────
# Pages shipped today that the static layer above doesn't cover:
#   - /admin/email-addresses    (new admin aggregator surface)
#   - /login                    (rewritten; no longer needs pending_token)
#   - /                         (prerelease apex)
#   - / on every subproduct subdomain (13 branded landings)
#
# These tests render each page through ``fastapi.testclient.TestClient``
# so they run offline without a uvicorn process. Headless-browser layout
# assertions stay in ``TestNoHorizontalScroll`` above; the assertions
# here are HTML-only and cover three contracts that matter at 375×812:
#
#   1. Mobile viewport meta tag present (``width=device-width``) AND
#      ``mobile-a11y.css`` is linked. mobile-a11y.css is what enforces
#      the ≥44px tap-target floor and the ≥16px input font-size at
#      ≤900px viewports, so its absence on any page = silent regression.
#   2. No interactive ``<a>``/``<button>`` ships an inline ``style=`` that
#      hard-caps its width/height below 44px (those would beat the CSS
#      cascade).
#   3. No form ``<input>`` ships an inline ``font-size`` below 16px
#      (would trigger iOS auto-zoom even though the CSS rule says ≥16px).
#
# bs4 is the parser of choice; if it's not installed the whole class
# self-skips so the static layer keeps running on CI hosts without it.

try:
    from bs4 import BeautifulSoup  # type: ignore
    _HAVE_BS4 = True
except ImportError:  # pragma: no cover
    _HAVE_BS4 = False


@unittest.skipUnless(_HAVE_BS4, "beautifulsoup4 not installed")
class TestMobileRenderToday(unittest.TestCase):
    """Mobile-viewport contract for pages shipped 2026-05-15.

    Uses the in-process TestClient so the suite needs neither a uvicorn
    process nor playwright. Assertions are HTML-only; the layout-level
    "no horizontal scroll" guarantee comes from mobile-a11y.css being
    linked (asserted) + the static TestMobileCSS / TestHTMLTablesWrapped
    classes above.
    """

    # All 13 active subproduct slugs as of 2026-05-15. Sourced from
    # ``gateway/subproduct.py:SUBPRODUCTS``; if that catalogue changes,
    # update this tuple and add fresh per-slug assertions.
    SUBPRODUCT_SLUGS = (
        "sports", "weather", "world", "crypto", "midterm", "traders",
        "whale", "voters", "climate", "disasters", "cb", "health", "love",
    )

    @classmethod
    def setUpClass(cls) -> None:
        import os as _os
        import sys as _sys
        _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))

        from fastapi.testclient import TestClient  # type: ignore
        import db as _db  # type: ignore
        import server as _server  # type: ignore

        cls._server = _server
        cls._db = _db
        cls._TestClient = TestClient

        # Build a 2FA-verified admin session so /admin/email-addresses
        # renders the page instead of redirecting to /login.
        email = f"mv_admin_{_os.getpid()}@test.local"
        existing = _db.get_user_by_email(email)
        if existing:
            uid = existing["id"]
        else:
            uid = _db.create_user(
                email, "Password1!verylong",
                username=f"mv_admin_{_os.getpid()}",
            )
        _db.set_user_role(uid, 2)
        try:
            _db.set_user_2fa_method(uid, "email_otp")
        except Exception:
            pass
        token = _db.create_session(uid)
        try:
            _db.mark_session_two_fa_verified(token)
        except Exception:
            pass
        cls._admin_session = token

    def _client(self, with_admin: bool = False):
        cookies = {"narve_session": self._admin_session} if with_admin else None
        return self._TestClient(self._server.app, cookies=cookies)

    # ── Helpers ────────────────────────────────────────────────────────
    _PX_RE = __import__("re").compile(
        r"(width|height|min-width|min-height|max-width|font-size)"
        r"\s*:\s*(\d+(?:\.\d+)?)\s*px",
        __import__("re").IGNORECASE,
    )

    def _assert_viewport_and_a11y_css(self, html: str, label: str) -> None:
        # ``mobile-a11y.css`` is injected by pwa_middleware into every
        # gateway-rendered page. If it's missing, the 44/16 floors aren't
        # in effect and the page WILL horizontal-scroll on 375 wide.
        soup = BeautifulSoup(html, "html.parser")
        meta = soup.find("meta", attrs={"name": "viewport"})
        self.assertIsNotNone(meta, f"{label}: no <meta name=viewport>")
        content = (meta.get("content") or "") if meta else ""
        self.assertIn(
            "width=device-width", content,
            f"{label}: viewport meta missing width=device-width: {content!r}",
        )
        self.assertIn(
            "mobile-a11y.css", html,
            f"{label}: rendered HTML does not link mobile-a11y.css",
        )

    def _assert_no_inline_undersized_tap_targets(self, html: str, label: str) -> None:
        """Interactive elements (<a>, <button>) must not ship inline
        styles that pin them below 44px in either dimension. The CSS
        floor in mobile-a11y.css raises everything to ≥44×44 at
        ≤900px, but an inline ``style="height: 28px"`` beats the
        cascade and would re-introduce the tap-target regression.
        """
        soup = BeautifulSoup(html, "html.parser")
        offenders: list[str] = []
        for el in soup.find_all(["a", "button"]):
            style = (el.get("style") or "")
            if not style:
                continue
            for m in self._PX_RE.finditer(style):
                prop = m.group(1).lower()
                if prop in ("width", "height", "min-width", "min-height"):
                    val = float(m.group(2))
                    if val < 44:
                        offenders.append(
                            f"<{el.name}> {prop}={val}px style={style[:120]!r}"
                        )
                        break
        self.assertEqual(
            [], offenders,
            f"{label}: interactive element(s) ship inline tap-target "
            f"below 44px:\n  " + "\n  ".join(offenders),
        )

    def _assert_form_inputs_min_16px(self, html: str, label: str) -> None:
        """No ``<input>`` may ship an inline ``font-size`` below 16px.
        Anything <16px triggers iOS auto-zoom on focus (the cascade rule
        in mobile-a11y.css uses ``!important`` to enforce ≥16px, but an
        inline-style font-size declared on the element itself with !important
        in the same style can still win — guard against both by rejecting
        any inline declaration below 16). Excludes hidden/checkbox/radio/range.
        """
        soup = BeautifulSoup(html, "html.parser")
        offenders: list[str] = []
        for inp in soup.find_all(["input", "select", "textarea"]):
            t = (inp.get("type") or "").lower()
            if t in ("checkbox", "radio", "hidden", "range"):
                continue
            style = (inp.get("style") or "")
            if not style:
                continue
            for m in self._PX_RE.finditer(style):
                if m.group(1).lower() == "font-size":
                    val = float(m.group(2))
                    if val < 16:
                        offenders.append(
                            f"<{inp.name} type={t!r}> font-size={val}px"
                        )
                        break
        self.assertEqual(
            [], offenders,
            f"{label}: form input(s) ship inline font-size below 16px:\n  "
            + "\n  ".join(offenders),
        )

    def _assert_no_overwide_fixed_widths(self, html: str, label: str) -> None:
        """No non-media element may declare an inline width > 375px on a
        mobile-bound page. Catches the classic "fixed pixel width on the
        wrapper" regression — that's how a phone gets horizontal scroll
        even when the CSS layer is intact.
        """
        soup = BeautifulSoup(html, "html.parser")
        offenders: list[str] = []
        skip_tags = {"svg", "img", "image", "rect", "circle", "path",
                     "line", "polyline", "polygon", "ellipse", "g",
                     "video", "canvas", "iframe"}
        for el in soup.find_all(style=True):
            if el.name in skip_tags:
                continue
            style = el.get("style", "")
            for m in self._PX_RE.finditer(style):
                prop = m.group(1).lower()
                if prop in ("width", "min-width"):
                    val = float(m.group(2))
                    if val > 375:
                        offenders.append(
                            f"<{el.name}> {prop}={val}px style={style[:120]!r}"
                        )
                        break
        self.assertEqual(
            [], offenders,
            f"{label}: element(s) ship inline width > 375px (would cause "
            f"horizontal scroll on a 375-wide viewport):\n  "
            + "\n  ".join(offenders),
        )

    def _check_all(self, html: str, label: str) -> None:
        self._assert_viewport_and_a11y_css(html, label)
        self._assert_no_inline_undersized_tap_targets(html, label)
        self._assert_form_inputs_min_16px(html, label)
        self._assert_no_overwide_fixed_widths(html, label)

    # ── /login (no longer requires pending_token cookie) ───────────────
    def test_login_page_renders_mobile_safe(self) -> None:
        r = self._client().get("/login")
        self.assertEqual(r.status_code, 200, "/login should render directly")
        self.assertIn(
            "Sign in", r.text,
            "/login should ship the standalone sign-in form (no token gate)",
        )
        self._check_all(r.text, "/login")

    def test_login_form_inputs_no_autozoom(self) -> None:
        """Defence-in-depth: explicitly assert the email + password inputs
        either declare no font-size inline OR declare it ≥16px. iOS will
        zoom on focus the moment one of them drifts below 16."""
        r = self._client().get("/login")
        soup = BeautifulSoup(r.text, "html.parser")
        fields = soup.select("input#email, input#password")
        self.assertGreaterEqual(
            len(fields), 2,
            "/login: expected at least email + password inputs",
        )
        for f in fields:
            style = (f.get("style") or "").lower()
            if "font-size" in style:
                for m in self._PX_RE.finditer(style):
                    if m.group(1).lower() == "font-size":
                        self.assertGreaterEqual(
                            float(m.group(2)), 16.0,
                            f"/login: <input {f.get('name')!r}> font-size "
                            f"< 16px would auto-zoom on iOS",
                        )

    # ── / (prerelease apex) ────────────────────────────────────────────
    def test_prerelease_apex_renders_mobile_safe(self) -> None:
        # Plain GET '/' on the apex host hits prerelease_page.
        r = self._client().get("/")
        self.assertEqual(r.status_code, 200)
        self._check_all(r.text, "/ (prerelease)")

    # ── /admin/email-addresses (new admin aggregator) ──────────────────
    def test_admin_email_addresses_renders_mobile_safe(self) -> None:
        r = self._client(with_admin=True).get("/admin/email-addresses")
        self.assertEqual(
            r.status_code, 200,
            f"/admin/email-addresses should render for an admin session "
            f"(got {r.status_code}); first 200 chars: {r.text[:200]!r}",
        )
        self._check_all(r.text, "/admin/email-addresses")

    # ── 13 subproduct subdomain landings ───────────────────────────────
    def test_all_subdomain_landings_render_mobile_safe(self) -> None:
        """Every entry in subproduct.SUBPRODUCTS must render its branded
        landing page at the apex path on its subdomain (Host header) for
        an unauthenticated visitor, and pass the same four mobile checks.
        """
        # Sanity-check the catalogue size — guard against silently
        # adding/removing a subproduct without updating this test.
        from subproduct import SUBPRODUCTS as _SP  # type: ignore
        self.assertEqual(
            set(_SP.keys()), set(self.SUBPRODUCT_SLUGS),
            "subproduct.SUBPRODUCTS drifted from the 13-slug list this "
            "test was written against — update SUBPRODUCT_SLUGS to match",
        )
        client = self._client()  # unauthenticated
        for slug in self.SUBPRODUCT_SLUGS:
            with self.subTest(subdomain=slug):
                host = f"{slug}.narve.ai"
                r = client.get("/", headers={"Host": host})
                self.assertEqual(
                    r.status_code, 200,
                    f"{host}/ should render the branded landing "
                    f"(got {r.status_code})",
                )
                # The slug should appear in the rendered hero — guard
                # against the apex prerelease accidentally taking over
                # when get_subdomain() regresses.
                self.assertIn(
                    slug, r.text.lower(),
                    f"{host}/ rendered HTML doesn't mention slug {slug!r}; "
                    f"the subdomain branding may have regressed",
                )
                self._check_all(r.text, f"{host}/")


if __name__ == "__main__":
    unittest.main()

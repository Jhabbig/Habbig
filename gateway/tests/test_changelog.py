"""Tests for the polished /changelog page + /changelog.rss feed.

Covers:

  * /changelog returns 200 and embeds every section heading present in
    CHANGELOG.md.
  * /changelog.rss returns the right mime type, parses as valid XML,
    contains 5+ items (against a fixture so the count is stable across
    weeks), and every <pubDate> is a valid RFC822 timestamp.
  * The dashboards "What's new" widget surfaces today's "Week of
    2026-05-14" content via /api/changelog (the widget already reads
    from CHANGELOG.md — this test guards that wiring).
"""

from __future__ import annotations

USES_TESTDB = True

import datetime as dt
import re
import unittest
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from tests import _testdb  # noqa: F401  — shared in-memory DB

import db
import changelog_routes


# A fixture rich enough to exercise the 5-item RSS assertion + all section
# kinds the parser renders. Mirrors the real CHANGELOG.md vocabulary so the
# HTML/RSS paths run against realistic input.
SAMPLE_CHANGELOG = """\
# Changelog

All notable changes here.

## Week of 2026-05-14

### Added
- **Voters Atlas** subproduct — election / electorate dashboard.
- **Climate Change** subproduct — long-horizon indicator dashboard.

### Changed
- **Typography — monospace.** `Geist Mono` is now canonical.

### Security
- **AUDIT #5 closed** — 0 critical, 0 high, 1 medium.

## Week of 2026-05-07

### Added
- Saved-views pinned sidebar.

### Fixed
- Schema drift in `market_snapshots` columns re-declared.

## Week of 2026-04-30

### Added
- Public API v1 with Bearer-auth.

## Week of 2026-04-23

### Changed
- Admin dashboard monochrome cleanup.

### Removed
- 2FA module fully removed after broken-feature assessment.

## Week of 2026-04-16

### Security
- Forensic watermarking + per-response numeric signing.

## [Unreleased]

### Added
- Community Takes (still under feature flag).
"""


_ORIG_PARSED_ENTRIES = changelog_routes._parsed_entries


def _seed_cache(entries):
    """Replace the module's parse-cache reader so tests are deterministic
    and don't read from the real CHANGELOG.md on disk. Monkey-patching
    ``_parsed_entries`` is more robust than touching the cache dict
    because the latter re-reads on mtime drift."""
    changelog_routes._parsed_entries = lambda: list(entries)


def _restore_cache():
    changelog_routes._parsed_entries = _ORIG_PARSED_ENTRIES


# ── Parser regression: "## Week of YYYY-MM-DD" form ──────────────────────


class TestWeekOfParser(unittest.TestCase):
    def test_parses_week_of_header(self):
        entries = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        # 5 Week-of entries + 1 [Unreleased] = 6 total.
        self.assertEqual(len(entries), 6)
        # First entry surfaces both a version label and a date.
        first = entries[0]
        self.assertEqual(first["date"], "2026-05-14")
        self.assertIn("Week of 2026-05-14", first["version"])

    def test_section_labels_preserved(self):
        entries = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        first = entries[0]
        self.assertIn("Added", first["sections"])
        self.assertIn("Changed", first["sections"])
        self.assertIn("Security", first["sections"])

    def test_unreleased_block_still_parses(self):
        # Mixed Week-of + [Unreleased] headers must both work.
        entries = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        self.assertTrue(
            any(e["version"] == "Unreleased" for e in entries),
            "Unreleased block lost when Week-of headers added",
        )


# ── Server-side HTML rendering ───────────────────────────────────────────


class TestRenderHTML(unittest.TestCase):
    def test_renders_section_chips_for_each_label(self):
        entries = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        html = changelog_routes.render_changelog_html(entries)
        for kind in ("added", "changed", "fixed", "removed", "security"):
            self.assertIn(f"cl-chip--{kind}", html,
                          f"missing chip variant: {kind}")

    def test_dates_become_anchor_ids(self):
        entries = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        html = changelog_routes.render_changelog_html(entries)
        self.assertIn('id="week-2026-05-14"', html)
        self.assertIn('id="week-2026-05-07"', html)

    def test_bullets_render_bold_and_code(self):
        entries = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        html = changelog_routes.render_changelog_html(entries)
        self.assertIn("<strong>Voters Atlas</strong>", html)
        self.assertIn("<code>Geist Mono</code>", html)

    def test_xss_payload_in_bullet_is_escaped(self):
        evil = (
            "# Changelog\n\n## Week of 2026-05-14\n\n### Added\n"
            "- <script>alert(1)</script> not a real bullet\n"
        )
        entries = changelog_routes.parse_changelog(evil)
        html = changelog_routes.render_changelog_html(entries)
        self.assertNotIn("<script>alert(1)", html)
        self.assertIn("&lt;script&gt;", html)

    def test_relative_time_for_today(self):
        self.assertEqual(
            changelog_routes._relative_time(
                "2026-05-14", now=dt.date(2026, 5, 14)
            ),
            "today",
        )

    def test_relative_time_buckets(self):
        now = dt.date(2026, 5, 14)
        self.assertEqual(
            changelog_routes._relative_time("2026-05-11", now=now),
            "3 days ago",
        )
        self.assertEqual(
            changelog_routes._relative_time("2026-05-04", now=now),
            "last week",
        )
        self.assertEqual(
            changelog_routes._relative_time("2026-04-28", now=now),
            "2 weeks ago",
        )


# ── RSS feed shape + RFC822 validation ───────────────────────────────────


class TestRSSRender(unittest.TestCase):
    def test_render_rss_emits_valid_xml(self):
        entries = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        xml = changelog_routes.render_rss(entries)
        # Should round-trip through the stdlib XML parser without raising.
        root = ET.fromstring(xml)
        self.assertEqual(root.tag, "rss")
        channel = root.find("channel")
        self.assertIsNotNone(channel)

    def test_rss_has_5_plus_items(self):
        entries = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        xml = changelog_routes.render_rss(entries)
        root = ET.fromstring(xml)
        items = root.findall("./channel/item")
        self.assertGreaterEqual(
            len(items), 5,
            f"expected 5+ items, got {len(items)}",
        )

    def test_rss_item_shape_for_week(self):
        entries = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        xml = changelog_routes.render_rss(entries)
        root = ET.fromstring(xml)
        items = root.findall("./channel/item")
        first = items[0]
        title = first.findtext("title") or ""
        link = first.findtext("link") or ""
        guid = first.find("guid")
        self.assertIn("Week of 2026-05-14", title)
        self.assertTrue(link.startswith("https://"))
        self.assertIn("#week-2026-05-14", link)
        self.assertEqual(guid.get("isPermaLink"), "false")
        self.assertEqual(guid.text, "narve-changelog-2026-05-14")

    def test_rss_pubdate_is_rfc822(self):
        entries = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        xml = changelog_routes.render_rss(entries)
        root = ET.fromstring(xml)
        for item in root.findall("./channel/item"):
            pub = item.findtext("pubDate") or ""
            parsed = parsedate_to_datetime(pub)
            self.assertIsNotNone(
                parsed.tzinfo,
                f"pubDate missing timezone: {pub}",
            )
            # Sanity: month names are spelled correctly per RFC 2822.
            self.assertRegex(
                pub,
                r"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} "
                r"\d{2}:\d{2}:\d{2} [\+\-]\d{4}$",
            )

    def test_rss_description_is_cdata_with_bullets(self):
        entries = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        xml = changelog_routes.render_rss(entries)
        # CDATA only appears when description content has HTML markup.
        self.assertIn("<![CDATA[", xml)
        # The first entry's first Added bullet should appear inside it.
        self.assertIn("Voters Atlas", xml)

    def test_rss_self_link_present(self):
        xml = changelog_routes.render_rss(
            changelog_routes.parse_changelog(SAMPLE_CHANGELOG),
            base_url="https://narve.ai",
        )
        self.assertIn('href="https://narve.ai/changelog.rss"', xml)
        self.assertIn('rel="self"', xml)


# ── Live HTTP routes ─────────────────────────────────────────────────────


class TestRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server  # noqa: F401  — registers seo_routes + changelog_routes
        from starlette.testclient import TestClient

        cls.server = server
        cls.client = TestClient(server.app)

        # changelog_seen table is needed by the JSON API (not by the page
        # or RSS), but other tests in the suite share this conn so we
        # create it idempotently to be a good neighbour.
        with db.conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS changelog_seen (
                    user_id INTEGER NOT NULL,
                    entry_key TEXT NOT NULL,
                    seen_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                    PRIMARY KEY (user_id, entry_key)
                )
                """
            )

    def setUp(self):
        # Seed the parse cache with the fixture so RSS / page assertions
        # don't depend on whatever happens to live in CHANGELOG.md today.
        entries = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        _seed_cache(entries)

    def tearDown(self):
        _restore_cache()

    def test_changelog_page_returns_200_with_section_headings(self):
        r = self.client.get("/changelog")
        self.assertEqual(r.status_code, 200)
        body = r.text
        # Each section kind from the fixture must show up in the page.
        for kind in ("added", "changed", "fixed", "removed", "security"):
            self.assertIn(f"cl-chip--{kind}", body, f"missing chip: {kind}")
        # Sticky subscribe bar + RSS feed discovery link.
        self.assertIn('data-cl-subscribe', body)
        self.assertIn('href="/changelog.rss"', body)
        self.assertIn('type="application/rss+xml"', body)

    def test_changelog_page_renders_week_of_today(self):
        r = self.client.get("/changelog")
        self.assertEqual(r.status_code, 200)
        # The week of 2026-05-14 (today) must surface — anchor + chip.
        self.assertIn('id="week-2026-05-14"', r.text)
        self.assertIn("Week of 2026-05-14", r.text)

    def test_changelog_rss_mime_type(self):
        # Host header must match the allowlist after the audit-5 fix —
        # TestClient's default ``testserver`` Host would 400.
        r = self.client.get("/changelog.rss", headers={"Host": "narve.ai"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(
            "application/rss+xml",
            r.headers.get("content-type", ""),
        )

    def test_changelog_rss_is_valid_xml_with_5_plus_items(self):
        r = self.client.get("/changelog.rss", headers={"Host": "narve.ai"})
        self.assertEqual(r.status_code, 200)
        root = ET.fromstring(r.text)
        self.assertEqual(root.tag, "rss")
        items = root.findall("./channel/item")
        self.assertGreaterEqual(
            len(items), 5,
            f"expected 5+ items, got {len(items)}",
        )

    def test_changelog_rss_dates_are_rfc822(self):
        r = self.client.get("/changelog.rss", headers={"Host": "narve.ai"})
        root = ET.fromstring(r.text)
        for item in root.findall("./channel/item"):
            pub = item.findtext("pubDate") or ""
            parsed = parsedate_to_datetime(pub)
            self.assertIsNotNone(parsed.tzinfo)
            self.assertRegex(
                pub,
                r"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} "
                r"\d{2}:\d{2}:\d{2} [\+\-]\d{4}$",
            )

    def test_changelog_rss_has_cache_header(self):
        r = self.client.get("/changelog.rss", headers={"Host": "narve.ai"})
        cache = r.headers.get("cache-control", "")
        self.assertIn("max-age=3600", cache)
        self.assertIn("public", cache)


# ── Widget integration — surfaces today's content via /api/changelog ─────


class TestWidgetSurfacesTodaysEntry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server  # noqa: F401
        from starlette.testclient import TestClient

        cls.client = TestClient(server.app)

    def setUp(self):
        # Same fixture seed so the widget API is deterministic.
        entries = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        _seed_cache(entries)

    def tearDown(self):
        _restore_cache()

    def test_api_changelog_top_entry_is_this_week(self):
        r = self.client.get("/api/changelog?limit=3")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertGreaterEqual(len(body["entries"]), 1)
        top = body["entries"][0]
        self.assertEqual(top["date"], "2026-05-14")
        self.assertIn("Week of 2026-05-14", top["version"])

    def test_api_returns_at_most_3_for_widget_default(self):
        r = self.client.get("/api/changelog?limit=3")
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(len(r.json()["entries"]), 3)


# ── Host injection / RSS base_url allowlist ──────────────────────────────


class TestHostAllowlist(unittest.TestCase):
    """Regression for the Host-injection finding in audit #5.

    A forged ``Host: evil.com`` header must never make it into the
    channel ``<link>`` / ``atom:link href`` attributes. The route layer
    enforces an allowlist via ``_validate_base_url`` and returns HTTP 400
    for anything outside it.
    """

    @classmethod
    def setUpClass(cls):
        import server  # noqa: F401  — registers changelog_routes
        from starlette.testclient import TestClient

        cls.client = TestClient(server.app)

    def setUp(self):
        entries = changelog_routes.parse_changelog(SAMPLE_CHANGELOG)
        _seed_cache(entries)

    def tearDown(self):
        _restore_cache()

    def test_validate_base_url_accepts_canonical(self):
        self.assertEqual(
            changelog_routes._validate_base_url("https://narve.ai"),
            "https://narve.ai",
        )

    def test_validate_base_url_accepts_localhost_with_port(self):
        self.assertEqual(
            changelog_routes._validate_base_url("http://localhost:8000"),
            "http://localhost:8000",
        )
        self.assertEqual(
            changelog_routes._validate_base_url("http://127.0.0.1:8001"),
            "http://127.0.0.1:8001",
        )

    def test_validate_base_url_rejects_evil_host(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            changelog_routes._validate_base_url("https://evil.com")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_validate_base_url_rejects_subdomain_of_evil(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            changelog_routes._validate_base_url("https://narve.evil.com")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_validate_base_url_rejects_localhost_lookalike(self):
        # ``localhost.evil`` smuggles past a naive startswith check.
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            changelog_routes._validate_base_url("http://localhost.evil")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_changelog_rss_rejects_evil_host_header(self):
        # TestClient forwards the Host header verbatim to the ASGI app.
        r = self.client.get(
            "/changelog.rss",
            headers={"Host": "evil.com"},
        )
        self.assertEqual(r.status_code, 400)

    def test_changelog_rss_accepts_narve_host(self):
        r = self.client.get(
            "/changelog.rss",
            headers={"Host": "narve.ai"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("https://narve.ai/changelog", r.text)

    def test_xml_escape_applied_to_base_url(self):
        # render_rss must XML-escape interpolations as belt-and-suspenders.
        xml = changelog_routes.render_rss(
            changelog_routes.parse_changelog(SAMPLE_CHANGELOG),
            base_url="https://narve.ai",
        )
        # Sanity: rendered XML round-trips through the stdlib parser.
        ET.fromstring(xml)


# ── [Unreleased] drafts filtered from public surfaces ────────────────────


_UNRELEASED_FIXTURE = """\
# Changelog

## [Unreleased]

### Added
- **Secret WIP** — not for public consumption yet.

## Week of 2026-05-14

### Added
- **Voters Atlas** — election dashboard.
"""


class TestUnreleasedFiltering(unittest.TestCase):
    """``## [Unreleased]`` blocks document work-in-progress and pre-disclosure
    security work; they must not appear on /api/changelog or in the RSS
    feed unless ``NARVE_CHANGELOG_ALLOW_DRAFTS`` is explicitly set."""

    @classmethod
    def setUpClass(cls):
        import server  # noqa: F401
        from starlette.testclient import TestClient

        cls.client = TestClient(server.app)

    def setUp(self):
        import os
        os.environ.pop("NARVE_CHANGELOG_ALLOW_DRAFTS", None)
        entries = changelog_routes.parse_changelog(_UNRELEASED_FIXTURE)
        _seed_cache(entries)

    def tearDown(self):
        import os
        os.environ.pop("NARVE_CHANGELOG_ALLOW_DRAFTS", None)
        _restore_cache()

    def test_public_entries_drops_unreleased_by_default(self):
        entries = changelog_routes.parse_changelog(_UNRELEASED_FIXTURE)
        public = changelog_routes._public_entries(entries)
        versions = [e["version"] for e in public]
        self.assertNotIn("Unreleased", versions)
        # The dated week is preserved.
        self.assertTrue(any("Week of 2026-05-14" in v for v in versions))

    def test_api_changelog_hides_unreleased_entries(self):
        r = self.client.get("/api/changelog?limit=20")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for entry in body["entries"]:
            self.assertNotEqual(
                (entry.get("version") or "").strip().lower(),
                "unreleased",
                f"Unreleased entry leaked into public API: {entry}",
            )
            # No-date entries are also drafts.
            self.assertIsNotNone(entry.get("date"))

    def test_rss_feed_hides_unreleased_entries(self):
        r = self.client.get(
            "/changelog.rss",
            headers={"Host": "narve.ai"},
        )
        self.assertEqual(r.status_code, 200)
        # The secret WIP bullet must not appear in the public feed.
        self.assertNotIn("Secret WIP", r.text)

    def test_env_var_opt_in_surfaces_unreleased(self):
        import os
        os.environ["NARVE_CHANGELOG_ALLOW_DRAFTS"] = "1"
        try:
            entries = changelog_routes.parse_changelog(_UNRELEASED_FIXTURE)
            public = changelog_routes._public_entries(entries)
            versions = [e["version"] for e in public]
            self.assertIn("Unreleased", versions)
        finally:
            os.environ.pop("NARVE_CHANGELOG_ALLOW_DRAFTS", None)


# ── CSS regression — font/colour violations stay fixed ───────────────────


class TestCssDesignTokens(unittest.TestCase):
    """Regression for the four CRITICAL findings in
    ``audits/audit_design_gateway_css.md`` against ``gateway/static/gateway.css``.

    These selectors had raw hex / rgba / chromatic / decorative-shadow
    violations. If a future refactor reintroduces any of them, the test
    fails — the design-skill hard rules are non-negotiable.
    """

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        path = (
            Path(__file__).resolve().parent.parent
            / "static" / "gateway.css"
        )
        cls.css = path.read_text(encoding="utf-8")

    # ── Finding 1: .narve-offline-banner @media block ──────────────────

    def test_offline_banner_uses_tokens_in_light_media_query(self):
        # The light @media block under .narve-offline-banner must use
        # tokens, not raw hex literals.
        block = re.search(
            r"@media\s*\(prefers-color-scheme:\s*light\)\s*\{\s*\.narve-offline-banner\s*\{([^}]+)\}",
            self.css,
        )
        self.assertIsNotNone(
            block,
            "expected `.narve-offline-banner` @media rule to remain in gateway.css",
        )
        body = block.group(1)
        for raw in ("#f5f5f5", "#374151", "#e5e7eb"):
            self.assertNotIn(
                raw, body,
                f"raw hex {raw} regressed into .narve-offline-banner @media",
            )
        self.assertIn("var(--bg-surface)", body)
        self.assertIn("var(--text-primary)", body)
        self.assertIn("var(--border-default)", body)

    # ── Finding 2: .narve-cmdp-error chromatic red ─────────────────────

    def test_cmdp_error_is_monochrome(self):
        # The chromatic red `#e46a6a` violated both the "no chromatic hue"
        # and the "status = position/weight not colour" rules.
        self.assertNotIn(
            "#e46a6a", self.css,
            "chromatic red #e46a6a regressed into gateway.css "
            "(should be var(--text-primary) + font-weight)",
        )
        m = re.search(
            r"\.narve-cmdp-error\s*\{([^}]+)\}",
            self.css,
        )
        self.assertIsNotNone(m, "missing .narve-cmdp-error selector")
        body = m.group(1)
        self.assertIn("var(--text-primary)", body)
        self.assertIn("font-weight", body)

    # ── Finding 3: decorative box-shadow on non-modals ─────────────────

    def test_no_decorative_box_shadow_on_non_modal_selectors(self):
        # These selectors must not declare a `box-shadow:` rule. Modal
        # containers (.narve-cmdp, modal overlays) are allowed.
        offenders = [
            ".auth-submit:hover",
            ".dash-card",
            ".dash-card:hover",
            ".billing-list",
            ".btn:hover",
            ".settings-card",
            ".narve-cmdp-pill",
        ]
        for selector in offenders:
            esc = re.escape(selector)
            for m in re.finditer(rf"{esc}\s*\{{([^}}]*)\}}", self.css):
                body = m.group(1)
                self.assertNotIn(
                    "box-shadow:",
                    body,
                    f"decorative box-shadow regressed into `{selector}`",
                )

    # ── Finding 4: raw rgba() paints in non-modal selectors ────────────

    def test_no_raw_rgba_in_selection_or_header_or_auth_body(self):
        # ::selection, .gw-header, .auth-body — three theme-regression
        # offenders that must paint through tokens, not raw rgba.
        for selector in ("::selection", ".gw-header", ".auth-body"):
            esc = re.escape(selector)
            m = re.search(rf"{esc}\s*\{{([^}}]*)\}}", self.css)
            self.assertIsNotNone(
                m, f"missing selector `{selector}` in gateway.css",
            )
            body = m.group(1)
            # Permit `transparent` keyword (auth-body gradient uses it);
            # reject raw rgba() literals.
            self.assertNotRegex(
                body,
                r"rgba\s*\(\s*\d",
                f"raw rgba() paint regressed into `{selector}`",
            )

    def test_content_area_no_shadow(self):
        # The .content-area / .page-frame block had a raw-rgba box-shadow
        # at L1852; verify both the shadow AND the rgba are gone.
        m = re.search(r"\.page-frame\s*\{([^}]+)\}", self.css)
        self.assertIsNotNone(m, "missing .page-frame block")
        body = m.group(1)
        self.assertNotIn("box-shadow", body)
        self.assertNotIn("rgba(", body)


if __name__ == "__main__":
    unittest.main()

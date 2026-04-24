"""Tests for the foundation bundle: OG routes, toast region, empty-state
helper, _base.html wrapping, meta-description coverage.

Each test asserts one promise of the foundation so a later session can
break any single promise and get a clean failure message pointing at
the regression.

Kept deliberately narrow — these exercise the shared infrastructure,
not the 99 individual pages that will migrate onto the base over
subsequent sessions.
"""

from __future__ import annotations

import glob
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import _testdb  # noqa: F401
import server  # noqa: E402


STATIC_DIR = Path(__file__).parent.parent / "static"


# ── OG card routes ───────────────────────────────────────────────────


class TestOgRoutes:
    def setup_method(self) -> None:
        from fastapi.testclient import TestClient
        self.client = TestClient(server.app)

    def test_og_default_returns_png(self):
        r = self.client.get("/og/default")
        assert r.status_code == 200, r.text
        assert r.headers.get("content-type") == "image/png"
        # PNGs start with the 8-byte magic 89 50 4E 47 0D 0A 1A 0A
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_og_pricing_returns_png(self):
        r = self.client.get("/og/pricing")
        assert r.status_code == 200
        assert r.headers.get("content-type") == "image/png"

    def test_og_calendar_returns_png(self):
        r = self.client.get("/og/calendar")
        assert r.status_code == 200

    def test_og_cache_headers_set_for_cdn(self):
        """Cache-Control should let Cloudflare + browsers hold onto the
        bytes for an hour with stale-while-revalidate beyond that."""
        r = self.client.get("/og/default")
        cc = r.headers.get("cache-control", "")
        assert "max-age=3600" in cc, cc
        assert "public" in cc, cc

    def test_og_unknown_source_returns_404(self):
        r = self.client.get("/og/source/__definitely_not_a_real_handle__")
        assert r.status_code == 404

    def test_og_unknown_market_returns_404(self):
        r = self.client.get("/og/market/__definitely_not_a_market__")
        assert r.status_code == 404

    def test_og_routes_are_public(self):
        """Gate middleware must let OG endpoints through so Twitter /
        Slack / Discord crawlers can fetch social previews even
        without the site-access cookie.
        """
        r = self.client.get("/og/default")
        # A 302 redirect to /gate would be the failure mode — the
        # response body would be empty HTML with a Location header.
        assert r.status_code == 200
        assert "image/png" in r.headers.get("content-type", "")


# ── render_empty helper ──────────────────────────────────────────────


class TestRenderEmpty:
    def test_basic_shape(self):
        html = server.render_empty(
            title="No predictions yet",
            body="Make your first call on any market.",
            actions=[
                {"label": "Browse markets", "href": "/markets", "primary": True},
            ],
        )
        assert 'class="nv-empty"' in html
        assert "No predictions yet" in html
        assert "Browse markets" in html
        assert "nv-empty__action--primary" in html
        assert 'href="/markets"' in html

    def test_escapes_untrusted_input(self):
        """Titles and bodies must HTML-escape so a future call site
        feeding user content into the empty-state helper can't turn
        it into an XSS vector."""
        html = server.render_empty(
            title="<script>alert(1)</script>",
            body="Hello & goodbye",
            actions=[{"label": '<img onerror="">', "href": "/"}],
        )
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html
        assert "&amp;" in html
        assert '<img onerror="">' not in html

    def test_multiple_actions(self):
        html = server.render_empty(
            title="x",
            body="y",
            actions=[
                {"label": "Primary", "href": "/a", "primary": True},
                {"label": "Secondary", "href": "/b"},
            ],
        )
        assert html.count("nv-empty__action") >= 2
        assert html.count("nv-empty__action--primary") == 1

    def test_missing_partial_falls_back(self, tmp_path, monkeypatch):
        """If the partial is missing, the helper must still return a
        well-formed empty state — a half-deployed server shouldn't
        500 on a missing file."""
        fake_static = tmp_path / "static"
        (fake_static / "_partials").mkdir(parents=True)
        monkeypatch.setattr(server, "STATIC_DIR", fake_static)
        html = server.render_empty(title="Stub", body="x")
        assert "nv-empty" in html
        assert "Stub" in html


# ── Base template + components CSS ───────────────────────────────────


class TestBaseTemplate:
    def test_base_html_present(self):
        assert (STATIC_DIR / "_base.html").exists()

    def test_components_css_present(self):
        assert (STATIC_DIR / "components.css").exists()

    def test_toast_js_present(self):
        assert (STATIC_DIR / "js" / "toast.js").exists()

    def test_base_exposes_required_slots(self):
        """Every migrated page template relies on these slot names —
        renaming any one would silently break the wrapping layer."""
        text = (STATIC_DIR / "_base.html").read_text()
        for slot in (
            "{{ title }}",
            "{{ meta_description }}",
            "{{ canonical_url }}",
            "{{ og_image }}",
            "{{ raw_content }}",
            "{{ raw_header }}",
            "{{ raw_footer }}",
            "{{ raw_page_scripts }}",
            "{{ raw_robots }}",
            "nv-toast-region",
            "skip-link",
        ):
            assert slot in text, f"_base.html missing slot: {slot!r}"

    def test_components_css_exposes_classes(self):
        css = (STATIC_DIR / "components.css").read_text()
        for cls in (
            ".nv-toast",
            ".nv-toast--enter",
            ".nv-toast--exit",
            ".nv-empty",
            ".nv-empty__title",
            ".nv-skel",
            ".skip-link",
        ):
            assert cls in css, f"components.css missing: {cls}"


# ── Toast JS contract ────────────────────────────────────────────────


class TestToastJs:
    def test_registers_globals(self):
        """The rest of the codebase already calls window.narveToast and
        window.narveToastError — both must exist."""
        js = (STATIC_DIR / "js" / "toast.js").read_text()
        assert "window.narveToast" in js
        assert "window.narveToastError" in js

    def test_no_alert_in_client_js(self):
        """Regression guard: every alert() call must go through the
        toast surface (or a narrow fallback chain that starts with
        narveToastError). Keeps alert() from creeping back in."""
        # Allowed in our own toast.js as the fallback reference.
        allowlist = {"toast.js"}
        offenders = []
        for p in sorted(glob.glob(str(STATIC_DIR / "*.js"))):
            name = os.path.basename(p)
            if name in allowlist:
                continue
            text = open(p).read()
            # Match bare alert(...) not preceded by a . (allows
            # window.alert fallback paths).
            for line in text.splitlines():
                stripped = line.lstrip()
                if stripped.startswith("//"):
                    continue
                if re.search(r"(?<![.\w])alert\s*\(", line):
                    offenders.append(f"{name}: {line.strip()[:100]}")
        assert not offenders, (
            "Bare alert() call found; use window.narveToast / "
            "narveToastError instead:\n" + "\n".join(offenders)
        )


# ── Meta-description coverage ────────────────────────────────────────


class TestMetaDescriptions:
    def test_every_public_page_has_meta_description(self):
        """Every HTML template (except the base + partials) must ship
        a meta description so social-share previews aren't blank."""
        missing = []
        for p in sorted(glob.glob(str(STATIC_DIR / "*.html"))):
            name = os.path.basename(p)
            if name.startswith("_"):
                continue
            text = open(p).read()
            if 'name="description"' not in text:
                missing.append(name)
        assert not missing, (
            f"{len(missing)} page(s) missing meta description:\n"
            + "\n".join(missing[:20])
        )


# ── Asset version consolidation ──────────────────────────────────────


class TestAssetVersioning:
    def test_no_hardcoded_gateway_css_version(self):
        """Every template should use the canonical {{ static: }} token
        so static_url() does the content-hash cache-bust — no more
        ?v=7 / ?v=8 drift."""
        offenders = []
        for p in sorted(glob.glob(str(STATIC_DIR / "*.html"))):
            name = os.path.basename(p)
            if name.startswith("_"):
                continue
            text = open(p).read()
            if re.search(r"gateway\.css\?v=\d+", text):
                offenders.append(name)
        assert not offenders, (
            f"{len(offenders)} page(s) still use hardcoded ?v=N on "
            f"gateway.css:\n" + "\n".join(offenders)
        )


# ── Stripe TODO cleanup ──────────────────────────────────────────────


class TestLandingTodoCleanup:
    def test_no_live_todos_in_landing(self):
        text = (STATIC_DIR / "landing.html").read_text()
        assert "TODO" not in text, (
            "landing.html still has a TODO comment — resolve or "
            "file a ticket + remove the marker."
        )


# ── Emoji chrome scrub ───────────────────────────────────────────────


class TestEmojiScrub:
    def test_no_emoji_in_page_chrome(self):
        """Only geometric-shape check/cross (U+2713 / U+2717) and the
        copy-to-clipboard glyph set are allowed — they're product
        indicators, not emojis. The Supplemental Symbols + SMP Emoji
        block must be empty across chrome."""
        import re
        # U+1F300 onward = emoji + pictographs; scrubbing this range
        # leaves the U+2600-U+27BF block alone, which holds our ✓/✗.
        pat = re.compile(r"[\U0001F300-\U0001FAFF]")
        offenders = []
        for p in sorted(glob.glob(str(STATIC_DIR / "*.html"))):
            name = os.path.basename(p)
            if name.startswith("_"):
                continue
            text = open(p).read()
            if pat.search(text):
                offenders.append(name)
        assert not offenders, (
            f"Emoji found in chrome of: {', '.join(offenders)}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

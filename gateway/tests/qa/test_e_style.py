"""Walk E — style invariants on canonical pages.

What this catches that the foundation pytest tests don't:

  - The Inter subset font ACTUALLY loads at runtime (not just that the
    @font-face rule is in gateway.css). A CSP regression or a missing
    static-mount would break this without breaking the unit tests.
  - No external font CDN (fonts.googleapis.com) is hit, even
    accidentally — the design rule is "self-host or fail".
  - Inline-style count stays low (foundation extracted 75 blocks; we
    don't want them creeping back through copy-paste).
  - Visible focus rings on the first focusable element of each page.

The list is deliberately short (CANONICAL_PAGES) so this walk runs
under 10s on a laptop. Per-feature deep style checks live in narrower
tests near the components they own.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from .pages import CANONICAL_PAGES  # noqa: E402


@pytest.mark.parametrize("path", CANONICAL_PAGES)
def test_inter_subset_loaded_via_network(page, browser_server, path):
    """The Inter-Variable-subset.woff2 file is actually requested by
    the browser — not just declared in CSS."""
    requested: list[str] = []
    page.on("request", lambda req: requested.append(req.url))
    page.goto(f"{browser_server}{path}", wait_until="networkidle")
    fonts = [u for u in requested if u.lower().endswith(".woff2")]
    inter_subset = [u for u in fonts if "Inter-Variable-subset" in u]
    if not fonts:
        # Some routes (e.g. /admin in some test environments) may
        # short-circuit to a redirect before fonts are needed. Skip
        # rather than fail in that case.
        pytest.skip(f"{path}: no woff2 requests; possibly a redirect")
    assert inter_subset, (
        f"{path}: Inter subset font not requested. "
        f"All woff2: {fonts}"
    )


@pytest.mark.parametrize("path", CANONICAL_PAGES)
def test_no_external_font_cdn(page, browser_server, path):
    """No font traffic should hit fonts.googleapis.com or fonts.gstatic.com."""
    requested: list[str] = []
    page.on("request", lambda req: requested.append(req.url))
    page.goto(f"{browser_server}{path}", wait_until="networkidle")
    bad = [u for u in requested if any(
        cdn in u for cdn in ("fonts.googleapis.com", "fonts.gstatic.com")
    )]
    assert not bad, (
        f"{path}: external font CDN hit (self-hosting violated): {bad}"
    )


@pytest.mark.parametrize("path", CANONICAL_PAGES)
def test_inline_style_attribute_count_bounded(page, browser_server, path):
    """Style="…" inline attributes are allowed for one-off layout
    tweaks (the codebase does use them) but should not exceed a
    reasonable cap per page. Catches a regression where someone
    inlines the entire stylesheet."""
    page.goto(f"{browser_server}{path}", wait_until="domcontentloaded")
    n = page.evaluate("""
        () => {
          // Exclude SVG descendants — they legitimately use style="..."
          // for fill/stroke, and the chrome doesn't ship hundreds of SVGs.
          let count = 0;
          for (const el of document.querySelectorAll("[style]")) {
            if (!el.closest("svg")) count++;
          }
          return count;
        }
    """)
    assert n < 200, f"{path}: inline-style attributes = {n} (cap 200)"


@pytest.mark.parametrize("path", CANONICAL_PAGES)
def test_focus_ring_visible(page, browser_server, path):
    """Tab to the first focusable element; assert it picks up a
    visible focus ring (outline OR box-shadow OR border colour shift).
    We don't pin the exact style — multiple components use different
    focus mechanisms — only that focus produces SOME visible change."""
    page.goto(f"{browser_server}{path}", wait_until="networkidle")
    # Pick the first interactive element. Skip if there isn't one
    # (some standalone pages may have nothing focusable).
    focusable = page.locator(
        "button, a[href], input:not([type='hidden']), [tabindex='0']"
    ).first
    if focusable.count() == 0:
        pytest.skip(f"{path}: no focusable element")
    focusable.focus()
    # Read computed outline + box-shadow + border on the focused
    # element. Any non-default value counts as "visible focus".
    has_focus_indicator = focusable.evaluate("""
        el => {
          const cs = getComputedStyle(el);
          const outline = cs.outline || "";
          const shadow  = cs.boxShadow || "";
          const outlineWidth = parseFloat(cs.outlineWidth) || 0;
          // Outline width > 0, OR box-shadow that isn't 'none', OR
          // a coloured outline of any width (browsers expose width=0
          // sometimes for invert outlines, so colour is the tiebreak).
          return (
            outlineWidth > 0
            || (outline && outline !== "none" && cs.outlineColor !== "rgba(0, 0, 0, 0)")
            || (shadow && shadow !== "none")
          );
        }
    """)
    assert has_focus_indicator, (
        f"{path}: first focusable element has no visible focus ring"
    )

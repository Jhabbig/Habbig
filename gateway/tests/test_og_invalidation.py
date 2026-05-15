"""Regression tests for the OG-card audit fixes (audit HIGH/MED, 2026-05-15).

Three audit findings — one test class each:

  A) ``on_market_resolved(slug)`` flushes data caches but never the
     OG card, so a freshly-resolved market keeps showing the pre-
     resolution narve-vs-market headline on Twitter/LinkedIn unfurls
     for up to an hour. (HIGH — public surface, hot path.)
  B) ``cache/ttl.py`` doc said ``og_card:market:{slug}`` ttl=600
     while ``og_routes.py`` used a flat 3600. Either is defensible,
     but the drift meant admins reading the doc undercounted the
     real exposure window 6×. (MED — operational.)
  C) ``og_cards._paste_logo`` opens ``LOGO_PATH`` straight off disk
     via ``Image.open`` without lowering ``MAX_IMAGE_PIXELS``. The
     avatar code path already does this; the OG path inherited the
     gap because it was built before the avatar guard landed. (MED —
     defence-in-depth; the asset is repo-controlled today but the
     guard is free and survives a future asset swap.)

Each class pins the contract the fix is supposed to enforce. If a
future refactor regresses any of them — e.g. a prefix typo in the
invalidation helper, a divergent constant sneaking back into
``og_routes.py``, an ``import`` re-ordering that re-raises the cap
before the module-top assignment — these tests will fail loudly.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Keep tests dep-free of REDIS so the async cache stays in its
# in-process fallback branch; the sync TTL cache (what we're really
# testing) is unaffected either way.
os.environ.pop("REDIS_URL", None)


# ─── A) Dynamic OG card invalidation ─────────────────────────────────────


class TestOgCardInvalidation(unittest.TestCase):
    """Resolving a market or recomputing credibility must bust the
    relevant ``og_card:*`` entries so social unfurls reflect the new
    state immediately. Without this, a card cached at T-0 is served
    until T+3600 even though the underlying numbers have changed.
    """

    def setUp(self):
        from cache import ttl_cache
        ttl_cache.clear()

    def test_on_market_resolved_drops_market_og_card(self):
        """Pin the HIGH-severity bug: the helper must remove the OG
        card key that ``og_routes.og_market`` populates. The actual
        full key in the cache is ``og_card:og:market:{slug}`` —
        ``og_cards.cached()`` prefixes ``og_card:`` onto the
        ``og:market:{slug}`` key the route passes in."""
        from cache import ttl_cache, ttl_invalidate

        slug = "will-fed-cut-rates-in-q3-2026"
        full_key = f"og_card:og:market:{slug}"

        ttl_cache.set(full_key, b"\x89PNG-stale-bytes", ttl_seconds=3600)
        self.assertIsNotNone(ttl_cache.get(full_key))

        ttl_invalidate.on_market_resolved(slug)
        self.assertIsNone(
            ttl_cache.get(full_key),
            "on_market_resolved must purge the og_card entry for the slug — "
            "audit HIGH 2026-05-15",
        )

    def test_on_market_resolved_uses_prefix_so_variants_get_purged(self):
        """If a future commit appends a suffix (e.g. ``:locale_en``,
        ``:v2``) to the card key, the prefix delete should still nuke
        it. Lock the contract as a prefix wipe, not a single-key
        delete, so the helper stays robust to renderer changes."""
        from cache import ttl_cache, ttl_invalidate

        slug = "trump-2028"
        base = f"og_card:og:market:{slug}"
        ttl_cache.set(base, b"a", ttl_seconds=3600)
        ttl_cache.set(f"{base}:locale_en", b"b", ttl_seconds=3600)
        ttl_cache.set(f"{base}:v2", b"c", ttl_seconds=3600)

        ttl_invalidate.on_market_resolved(slug)

        self.assertIsNone(ttl_cache.get(base))
        self.assertIsNone(ttl_cache.get(f"{base}:locale_en"))
        self.assertIsNone(ttl_cache.get(f"{base}:v2"))

    def test_on_market_resolved_leaves_unrelated_og_cards_alone(self):
        """Wiping a market's card must not accidentally take out
        another market's card or any source card. Prefix discipline
        means ``og_card:og:market:foo`` ≠ ``og_card:og:market:bar``."""
        from cache import ttl_cache, ttl_invalidate

        ttl_cache.set("og_card:og:market:foo", b"foo", ttl_seconds=3600)
        ttl_cache.set("og_card:og:market:bar", b"bar", ttl_seconds=3600)
        ttl_cache.set("og_card:og:source:fedwatcher", b"src", ttl_seconds=3600)
        ttl_cache.set("og_card:og:default", b"def", ttl_seconds=3600)

        ttl_invalidate.on_market_resolved("foo")

        self.assertIsNone(ttl_cache.get("og_card:og:market:foo"))
        self.assertEqual(ttl_cache.get("og_card:og:market:bar"), b"bar")
        self.assertEqual(ttl_cache.get("og_card:og:source:fedwatcher"), b"src")
        self.assertEqual(ttl_cache.get("og_card:og:default"), b"def")

    def test_on_credibility_recompute_drops_all_source_og_cards(self):
        """The nightly cron rescores every source — the headline
        credibility number on every source card may move. Wipe the
        whole ``og_card:og:source:*`` namespace rather than guess
        which handles changed."""
        from cache import ttl_cache, ttl_invalidate

        ttl_cache.set("og_card:og:source:fedwatcher", b"a", ttl_seconds=3600)
        ttl_cache.set("og_card:og:source:marketskeptic", b"b", ttl_seconds=3600)
        ttl_cache.set("og_card:og:source:julian", b"c", ttl_seconds=3600)
        # Market + default cards must survive — they don't display per-
        # source credibility and the cron didn't touch them.
        ttl_cache.set("og_card:og:market:foo", b"m", ttl_seconds=3600)
        ttl_cache.set("og_card:og:default", b"d", ttl_seconds=3600)

        ttl_invalidate.on_credibility_recompute()

        self.assertIsNone(ttl_cache.get("og_card:og:source:fedwatcher"))
        self.assertIsNone(ttl_cache.get("og_card:og:source:marketskeptic"))
        self.assertIsNone(ttl_cache.get("og_card:og:source:julian"))
        self.assertEqual(ttl_cache.get("og_card:og:market:foo"), b"m")
        self.assertEqual(ttl_cache.get("og_card:og:default"), b"d")


# ─── B) TTL drift between docs and runtime ───────────────────────────────


class TestOgCardTtlAlignment(unittest.TestCase):
    """The audit found the doc string in ``cache/ttl.py`` claimed
    ``og_card:market:{slug}`` ttl=600 while ``og_routes.py`` used a
    flat 3600. Fix collapsed the values to 3600 everywhere and made
    ``og_routes.py`` import the canonical value from ``DEFAULT_TTLS``
    so the two cannot drift again.
    """

    def test_default_ttls_og_card_is_3600(self):
        """The canonical TTL is now 3600s. If anyone lowers this back
        to 600 in the dict without also dropping the routes' Cache-
        Control max-age, edge caches will keep serving the stale card
        for longer than the in-process cache thinks it lives."""
        from cache import DEFAULT_TTLS
        self.assertEqual(DEFAULT_TTLS["og_card"], 3600)

    def test_og_routes_pulls_ttl_from_default_ttls(self):
        """Route file must not re-declare the TTL as a magic number —
        it should read from ``DEFAULT_TTLS``. Otherwise the drift the
        audit caught reappears the next time someone changes one
        constant in isolation."""
        from cache import DEFAULT_TTLS
        import og_routes
        self.assertEqual(og_routes._CACHE_TTL, DEFAULT_TTLS["og_card"])

    def test_cache_control_header_matches_default_ttl(self):
        """The public ``Cache-Control: public, max-age=N`` header
        should advertise the same N that we cache for. A mismatch
        means Cloudflare and the origin disagree on freshness — the
        symptom users see is "I resolved the market but Twitter still
        shows the old card for 50 minutes"."""
        from cache import DEFAULT_TTLS
        import og_routes
        expected = f"public, max-age={DEFAULT_TTLS['og_card']}, stale-while-revalidate=86400"
        self.assertEqual(og_routes._HEADERS["Cache-Control"], expected)


# ─── C) Pillow decompression-bomb guard on logo open ─────────────────────


try:
    from PIL import Image as _PIL_Image  # noqa: F401
    _PIL_OK = True
except Exception:
    _PIL_OK = False


@unittest.skipUnless(_PIL_OK, "Pillow not installed")
class TestOgCardsImagePixelsCap(unittest.TestCase):
    """``og_cards._paste_logo`` opens ``LOGO_PATH`` via ``Image.open``;
    Pillow's default ~89M-pixel cap only warns, never raises. The fix
    lowers ``Image.MAX_IMAGE_PIXELS`` to 16M at module import time so
    a future malicious-asset swap (or just an oversized logo upload
    flow) trips a hard ``DecompressionBombError`` rather than OOMing
    the worker mid-render.
    """

    def test_max_image_pixels_capped_at_16m_after_og_cards_import(self):
        """Pin the load-bearing side effect. Importing ``og_cards``
        must lower the global cap to 16,000,000. The avatar code
        path sets the same value — if the two ever diverge, document
        the reason; today they should match."""
        # Force a fresh import so we observe the module-load side
        # effect even when another test already imported og_cards.
        sys.modules.pop("og_cards", None)
        import og_cards  # noqa: F401 — side-effect import
        from PIL import Image

        self.assertEqual(
            Image.MAX_IMAGE_PIXELS,
            16_000_000,
            "og_cards must cap Image.MAX_IMAGE_PIXELS at 16M — audit MED "
            "2026-05-15. If you intentionally raised this, also raise the "
            "matching constant in profile_routes.py and document why.",
        )

    def test_decompression_bomb_raises_when_opening_oversized_png(self):
        """End-to-end: a hand-crafted PNG larger than 2× the cap
        causes Pillow to raise ``DecompressionBombError`` at
        ``Image.open(...).load()`` time, exactly the behaviour the
        ``_paste_logo`` helper now relies on for safety. We don't
        invoke ``_paste_logo`` directly (it logs+swallows on any
        exception by design — the OG card still renders without the
        logo) but we do assert the underlying Pillow contract that
        the cap enforces."""
        import io
        # Force re-import so the cap is definitely in place.
        sys.modules.pop("og_cards", None)
        import og_cards  # noqa: F401
        from PIL import Image

        # 6000×6000 = 36M > 2 × 16M, so Pillow raises (not just warns).
        big = Image.new("RGB", (6000, 6000), (0, 0, 0))
        buf = io.BytesIO()
        big.save(buf, format="PNG")
        buf.seek(0)

        with self.assertRaises(Image.DecompressionBombError):
            Image.open(buf).load()


if __name__ == "__main__":
    unittest.main()

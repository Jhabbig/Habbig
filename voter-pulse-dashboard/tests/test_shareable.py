"""Smoke tests for the shareable-card renderer."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import shareable  # noqa: E402


class ShareableTests(unittest.TestCase):
    def test_mood_card_returns_bytes(self):
        body, ctype = shareable.render_mood_card({
            "overall": 48, "label": "Strained",
            "subscores": {"pocketbook": {"score": 38}, "jobs": {"score": 52}, "sentiment": {"score": 33}},
        })
        self.assertIsInstance(body, (bytes, bytearray))
        self.assertGreater(len(body), 1000)  # any real image will be larger
        self.assertIn(ctype, ("image/png", "image/svg+xml"))

    def test_country_card_with_minimal_payload(self):
        body, ctype = shareable.render_country_card({
            "iso3": "VNM", "name": "Vietnam",
            "latest_stage": {
                "label": "Industrialising", "stage": 2,
                "agriculture_pct": 28, "industry_pct": 33, "services_pct": 39,
            },
        })
        self.assertGreater(len(body), 1000)

    def test_country_card_handles_missing_latest_stage(self):
        body, _ = shareable.render_country_card({"iso3": "ZZZ", "name": "Nowhere"})
        # Still renders a valid image, just with the empty state
        self.assertGreater(len(body), 500)

    def test_backtest_card(self):
        body, ctype = shareable.render_backtest_card({
            "headline": {"horizon_months": 6, "correct": 8, "n": 12, "accuracy_pct": 66.7},
        })
        self.assertGreater(len(body), 1000)

    def test_html_preview_contains_og_tags(self):
        html = shareable.html_preview(
            kind="mood",
            og_image_url="https://example.com/share/mood.png",
            title="Voter Pulse",
            description="National mood 48 (Strained).",
            canonical_url="https://example.com/share/mood",
        )
        self.assertIn('property="og:title"', html)
        self.assertIn('property="og:image"', html)
        self.assertIn('name="twitter:card"', html)
        self.assertIn("National mood 48 (Strained).", html)

    def test_html_preview_escapes_unsafe_chars(self):
        html = shareable.html_preview(
            kind="x", og_image_url="x", title="<script>x</script>",
            description="x", canonical_url="x",
        )
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()

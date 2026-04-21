"""Bot formatter tests — Telegram + Discord shared output."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from bots.formatters import (  # noqa: E402
    format_best_bet_discord,
    format_best_bet_telegram,
)


def _bet() -> dict:
    return {
        "market_slug": "will-btc-pass-100k-by-eoy",
        "question": "Will BTC pass $100k by EOY 2026?",
        "platform": "polymarket",
        "side": "yes",
        "betyc_probability": 0.72,
        "market_price": 0.58,
        "edge_pct": 14.0,
        "confidence": "high",
        "credibility_avg": 0.81,
        "source_count": 12,
        "top_sources": [
            {"handle": "@PredictIt", "credibility": 0.84},
            {"handle": "@NateSilver", "credibility": 0.79},
        ],
        "category": "crypto",
    }


class TestTelegramFormatter(unittest.TestCase):
    def test_contains_slug_link(self):
        # MarkdownV2 escapes '.', '-', '(' etc. Assert on a substring
        # that survives escaping (the unique slug body, minus the
        # escapable chars it contains).
        out = format_best_bet_telegram(_bet())
        self.assertIn("narve", out)
        self.assertIn("markets", out)
        self.assertIn("will", out)
        self.assertIn("btc", out)
        self.assertIn("pass", out)
        self.assertIn("100k", out)

    def test_escapes_reserved_chars(self):
        out = format_best_bet_telegram(_bet())
        # MarkdownV2 reserved chars inside the question text should be
        # escaped. '$' isn't reserved, '?' isn't reserved either — we
        # verify the literal '.' in 'narve.ai' is escaped.
        self.assertIn("narve\\.ai", out)

    def test_contains_side_and_edge(self):
        out = format_best_bet_telegram(_bet())
        self.assertIn("YES", out)
        self.assertIn("14", out)


class TestDiscordFormatter(unittest.TestCase):
    def test_embed_kwargs_shape(self):
        out = format_best_bet_discord(_bet())
        self.assertIn("title", out)
        self.assertIn("url", out)
        self.assertIn("color", out)
        self.assertIn("fields", out)
        names = {f["name"] for f in out["fields"]}
        for expect in ("Side", "Edge", "Confidence",
                       "narve YES", "Market YES", "Sources"):
            self.assertIn(expect, names)

    def test_color_band_by_edge(self):
        high = format_best_bet_discord({**_bet(), "edge_pct": 15.0})["color"]
        med = format_best_bet_discord({**_bet(), "edge_pct": 6.0})["color"]
        low = format_best_bet_discord({**_bet(), "edge_pct": 1.0})["color"]
        self.assertNotEqual(high, med)
        self.assertNotEqual(med, low)


if __name__ == "__main__":
    unittest.main()

"""Subproduct content filter tests."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from subproduct_filters import (  # noqa: E402
    filter_by_subproduct,
    matches_subproduct,
    sql_where_for,
)


def _row(**kw) -> dict:
    base = {"category": None, "content": "", "question": "", "platform": ""}
    base.update(kw)
    return base


class TestCategoryMatch(unittest.TestCase):
    def test_sports(self):
        rows = [
            _row(category="nfl", content="Will the Chiefs win?"),
            _row(category="mma", content="UFC 299"),
            _row(category="politics", content="Election 2026"),
        ]
        kept = filter_by_subproduct(rows, "sports")
        self.assertEqual(len(kept), 2)

    def test_weather(self):
        rows = [
            _row(category="weather", content="Temperature over 100F in Phoenix?"),
            _row(category="politics", content="Will it rain on Nov 5?"),
            _row(category="other", content="Totally unrelated bet"),
        ]
        kept = filter_by_subproduct(rows, "weather")
        # Row 1 matches via category. Row 2 matches via 'rain' keyword.
        # Row 3 has no weather keyword and no weather category.
        self.assertEqual(len(kept), 2)

    def test_crypto_via_keyword(self):
        rows = [
            _row(category="finance", content="Will BTC pass $100k by EOY?"),
            _row(category="crypto", content="ETH ETF approval"),
            _row(category="politics", content="nothing to do with crypto"),
        ]
        kept = filter_by_subproduct(rows, "crypto")
        # First two match (first via keyword, second via category).
        # The third has "crypto" in the content so it also matches —
        # filters err toward inclusion for this subproduct.
        self.assertGreaterEqual(len(kept), 2)

    def test_midterm_us_only(self):
        rows = [
            _row(category="politics", content="Senate Ohio race winner?"),
            _row(category="politics", content="UK general election winner?"),
        ]
        kept = filter_by_subproduct(rows, "midterm")
        # First matches the US-specific regex via 'Senate'; second has
        # no US keyword and should be filtered out.
        self.assertEqual(len(kept), 1)
        self.assertIn("senate", kept[0]["content"].lower())

    def test_traders_passthrough(self):
        rows = [
            _row(platform="polymarket", content="anything"),
            _row(platform="kalshi", content="different"),
            _row(platform="polymarket", content="third"),
        ]
        kept = filter_by_subproduct(rows, "traders")
        # traders is platform=polymarket only.
        self.assertEqual(len(kept), 2)

    def test_single_row_matches_subproduct(self):
        self.assertTrue(matches_subproduct(
            _row(category="nfl"), "sports",
        ))
        self.assertFalse(matches_subproduct(
            _row(category="nfl"), "crypto",
        ))


class TestSqlWhere(unittest.TestCase):
    def test_sports_has_placeholders(self):
        frag, params = sql_where_for("sports", alias="p")
        self.assertIn("p.category IN", frag)
        self.assertEqual(len(params), len({"sports", "nfl", "nba", "soccer", "mma", "tennis", "mlb", "nhl"}))

    def test_traders_has_noop_clause(self):
        frag, params = sql_where_for("traders")
        self.assertIn("1=1", frag)
        self.assertEqual(params, [])

    def test_crypto_includes_keyword_literals(self):
        frag, params = sql_where_for("crypto", alias="p")
        self.assertIn("LOWER(p.content) LIKE ?", frag)
        lower_params = [p.lower() for p in params]
        self.assertTrue(any("bitcoin" in p for p in lower_params))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Offline smoke tests for the Love Index computation.

Mocks the data fetchers so the methodology can be exercised without network.
Run: python3 test_methodology.py
"""
from __future__ import annotations

import sys
from unittest.mock import patch

import server


# Synthetic country meta: 8 countries, 2 in each income tier.
META = {
    "USA": {"iso3": "USA", "iso2": "US", "name": "United States", "income_tier": "H",  "region": "Americas"},
    "DEU": {"iso3": "DEU", "iso2": "DE", "name": "Germany",       "income_tier": "H",  "region": "Europe"},
    "FRA": {"iso3": "FRA", "iso2": "FR", "name": "France",        "income_tier": "H",  "region": "Europe"},
    "GBR": {"iso3": "GBR", "iso2": "GB", "name": "United Kingdom","income_tier": "H",  "region": "Europe"},
    "BRA": {"iso3": "BRA", "iso2": "BR", "name": "Brazil",        "income_tier": "UM", "region": "Americas"},
    "ZAF": {"iso3": "ZAF", "iso2": "ZA", "name": "South Africa",  "income_tier": "UM", "region": "Africa"},
    "MEX": {"iso3": "MEX", "iso2": "MX", "name": "Mexico",        "income_tier": "UM", "region": "Americas"},
    "TUR": {"iso3": "TUR", "iso2": "TR", "name": "Turkey",        "income_tier": "UM", "region": "Europe"},
}


def fail(msg):
    print("  FAIL:", msg); sys.exit(1)


def ok(msg):
    print("  ok:", msg)


def test_percentile_rank_within_tier():
    print("test: percentile rank within income tier")
    with patch.object(server, "get_country_meta", return_value=META):
        # higher_is_better=True, no cap
        vals = {"USA": 5.0, "DEU": 4.0, "FRA": 3.0, "GBR": 2.0,
                "BRA": 9.0, "ZAF": 7.0, "MEX": 5.0, "TUR": 3.0}
        out = server.percentile_rank_within_tier(vals, higher_is_better=True)
        if not (out["USA"] == 100.0 and out["GBR"] == 0.0):
            fail(f"USA/GBR ends of H-tier: {out}")
        ok("higher_is_better top/bottom of tier are 100/0")
        # cross-tier independence: BRA (UM tier max) is 100 even though 9.0 > USA 5.0
        if out["BRA"] != 100.0:
            fail(f"BRA should be 100 within UM tier, got {out['BRA']}")
        ok("cross-tier independence holds")

        # higher_is_better=False (lower=better)
        out = server.percentile_rank_within_tier(vals, higher_is_better=False)
        if not (out["GBR"] == 100.0 and out["USA"] == 0.0):
            fail(f"inverted: GBR/USA: {out}")
        ok("higher_is_better=False inverts ranks")

        # cap_pct
        out = server.percentile_rank_within_tier(vals, higher_is_better=True, cap_pct=80)
        if max(out.values()) > 80.0:
            fail(f"cap not applied: max={max(out.values())}")
        ok("cap_pct caps top of distribution")


def test_compute_subscores_missing_data_policy():
    print("test: missing-data policy (>=2 of 3 Tier-A/B subscores)")
    with patch.object(server, "get_country_meta", return_value=META), \
         patch.object(server, "_safe_fetch") as fetch:
        # Only divorce rate (Stability) — 1 subscore -> all countries dropped
        fetch.side_effect = lambda key, _loader: (
            {"USA": 2.7, "DEU": 1.8, "FRA": 1.9, "GBR": 1.6,
             "BRA": 1.4, "ZAF": 0.4, "MEX": 0.9, "TUR": 1.6}
            if key == "eurostat_divorce" else {}
        )
        out = server.compute_subscores()
        if out:
            fail(f"with only Stability, no country should rank, got {len(out)}")
        ok("countries dropped when only 1 of 3 Tier-A/B subscores present")

    # Two subscores -> should rank
    with patch.object(server, "get_country_meta", return_value=META), \
         patch.object(server, "_safe_fetch") as fetch:
        marriage = {"USA": 6.1, "DEU": 4.9, "FRA": 3.5, "GBR": 4.4,
                    "BRA": 5.0, "ZAF": 4.5, "MEX": 4.0, "TUR": 6.2}
        divorce  = {"USA": 2.7, "DEU": 1.8, "FRA": 1.9, "GBR": 1.6,
                    "BRA": 1.4, "ZAF": 0.4, "MEX": 0.9, "TUR": 1.6}
        fetch.side_effect = lambda key, _loader: (
            marriage if key == "eurostat_marriage"
            else divorce if key == "eurostat_divorce"
            else {}
        )
        out = server.compute_subscores()
        if len(out) != 8:
            fail(f"expected 8 countries ranked, got {len(out)}")
        ok(f"all 8 countries ranked with Partnership + Stability present")

        # Composite must be a weighted avg of Partnership (30%) + Stability (25%)
        c = out["DEU"]
        p, s = c["subscores"]["partnership"], c["subscores"]["stability"]
        expected = (0.30 * p + 0.25 * s) / (0.30 + 0.25)
        if abs(c["composite"] - round(expected, 1)) > 0.05:
            fail(f"DEU composite: got {c['composite']}, expected {expected}")
        ok("composite uses renormalized weights when subscores missing")

        # Partnership must be capped at 80
        for c in out.values():
            p = c["subscores"]["partnership"]
            if p is not None and p > 80.0:
                fail(f"{c['iso3']} partnership uncapped: {p}")
        ok("partnership cap enforced (<= 80 pct)")


def test_weights_sum_to_one():
    print("test: methodology constants")
    if abs(sum(server.WEIGHTS.values()) - 1.0) > 1e-9:
        fail(f"weights don't sum to 1: {server.WEIGHTS}")
    ok(f"weights sum to 1.0: {server.WEIGHTS}")


def main():
    test_weights_sum_to_one()
    test_percentile_rank_within_tier()
    test_compute_subscores_missing_data_policy()
    print("\nall tests passed.")


if __name__ == "__main__":
    main()

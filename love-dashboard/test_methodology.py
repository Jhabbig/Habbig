#!/usr/bin/env python3
"""Offline smoke tests for the Love Index computation.

Mocks the data fetchers so the methodology can be exercised without network.
Run: python3 test_methodology.py
"""
from __future__ import annotations

import sys
from unittest.mock import patch

import insights as insights_module
import sensitivity as sensitivity_module
import server


def _clear_cache():
    """The server caches subscore_layers between calls; tests need a clean slate."""
    server._cache.clear()


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
    _clear_cache()
    with patch.object(server, "get_country_meta", return_value=META), \
         patch.object(server, "_safe_fetch") as fetch:
        fetch.side_effect = lambda key, _loader: (
            {"USA": 2.7, "DEU": 1.8, "FRA": 1.9, "GBR": 1.6,
             "BRA": 1.4, "ZAF": 0.4, "MEX": 0.9, "TUR": 1.6}
            if key == "eurostat_divorce" else {}
        )
        out = server.compute_subscores()
        if out:
            fail(f"with only Stability, no country should rank, got {len(out)}")
        ok("countries dropped when only 1 of 3 Tier-A/B subscores present")

    _clear_cache()
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

        c = out["DEU"]
        p, s = c["subscores"]["partnership"], c["subscores"]["stability"]
        expected = (0.30 * p + 0.25 * s) / (0.30 + 0.25)
        if abs(c["composite"] - round(expected, 1)) > 0.05:
            fail(f"DEU composite: got {c['composite']}, expected {expected}")
        ok("composite uses renormalized weights when subscores missing")

        for c in out.values():
            p = c["subscores"]["partnership"]
            if p is not None and p > 80.0:
                fail(f"{c['iso3']} partnership uncapped: {p}")
        ok("partnership cap enforced (<= 80 pct)")


def test_custom_weights():
    print("test: custom weights via _normalize_weights and compute_subscores")
    _clear_cache()
    # Equal weights (renormalized to 0.25 each)
    w = server._normalize_weights({"connection": 1, "partnership": 1, "stability": 1, "activity": 1})
    if abs(sum(w.values()) - 1.0) > 1e-9:
        fail(f"equal-input weights don't normalize to 1: {w}")
    if any(abs(v - 0.25) > 1e-9 for v in w.values()):
        fail(f"equal weights not 0.25 each: {w}")
    ok("equal-input weights renormalize to 0.25 each")

    # Empty / None falls back to default
    if server._normalize_weights(None) != server.WEIGHTS:
        fail("None weights should return defaults")
    if server._normalize_weights({}) != server.WEIGHTS:
        fail("empty weights should return defaults")
    ok("None/empty weights fall back to defaults")

    # Compute with two different weight schemes and verify rankings differ.
    # Within H tier: USA highest WHR, DEU lowest. Within H tier: USA highest
    # marriage rate, GBR lowest. Different weights -> different H-tier order.
    _clear_cache()
    with patch.object(server, "get_country_meta", return_value=META), \
         patch.object(server, "_safe_fetch") as fetch:
        marriage = {"USA": 6.5, "DEU": 5.0, "FRA": 4.0, "GBR": 3.0,
                    "BRA": 5.0, "ZAF": 4.0, "MEX": 5.0, "TUR": 6.0}
        divorce  = {"USA": 2.7, "DEU": 1.8, "FRA": 1.9, "GBR": 1.6,
                    "BRA": 1.4, "ZAF": 0.4, "MEX": 0.9, "TUR": 1.6}
        whr      = {"USA": 95, "DEU": 80, "FRA": 75, "GBR": 90,
                    "BRA": 60, "ZAF": 50, "MEX": 65, "TUR": 55}
        fetch.side_effect = lambda key, _loader: (
            marriage if key == "eurostat_marriage"
            else divorce if key == "eurostat_divorce"
            else whr if key == "whr_social_support"
            else {}
        )
        biased_conn = {"connection": 1.0, "partnership": 0.0, "stability": 0.0, "activity": 0.0}
        out_conn = server.compute_subscores(biased_conn)
        h_order_conn = [c["iso3"] for c in
                        sorted([c for c in out_conn.values() if c["income_tier"] == "H"],
                               key=lambda c: c["composite"], reverse=True)]
        if h_order_conn[0] != "USA":
            fail(f"connection-weighted H tier should lead with USA (highest WHR), got {h_order_conn}")
        ok(f"connection-weighting orders H tier: {h_order_conn}")

        _clear_cache()
        biased_part = {"connection": 0.0, "partnership": 1.0, "stability": 0.0, "activity": 0.0}
        out_part = server.compute_subscores(biased_part)
        h_order_part = [c["iso3"] for c in
                        sorted([c for c in out_part.values() if c["income_tier"] == "H"],
                               key=lambda c: c["composite"], reverse=True)]
        if h_order_part[-1] != "GBR":
            fail(f"partnership-weighted H tier should rank GBR last (lowest marriage rate), got {h_order_part}")
        ok(f"partnership-weighting orders H tier: {h_order_part}")
        if h_order_conn == h_order_part:
            fail("changing weights should change rankings, but they're identical")
        ok("different weights produce different rankings")


def test_insights_engine():
    print("test: insights engine (outlier, divergence, peer_leader, coverage_gap)")
    countries = [
        {"iso3": "AAA", "name": "Alpha",   "income_tier": "H",  "subscores": {"connection": 80, "partnership": 60, "stability": 70, "activity": None}, "composite": 71.0, "used": ["connection","partnership","stability"]},
        {"iso3": "BBB", "name": "Beta",    "income_tier": "H",  "subscores": {"connection": 40, "partnership": 60, "stability": 50, "activity": None}, "composite": 49.5, "used": ["connection","partnership","stability"]},
        {"iso3": "CCC", "name": "Gamma",   "income_tier": "H",  "subscores": {"connection": 50, "partnership": 90, "stability": 30, "activity": None}, "composite": 58.0, "used": ["connection","partnership","stability"]},
        {"iso3": "DDD", "name": "Delta",   "income_tier": "H",  "subscores": {"connection": 50, "partnership": 50, "stability": 50, "activity": None}, "composite": 50.0, "used": ["connection","partnership","stability"]},
        {"iso3": "EEE", "name": "Epsilon", "income_tier": "H",  "subscores": {"connection": 50, "partnership": 50, "stability": 50, "activity": None}, "composite": 50.0, "used": ["connection","partnership","stability"]},
    ]
    meta = {c["iso3"]: {"name": c["name"], "iso2": c["iso3"][:2], "income_tier": c["income_tier"]}
            for c in countries}

    out = insights_module.generate_insights(countries, meta)
    if not out:
        fail("expected at least one insight, got none")
    ok(f"generated {len(out)} insights")

    kinds = {i["kind"] for i in out}
    if "peer_leader" not in kinds:
        fail("expected a peer_leader insight (Alpha leads H tier)")
    ok("peer_leader fires for Alpha")
    if "divergence" not in kinds:
        fail("expected a divergence insight (Gamma's 90 partnership vs 30 stability)")
    ok("divergence fires for Gamma (90 partnership vs 30 stability)")
    if "outlier" not in kinds:
        fail("expected an outlier insight (Alpha connection 80 vs tier mean ~54)")
    ok("outlier fires for Alpha on connection")


def test_weights_sum_to_one():
    print("test: methodology constants")
    if abs(sum(server.WEIGHTS.values()) - 1.0) > 1e-9:
        fail(f"weights don't sum to 1: {server.WEIGHTS}")
    ok(f"weights sum to 1.0: {server.WEIGHTS}")


def test_sensitivity_engine():
    print("test: sensitivity engine (per-country rank ranges across perturbations)")

    # Three countries whose ordering depends heavily on weight choice:
    # - USA wins on Connection (95), terrible elsewhere
    # - DEU wins on Partnership (95), terrible elsewhere
    # - FRA is balanced (70 across the board) -> always rank 2 -> stable
    scores = {
        "USA": {"connection": 95, "partnership": 30, "stability": 30, "activity": 30},
        "DEU": {"connection": 30, "partnership": 95, "stability": 30, "activity": 30},
        "FRA": {"connection": 70, "partnership": 70, "stability": 70, "activity": 70},
    }

    def fake_compute(weights):
        denom = sum(weights.values()) or 1.0
        out = {}
        for iso, s in scores.items():
            num = sum(weights[k] * s[k] for k in weights)
            out[iso] = {
                "iso3": iso,
                "iso2": iso[:2],
                "name": iso,
                "income_tier": "H",
                "composite": num / denom,
                "subscores": s,
            }
        return out

    result = sensitivity_module.compute_sensitivity(fake_compute, dict(server.WEIGHTS))

    if "countries" not in result or "perturbations" not in result:
        fail("sensitivity payload missing top-level keys")
    ok(f"ran {len(result['perturbations'])} perturbations")

    fra = result["countries"]["FRA"]
    if fra["stability"] != "high":
        fail(f"FRA should be stably ranked, got {fra['stability']} (range={fra['rank_range']})")
    ok(f"FRA flagged 'high' stability (range={fra['rank_range']})")

    usa = result["countries"]["USA"]
    deu = result["countries"]["DEU"]
    if usa["rank_range"] == 0 or deu["rank_range"] == 0:
        fail(f"USA/DEU should shuffle under perturbations; ranges USA={usa['rank_range']}, DEU={deu['rank_range']}")
    ok(f"USA range={usa['rank_range']}, DEU range={deu['rank_range']} (both > 0)")

    # Baseline rank must be present and within 1..N
    for iso, c in result["countries"].items():
        if c["rank_baseline"] is None or c["rank_baseline"] < 1:
            fail(f"{iso}: bad baseline rank {c['rank_baseline']}")
    ok("every country has a baseline rank")

    dist = result["stability_distribution"]
    if dist["high"] + dist["medium"] + dist["low"] != 3:
        fail(f"stability_distribution does not sum to N: {dist}")
    ok(f"stability distribution sums correctly: {dist}")


def main():
    test_weights_sum_to_one()
    test_percentile_rank_within_tier()
    test_compute_subscores_missing_data_policy()
    test_custom_weights()
    test_insights_engine()
    test_sensitivity_engine()
    print("\nall tests passed.")


if __name__ == "__main__":
    main()

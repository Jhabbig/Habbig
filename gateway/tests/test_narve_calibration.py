"""Known-answer tests for narve's calibration grader.

The grader is the thing that decides whether narve is any good, so it is tested
against hand-built batches whose correct score is known by construction:

  * A PERFECTLY-CALIBRATED forecaster (says 0.9 on things that happen, 0.1 on
    things that don't) must score ~100% directional accuracy and a low Brier
    (~0.01) — well inside coinflip-beating territory.
  * A COIN-FLIP forecaster (always ~0.5) must score Brier ~0.25 and vs_coinflip
    ~0, by definition.
  * An ANTI-calibrated forecaster (confident and wrong) must score ~0% accuracy
    and a high Brier (~0.81), worse than the coin.

fetch_resolved_markets is networked, so it is not exercised here (it is hit live
by the build harness); the parsing/decisive rule it delegates to is covered by
tests/test_polymarket_ingest.py.
"""
from intelligence import narve_calibration as nc


def _perfect_batch(n_yes=15, n_no=15):
    """Forecaster says 0.9 on every market that resolves YES and 0.1 on every
    market that resolves NO — perfectly calibrated and perfectly directional."""
    fcs = []
    for i in range(n_yes):
        fcs.append({"market_id": f"yes-{i}", "narve_probability": 0.9,
                    "resolved_outcome": 1})
    for i in range(n_no):
        fcs.append({"market_id": f"no-{i}", "narve_probability": 0.1,
                    "resolved_outcome": 0})
    return fcs


def _coinflip_batch(n=40):
    """Forecaster always says ~0.5; outcomes split evenly. Brier -> 0.25."""
    fcs = []
    for i in range(n):
        fcs.append({"market_id": f"cf-{i}", "narve_probability": 0.5,
                    "resolved_outcome": i % 2})
    return fcs


def _anti_calibrated_batch(n_yes=15, n_no=15):
    """Confident and WRONG: 0.9 on things that DON'T happen, 0.1 on things that
    DO. The worst case — high Brier, ~0 accuracy."""
    fcs = []
    for i in range(n_yes):  # actually YES, but narve said 0.1
        fcs.append({"market_id": f"yes-{i}", "narve_probability": 0.1,
                    "resolved_outcome": 1})
    for i in range(n_no):   # actually NO, but narve said 0.9
        fcs.append({"market_id": f"no-{i}", "narve_probability": 0.9,
                    "resolved_outcome": 0})
    return fcs


def test_perfect_forecaster_high_accuracy_low_brier():
    out = nc.grade(_perfect_batch())
    assert out["n"] == 30
    assert out["accuracy"] == 1.0                       # every directional call right
    assert out["brier"] is not None
    assert out["brier"] < 0.02                          # (0.9-1)^2 = 0.01 throughout
    assert out["calibration"] > 0.9                     # near-perfect UI score
    assert out["vs_coinflip"] > 0.2                     # crushes the coin (0.25 - ~0.01)


def test_coinflip_forecaster_brier_near_quarter():
    out = nc.grade(_coinflip_batch())
    assert out["n"] == 40
    assert out["brier"] is not None
    assert abs(out["brier"] - 0.25) < 1e-6              # (0.5-o)^2 = 0.25 always
    assert abs(out["vs_coinflip"]) < 1e-6              # no edge over a coin


def test_anti_calibrated_forecaster_is_worse_than_coin():
    out = nc.grade(_anti_calibrated_batch())
    assert out["n"] == 30
    assert out["accuracy"] == 0.0                       # every call wrong
    assert out["brier"] is not None
    assert out["brier"] > 0.8                           # (0.9-0)^2 = 0.81
    assert out["vs_coinflip"] < 0                       # worse than the coin
    assert out["calibration"] == 0.0                    # clamped floor


def test_perfect_beats_coin_beats_anti():
    """Ordering sanity: the three archetypes rank as expected on Brier."""
    perfect = nc.grade(_perfect_batch())["brier"]
    coin = nc.grade(_coinflip_batch())["brier"]
    anti = nc.grade(_anti_calibrated_batch())["brier"]
    assert perfect < coin < anti


def test_directional_accuracy_treats_half_as_yes():
    # p == 0.5 is a YES call (>= 0.5). One YES (hit) + one NO (miss) -> 50%.
    out = nc.grade([
        {"market_id": "a", "narve_probability": 0.5, "resolved_outcome": 1},
        {"market_id": "b", "narve_probability": 0.5, "resolved_outcome": 0},
    ])
    assert out["n"] == 2
    assert out["accuracy"] == 0.5


def test_below_min_sample_brier_is_none_accuracy_still_reported():
    # Fewer than MIN_SAMPLE (10) usable records: Brier/calibration/vs_coinflip
    # are None (not meaningful), but directional accuracy is still computed.
    out = nc.grade([
        {"market_id": "a", "narve_probability": 0.8, "resolved_outcome": 1},
        {"market_id": "b", "narve_probability": 0.2, "resolved_outcome": 0},
    ])
    assert out["n"] == 2
    assert out["accuracy"] == 1.0
    assert out["brier"] is None
    assert out["calibration"] is None
    assert out["vs_coinflip"] is None
    # reliability bins are always shaped (10 buckets) for chart continuity
    assert len(out["reliability_bins"]) == 10


def test_empty_and_malformed_inputs():
    empty = nc.grade([])
    assert empty["n"] == 0 and empty["accuracy"] is None
    assert empty["reliability_bins"] == []

    # malformed records (bad prob / non-binary outcome / non-dict) are skipped
    out = nc.grade([
        {"market_id": "ok1", "narve_probability": 0.9, "resolved_outcome": 1},
        {"market_id": "bad-prob", "narve_probability": 1.7, "resolved_outcome": 1},
        {"market_id": "bad-out", "narve_probability": 0.4, "resolved_outcome": 2},
        {"market_id": "none-prob", "narve_probability": None, "resolved_outcome": 0},
        "not-a-dict",
    ])
    assert out["n"] == 1                                # only the clean record survives


def test_reliability_bins_locate_predictions():
    # A well-calibrated forecaster: 0.85 on markets that resolve YES (observed
    # YES-rate 1.0) and 0.15 on markets that resolve NO (observed YES-rate 0.0).
    # Each bin's |predicted_avg - observed_yes_rate| == 0.15, comfortably inside
    # the diagram's 0.10 over/under-confidence threshold band edges, and clear of
    # the float-drift boundary that an exact-0.10 delta would sit on.
    fcs = ([{"market_id": f"y{i}", "narve_probability": 0.85, "resolved_outcome": 1}
            for i in range(15)]
           + [{"market_id": f"n{i}", "narve_probability": 0.15, "resolved_outcome": 0}
              for i in range(15)])
    out = nc.grade(fcs)
    assert out["accuracy"] == 1.0                        # directional calls all right
    top = out["reliability_bins"][-2]                    # [0.8, 0.9): the 0.85s
    assert top["count"] == 15
    assert top["predicted_avg"] == 0.85
    assert top["actual_accuracy"] == 1.0                 # all resolved YES
    low = out["reliability_bins"][1]                     # [0.1, 0.2): the 0.15s
    assert low["count"] == 15
    assert low["predicted_avg"] == 0.15
    assert low["actual_accuracy"] == 0.0                 # none resolved YES
    assert out["reliability_bins"][0]["count"] == 0      # [0.0, 0.1) empty

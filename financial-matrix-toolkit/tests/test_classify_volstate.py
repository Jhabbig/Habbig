"""Test the volatility-state classifier: a target where high accuracy is REAL.

Locks in the contrast with return direction: vol state is genuinely predictable
(well above the 50% chance floor), because volatility clusters/persists.
"""

from classify_volstate import make_state, build_features, run
from data import load_market_data


def test_state_is_balanced_by_construction():
    md = load_market_data()
    _, vol = build_features(md.R, vol_window=5)
    S = make_state(vol, window=63)
    import numpy as np
    frac = np.nanmean(S)
    assert 0.4 < frac < 0.6  # calm/turbulent ~ 50/50, so 50% == chance


def test_volstate_is_highly_predictable():
    md = load_market_data()
    acc = run(md.R, vol_window=21, med_window=63, train=300, step=10, horizon=1)
    base = acc["BaseRate(null)"][0] / acc["BaseRate(null)"][1]
    pers = acc["Persistence"][0] / acc["Persistence"][1]
    assert 0.4 < base < 0.6      # honest chance floor near 50%
    assert pers > 0.8            # vol regime genuinely predictable well above chance


def test_direction_contrast_documented():
    # Sanity: the classifier's best model should crush the base rate - the whole
    # point being that this is possible for vol state but NOT for return direction.
    md = load_market_data()
    acc = run(md.R, vol_window=10, med_window=63, train=300, step=10, horizon=1)
    base = acc["BaseRate(null)"][0] / acc["BaseRate(null)"][1]
    best = max(acc[m][0] / acc[m][1] for m in acc if m != "BaseRate(null)")
    assert best - base > 0.2

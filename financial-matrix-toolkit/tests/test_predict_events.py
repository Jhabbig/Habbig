"""Tests for the general event-prediction harness (predict_events.py).

Locks in the imbalance-aware behaviour: rare-event accuracy is a liar, so the
base-rate null gets ~0 recall and 0.5 BALANCED accuracy, while models that add
real skill push balanced accuracy above 0.5.
"""

import numpy as np

from data import load_market_data
from predict_events import EVENTS, build_context, clf_metrics, evaluate_event


def test_clf_metrics_known_confusion():
    y_true = np.array([1, 1, 0, 0, 1])
    y_pred = np.array([1, 0, 0, 1, 1])          # tp=2 fn=1 tn=1 fp=1
    d = clf_metrics(y_true, y_pred)
    assert abs(d["acc"] - 0.6) < 1e-9
    assert abs(d["recall"] - 2 / 3) < 1e-9
    assert abs(d["precision"] - 2 / 3) < 1e-9
    assert abs(d["base_rate"] - 0.6) < 1e-9


def test_event_registry_labels_are_binary():
    md = load_market_data()
    ctx = build_context(md, vol_window=5, med_window=63)
    for name, (fn, _desc) in EVENTS.items():
        S = fn(ctx)
        assert S.shape == md.R.shape
        vals = np.unique(S[np.isfinite(S)])
        assert set(vals).issubset({0.0, 1.0}), name


def test_balanced_event_is_predictable():
    md = load_market_data()
    ctx = build_context(md, vol_window=5, med_window=63)
    res = evaluate_event(ctx, EVENTS["vol_state"][0], train=400, step=15, horizon=1,
                         logistic_cfg=dict(l2=1.0, lr=0.3, epochs=200))
    assert abs(res["BaseRate(null)"]["bal_acc"] - 0.5) < 0.05   # floor
    assert res["Logistic"]["bal_acc"] > 0.7                     # real skill


def test_rare_event_accuracy_is_not_skill():
    # big_move is a ~10% tail event: the base rate scores HIGH accuracy but ~0
    # recall and 0.5 balanced accuracy -> the honesty check the tool exists for.
    md = load_market_data()
    ctx = build_context(md, vol_window=5, med_window=63)
    res = evaluate_event(ctx, EVENTS["big_move"][0], train=400, step=15, horizon=1,
                         logistic_cfg=dict(l2=1.0, lr=0.3, epochs=200))
    base = res["BaseRate(null)"]
    assert base["base_rate"] < 0.25          # genuinely rare
    assert base["acc"] > 0.7                  # high accuracy...
    assert base["recall"] < 0.05              # ...but catches ~no events
    assert abs(base["bal_acc"] - 0.5) < 0.05  # balanced accuracy exposes it

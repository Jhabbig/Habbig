"""calibration.py - turn raw classifier scores into trustworthy probabilities.

A model can rank well (high AUC) yet be badly calibrated - it says "90%" when the
event happens 60% of the time. If you want to SIZE POSITIONS on P(event), the
number has to mean what it says. Two standard recalibrators (fit on training data,
applied out-of-sample), plus a reliability diagram and the Expected Calibration
Error (ECE) to measure how honest the probabilities are.

    platt  - fit a sigmoid  sigmoid(A*logit(p)+B)   (parametric, stable on little data)
    isotonic - monotonic step fit via pool-adjacent-violators (flexible, needs more data)
"""

from __future__ import annotations

import numpy as np


def _logit(p, eps=1e-6):
    p = np.clip(np.asarray(p, dtype=float), eps, 1 - eps)
    return np.log(p / (1 - p))


# --------------------------------------------------------------------------- #
# Platt scaling
# --------------------------------------------------------------------------- #
def platt_fit(scores, y, iters=300, lr=0.5):
    """Fit sigmoid(A*z + B) where z = standardised logit(score)."""
    x = _logit(scores)
    y = np.asarray(y, dtype=float)
    mu, sd = float(x.mean()), float(x.std() + 1e-9)
    z = (x - mu) / sd
    A, B = 1.0, 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-(A * z + B)))
        A -= lr * float(np.mean((p - y) * z))
        B -= lr * float(np.mean(p - y))
    return {"kind": "platt", "A": A, "B": B, "mu": mu, "sd": sd}


def _platt_apply(m, scores):
    z = (_logit(scores) - m["mu"]) / m["sd"]
    return 1.0 / (1.0 + np.exp(-(m["A"] * z + m["B"])))


# --------------------------------------------------------------------------- #
# Isotonic regression (pool-adjacent-violators)
# --------------------------------------------------------------------------- #
def isotonic_fit(scores, y):
    s = np.asarray(scores, dtype=float)
    yv = np.asarray(y, dtype=float)
    order = np.argsort(s, kind="mergesort")
    xs, ys = s[order], yv[order]
    blocks = []  # each: [value, weight, start, end]
    for k in range(len(ys)):
        b = [ys[k], 1.0, k, k]
        while blocks and blocks[-1][0] >= b[0]:
            pb = blocks.pop()
            v = (pb[0] * pb[1] + b[0] * b[1]) / (pb[1] + b[1])
            b = [v, pb[1] + b[1], pb[2], b[3]]
        blocks.append(b)
    yhat = np.empty(len(ys))
    for v, w, a, e in blocks:
        yhat[a:e + 1] = v
    # collapse to unique x (keep last fitted value per x) for interpolation
    ux = np.unique(xs)
    uy = np.array([yhat[xs == x][-1] for x in ux])
    return {"kind": "isotonic", "x": ux, "y": uy}


def _isotonic_apply(m, scores):
    return np.interp(np.asarray(scores, dtype=float), m["x"], m["y"],
                     left=m["y"][0], right=m["y"][-1])


# --------------------------------------------------------------------------- #
# unified interface
# --------------------------------------------------------------------------- #
def fit_calibrator(scores, y, method="platt"):
    y = np.asarray(y, dtype=float)
    if method == "none" or len(np.unique(y)) < 2 or len(y) < 20:
        return {"kind": "identity"}
    if method == "isotonic":
        return isotonic_fit(scores, y)
    return platt_fit(scores, y)


def apply_calibrator(m, scores):
    if m.get("kind") == "identity":
        return np.asarray(scores, dtype=float)
    if m["kind"] == "isotonic":
        return _isotonic_apply(m, scores)
    return _platt_apply(m, scores)


# --------------------------------------------------------------------------- #
# reliability + ECE
# --------------------------------------------------------------------------- #
def reliability(y, p, n_bins=10):
    """Return (mean_predicted, observed_freq, count) per probability bin."""
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    edges = np.linspace(0, 1, n_bins + 1)
    idx = np.clip(np.digitize(p, edges) - 1, 0, n_bins - 1)
    mean_pred = np.full(n_bins, np.nan)
    obs = np.full(n_bins, np.nan)
    cnt = np.zeros(n_bins)
    for b in range(n_bins):
        m = idx == b
        cnt[b] = m.sum()
        if m.any():
            mean_pred[b] = p[m].mean()
            obs[b] = y[m].mean()
    return mean_pred, obs, cnt


def calibrator_to_arrays(m):
    """Flatten a calibrator into np.savez-friendly kwargs."""
    d = {"cal_kind": np.array(m.get("kind", "identity"))}
    if m.get("kind") == "platt":
        d.update(cal_A=np.array(m["A"]), cal_B=np.array(m["B"]),
                 cal_mu=np.array(m["mu"]), cal_sd=np.array(m["sd"]))
    elif m.get("kind") == "isotonic":
        d.update(cal_x=m["x"], cal_y=m["y"])
    return d


def calibrator_from_arrays(d):
    """Rebuild a calibrator from a loaded npz."""
    kind = str(d["cal_kind"]) if "cal_kind" in d else "identity"
    if kind == "platt":
        return {"kind": "platt", "A": float(d["cal_A"]), "B": float(d["cal_B"]),
                "mu": float(d["cal_mu"]), "sd": float(d["cal_sd"])}
    if kind == "isotonic":
        return {"kind": "isotonic", "x": d["cal_x"], "y": d["cal_y"]}
    return {"kind": "identity"}


def expected_calibration_error(y, p, n_bins=10):
    mean_pred, obs, cnt = reliability(y, p, n_bins)
    tot = cnt.sum()
    if tot == 0:
        return float("nan")
    mask = cnt > 0
    w = cnt[mask] / tot
    return float(np.sum(w * np.abs(mean_pred[mask] - obs[mask])))

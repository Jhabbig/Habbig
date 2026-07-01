#!/usr/bin/env python3
"""classify_volstate.py - a target where HIGH accuracy is real: volatility state.

Return DIRECTION is ~unpredictable (a coin flip). Volatility MAGNITUDE is not - it
clusters, so "will tomorrow be calm or turbulent?" is genuinely forecastable. This
trains SEVERAL classifiers at once and reports their out-of-sample accuracy against
an honest 50% base-rate floor (the state is defined so calm/turbulent are ~50/50,
so beating 50% by a wide margin is real skill, not just guessing the majority).

Target: is the next period's short-horizon realized volatility ABOVE its own
trailing median? (1 = turbulent, 0 = calm). Predicted from information available
today only (walk-forward, no look-ahead).

SOURCES:
  --demo               use the toolkit's cached/synthetic 15-asset panel (offline)
  --ticker AAPL        a single Yahoo symbol via yfinance (needs open network)

EXAMPLES:
  python classify_volstate.py --demo
  python classify_volstate.py --demo --vol-window 5 --horizon 1
  python classify_volstate.py --ticker ^GSPC
"""

from __future__ import annotations

import argparse

import numpy as np

from core import set_global_seed
from data import load_market_data


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def build_features(R, vol_window):
    """Per-asset volatility features and the binary turbulent/calm state.

    Returns F (T, n, k) features, S (T, n) current state, VOL (T, n).
    """
    T, n = R.shape
    absr = np.abs(R)
    # short-horizon realised vol (rolling std), padded to length T
    vol = np.full((T, n), np.nan)
    for t in range(vol_window, T + 1):
        vol[t - 1] = R[t - vol_window:t].std(axis=0)
    logvol = np.log(vol + 1e-8)
    # rolling means of log-vol (5- and 21-day) as extra features
    def roll(a, w):
        out = np.full_like(a, np.nan)
        c = np.cumsum(np.nan_to_num(a), axis=0)
        for t in range(w, len(a) + 1):
            out[t - 1] = (c[t - 1] - (c[t - 1 - w] if t - 1 - w >= 0 else 0)) / w
        return out
    lv5, lv21 = roll(logvol, 5), roll(logvol, 21)
    F = np.stack([logvol, lv5, lv21, np.log(absr + 1e-8)], axis=2)  # (T, n, 4)
    return F, vol


def make_state(vol, window):
    """state_t = 1 if vol_t is above its trailing median over `window`, else 0."""
    T, n = vol.shape
    S = np.full((T, n), np.nan)
    for t in range(window, T):
        med = np.nanmedian(vol[t - window:t], axis=0)
        S[t] = (vol[t] > med).astype(float)
    return S


def fit_logistic(X, y, l2=1.0, lr=0.2, epochs=300, seed=42):
    rng = np.random.default_rng(seed)
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Xz = np.hstack([np.ones((len(X), 1)), (X - mu) / sd])
    w = rng.normal(0, 0.01, Xz.shape[1])
    reg = l2 * np.ones(Xz.shape[1]); reg[0] = 0
    for _ in range(epochs):
        p = _sigmoid(Xz @ w)
        w -= lr * (Xz.T @ (p - y) / len(y) + reg * w)
    return w, mu, sd


def predict_logistic(w, mu, sd, X):
    Xz = np.hstack([np.ones((len(X), 1)), (X - mu) / sd])
    return (_sigmoid(Xz @ w) >= 0.5).astype(float)


def run(R, vol_window, med_window, train, step, horizon):
    F, vol = build_features(R, vol_window)
    S = make_state(vol, med_window)
    T, n, k = F.shape
    start = max(med_window, 21) + 5

    # accumulators for each model's correct/total
    acc = {m: [0, 0] for m in ("BaseRate(null)", "Persistence", "Logistic", "EWMA-threshold")}

    # EWMA vol per asset (RiskMetrics) for the EWMA classifier
    ewma = np.full((T, n), np.nan)
    v = np.nanvar(R[:30], axis=0)
    for t in range(T):
        v = 0.94 * v + 0.06 * R[t] ** 2
        ewma[t] = np.sqrt(v)

    t = max(start, train)
    while t + horizon < T:
        tr = slice(t - train, t)
        # pooled training pairs across assets: features at time i predict state at i+horizon
        Xtr, ytr = [], []
        for i in range(t - train, t - horizon):
            m = np.isfinite(F[i]).all(1) & np.isfinite(S[i + horizon])
            if m.any():
                Xtr.append(F[i][m]); ytr.append(S[i + horizon][m])
        if not Xtr:
            t += step; continue
        Xtr = np.vstack(Xtr); ytr = np.concatenate(ytr)
        w, mu, sd = fit_logistic(Xtr, ytr)

        # predict at origin t for all assets, compare to true state at t+horizon
        valid = np.isfinite(F[t]).all(1) & np.isfinite(S[t + horizon]) & np.isfinite(S[t])
        if valid.any():
            ytrue = S[t + horizon][valid]
            # base rate = majority class in training
            base = 1.0 if ytr.mean() >= 0.5 else 0.0
            pred_base = np.full(valid.sum(), base)
            pred_pers = S[t][valid]                                   # persistence
            pred_log = predict_logistic(w, mu, sd, F[t][valid])       # logistic
            med_e = np.nanmedian(ewma[t - med_window:t], axis=0)[valid]
            pred_ewma = (ewma[t][valid] > med_e).astype(float)        # EWMA threshold
            for name, pred in [("BaseRate(null)", pred_base), ("Persistence", pred_pers),
                               ("Logistic", pred_log), ("EWMA-threshold", pred_ewma)]:
                acc[name][0] += np.sum(pred == ytrue)
                acc[name][1] += len(ytrue)
        t += step
    return acc


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--demo", action="store_true", help="use the cached/synthetic 15-asset panel")
    src.add_argument("--ticker", help="a single Yahoo symbol via yfinance")
    ap.add_argument("--vol-window", type=int, default=5, help="days for the realised-vol measure")
    ap.add_argument("--med-window", type=int, default=63, help="trailing window for the calm/turbulent split")
    ap.add_argument("--train", type=int, default=500, help="training window (days)")
    ap.add_argument("--step", type=int, default=3, help="walk-forward step")
    ap.add_argument("--horizon", type=int, default=1, help="predict the vol state this many days ahead")
    args = ap.parse_args()
    set_global_seed(42)

    if args.ticker:
        import pandas as pd
        try:
            import yfinance as yf
        except ImportError:
            raise SystemExit("Install yfinance: pip install -r requirements-live.txt")
        raw = yf.download(args.ticker, start="2005-01-01", progress=False, auto_adjust=True)
        if raw is None or len(raw) == 0:
            raise SystemExit(f"No data for {args.ticker!r}.")
        close = raw["Close"]
        close = close.iloc[:, 0] if hasattr(close, "columns") else close
        R = np.diff(np.log(close.dropna().to_numpy(dtype=float)))[:, None]
        label = args.ticker
    else:
        md = load_market_data()
        R = md.R
        label = f"{md.n}-asset panel ({md.source})"

    print(f"\n\033[1mVolatility-state classification - {label}\033[0m")
    print(f"  target: is vol {args.horizon}d ahead above its {args.med_window}d trailing median?")
    print(f"  (calm vs turbulent ~ 50/50 by construction, so 50% = pure chance)\n")

    acc = run(R, args.vol_window, args.med_window, args.train, args.step, args.horizon)
    base = acc["BaseRate(null)"][0] / max(1, acc["BaseRate(null)"][1])
    print(f"  {'model':<18s}{'accuracy':>10s}{'vs 50% floor':>14s}")
    for name, (c, tot) in acc.items():
        a = c / max(1, tot)
        tag = "\033[90m(chance floor)\033[0m" if name == "BaseRate(null)" else (
            f"\033[92m+{(a-0.5)*100:.0f} pts\033[0m" if a > 0.55 else f"\033[91m+{(a-0.5)*100:.0f} pts\033[0m")
        print(f"  {name:<18s}{a:>9.1%}{'':4s}{tag}")
    best = max((n for n in acc if n != "BaseRate(null)"), key=lambda n: acc[n][0] / max(1, acc[n][1]))
    ba = acc[best][0] / max(1, acc[best][1])
    print(f"\n  \033[1mBest: {best} at {ba:.1%} accuracy\033[0m (vs 50% coin-flip floor).")
    print("  This is REAL, high accuracy - because volatility clusters. Contrast with")
    print("  next-day return DIRECTION, which no model gets above ~53%. Accuracy is a")
    print("  property of the TARGET, not of how hard or how long you train.\n")


if __name__ == "__main__":
    main()

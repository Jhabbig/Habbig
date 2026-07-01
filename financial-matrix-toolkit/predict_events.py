#!/usr/bin/env python3
"""predict_events.py - a general, honest harness for predicting market EVENTS.

Generalises the volatility-state classifier to ANY binary event you can define
over the data. Crucially, most events are RARE (a vol flip, a crash, a big move),
and on rare events plain accuracy is a liar - "predict it never happens" scores
90%+ and is useless. So every event is scored with imbalance-aware metrics:

    accuracy, BALANCED accuracy, precision, recall, F1, and SKILL = balanced
    accuracy - 0.5 (the honest floor: a base-rate guesser scores 0.5 balanced,
    no matter how skewed the classes).

Models trained simultaneously per event:
    BaseRate(null)  - always predict the majority class (the floor)
    Persistence     - predict the event repeats (label_t -> label_{t+h})
    Logistic        - class-weighted logistic regression on the features

EVENTS (all per-asset, walk-forward, no look-ahead):
    vol_state       is short-horizon vol above its trailing median?   (balanced)
    vol_transition  will the vol regime FLIP calm<->turbulent?         (rare, the hard one)
    big_move        will |return| exceed k x trailing std?            (tail, clusters)
    drawdown        is the asset in a drawdown deeper than X%?        (persistent)
    trend_up        is the price above its N-day moving average?      (persistent)

USAGE:
    python predict_events.py --demo --event all
    python predict_events.py --demo --event vol_transition
    python predict_events.py --demo --event vol_state --grid     # logistic hyperparameter sweep
    python predict_events.py --ticker ^GSPC --event big_move
"""

from __future__ import annotations

import argparse
import itertools

import numpy as np

from classify_volstate import _roll_mean, _roll_median, build_features, make_state
from core import set_global_seed
from data import load_market_data


# --------------------------------------------------------------------------- #
# Context + features shared by all events
# --------------------------------------------------------------------------- #
def build_context(md, vol_window, med_window):
    R = np.asarray(md.R, dtype=float)
    P = np.asarray(md.P, dtype=float)
    T, n = R.shape
    price = P[1:]                                   # aligned to R (T, n)
    F_vol, vol = build_features(R, vol_window, med_window)  # (T,n,8), vol (T,n)
    state = make_state(vol, med_window)
    runmax = np.maximum.accumulate(np.where(np.isfinite(price), price, -np.inf), axis=0)
    dd = 1.0 - price / np.where(runmax > 0, runmax, np.nan)   # drawdown depth (T,n)
    mom5, mom21 = _roll_mean(R, 5), _roll_mean(R, 21)
    ma_ratio = np.log(price) - _roll_mean(np.log(np.clip(price, 1e-9, None)), 21)
    std_w = np.full((T, n), np.nan)
    for t in range(vol_window, T + 1):
        std_w[t - 1] = R[t - vol_window:t].std(axis=0)
    # general feature matrix: vol features + momentum + drawdown + trend + |r|
    F = np.concatenate(
        [F_vol,
         mom5[:, :, None], mom21[:, :, None], dd[:, :, None],
         ma_ratio[:, :, None], np.abs(R)[:, :, None]],
        axis=2,
    )
    return dict(R=R, price=price, vol=vol, state=state, dd=dd, std=std_w, F=F,
                med_window=med_window)


# --------------------------------------------------------------------------- #
# Event labelers: each returns S (T, n) in {0,1} with NaN where undefined
# --------------------------------------------------------------------------- #
def ev_vol_state(ctx):
    return ctx["state"].copy()


def ev_vol_transition(ctx):
    S = ctx["state"]
    T, n = S.shape
    tr = np.full((T, n), np.nan)
    tr[1:] = (S[1:] != S[:-1]).astype(float)
    tr[~np.isfinite(S)] = np.nan
    tr[1:][~np.isfinite(S[:-1])] = np.nan
    return tr


def ev_big_move(ctx, k=2.0):
    R, std = ctx["R"], ctx["std"]
    lab = (np.abs(R) > k * std).astype(float)
    lab[~np.isfinite(std)] = np.nan
    return lab


def ev_drawdown(ctx, x=0.05):
    dd = ctx["dd"]
    lab = (dd > x).astype(float)
    lab[~np.isfinite(dd)] = np.nan
    return lab


def ev_trend_up(ctx):
    R, price = ctx["R"], ctx["price"]
    ma = _roll_mean(np.log(np.clip(price, 1e-9, None)), 21)
    lab = (np.log(np.clip(price, 1e-9, None)) > ma).astype(float)
    lab[~np.isfinite(ma)] = np.nan
    return lab


EVENTS = {
    "vol_state": (ev_vol_state, "vol above trailing median (balanced)"),
    "vol_transition": (ev_vol_transition, "vol regime FLIPS calm<->turbulent (rare, hard)"),
    "big_move": (ev_big_move, "|return| > 2x trailing std (tail)"),
    "drawdown": (ev_drawdown, "in a drawdown deeper than 5% (persistent)"),
    "trend_up": (ev_trend_up, "price above its 21-day average (persistent)"),
}


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def fit_logistic_weighted(X, y, l2=1.0, lr=0.3, epochs=300, seed=42):
    """Class-weighted logistic regression (balances rare positives)."""
    rng = np.random.default_rng(seed)
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Xz = np.hstack([np.ones((len(X), 1)), (X - mu) / sd])
    p = float(np.clip(y.mean(), 1e-3, 1 - 1e-3))
    sw = np.where(y == 1, 0.5 / p, 0.5 / (1 - p))       # inverse-frequency weights
    sw = sw / sw.mean()
    w = rng.normal(0, 0.01, Xz.shape[1])
    reg = l2 * np.ones(Xz.shape[1]); reg[0] = 0.0
    m = len(y)
    for _ in range(epochs):
        pr = _sigmoid(Xz @ w)
        # both terms averaged over samples => lr*l2 can't blow up the weights
        grad = (Xz.T @ (sw * (pr - y)) + reg * w) / m
        w -= lr * grad
        w = np.clip(w, -50, 50)  # safety against any residual overflow
    return w, mu, sd


def predict_logistic(w, mu, sd, X):
    Xz = np.hstack([np.ones((len(X), 1)), (X - mu) / sd])
    return (_sigmoid(Xz @ w) >= 0.5).astype(float)


# --------------------------------------------------------------------------- #
# Metrics (imbalance-aware)
# --------------------------------------------------------------------------- #
def clf_metrics(y_true, y_pred):
    y_true = np.asarray(y_true); y_pred = np.asarray(y_pred)
    tp = float(np.sum((y_pred == 1) & (y_true == 1)))
    tn = float(np.sum((y_pred == 0) & (y_true == 0)))
    fp = float(np.sum((y_pred == 1) & (y_true == 0)))
    fn = float(np.sum((y_pred == 0) & (y_true == 1)))
    tot = tp + tn + fp + fn
    acc = (tp + tn) / tot if tot else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")      # true positive rate
    tnr = tn / (tn + fp) if (tn + fp) else float("nan")
    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    bal = np.nanmean([recall, tnr])
    f1 = (2 * precision * recall / (precision + recall)
          if (precision and recall and np.isfinite(precision) and np.isfinite(recall)) else 0.0)
    return dict(acc=acc, bal_acc=bal, precision=precision, recall=recall, f1=f1,
                base_rate=float(np.mean(y_true)))


# --------------------------------------------------------------------------- #
# Walk-forward evaluation of one event
# --------------------------------------------------------------------------- #
def evaluate_event(ctx, label_fn, train, step, horizon, logistic_cfg):
    F, S = ctx["F"], label_fn(ctx)
    T, n, k = F.shape
    start = 63 + 21
    yt = {m: [] for m in ("BaseRate(null)", "Persistence", "Logistic")}
    yp = {m: [] for m in yt}

    t = max(start, train)
    while t + horizon < T:
        # pooled training pairs: features at i -> label at i+horizon
        Xtr, ytr = [], []
        for i in range(t - train, t - horizon):
            m = np.isfinite(F[i]).all(1) & np.isfinite(S[i + horizon])
            if m.any():
                Xtr.append(F[i][m]); ytr.append(S[i + horizon][m])
        if not Xtr:
            t += step; continue
        Xtr = np.vstack(Xtr); ytr = np.concatenate(ytr)
        if len(np.unique(ytr)) < 2:      # need both classes to train
            t += step; continue
        w, mu, sd = fit_logistic_weighted(Xtr, ytr, **logistic_cfg)

        valid = np.isfinite(F[t]).all(1) & np.isfinite(S[t + horizon]) & np.isfinite(S[t])
        if valid.any():
            ytrue = S[t + horizon][valid]
            base = 1.0 if ytr.mean() >= 0.5 else 0.0
            preds = {
                "BaseRate(null)": np.full(valid.sum(), base),
                "Persistence": S[t][valid],
                "Logistic": predict_logistic(w, mu, sd, F[t][valid]),
            }
            for m in yt:
                yt[m].append(ytrue); yp[m].append(preds[m])
        t += step

    out = {}
    for m in yt:
        if yt[m]:
            out[m] = clf_metrics(np.concatenate(yt[m]), np.concatenate(yp[m]))
    return out


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
_G, _Y, _R, _B, _X = "\033[92m", "\033[93m", "\033[91m", "\033[1m", "\033[0m"


def print_event(name, desc, res):
    print(f"\n{_B}EVENT: {name}{_X}  ({desc})")
    br = next(iter(res.values()))["base_rate"]
    print(f"  base rate of event = {br:.1%}   (so plain accuracy is misleading if far from 50%)")
    print(f"  {'model':<16s}{'acc':>7s}{'bal_acc':>9s}{'prec':>7s}{'recall':>8s}{'F1':>7s}{'skill':>8s}")
    for m, d in res.items():
        skill = d["bal_acc"] - 0.5
        if m.startswith("BaseRate"):
            tag = f"{_R}floor{_X}"
        elif skill > 0.08:
            tag = f"{_G}+{skill:.2f}{_X}"
        elif skill > 0.02:
            tag = f"{_Y}+{skill:.2f}{_X}"
        else:
            tag = f"{_R}{skill:+.2f}{_X}"
        prec = f"{d['precision']:>7.1%}" if np.isfinite(d["precision"]) else f"{'  -  ':>7s}"
        print(f"  {m:<16s}{d['acc']:>7.1%}{d['bal_acc']:>9.1%}{prec}"
              f"{d['recall']:>8.1%}{d['f1']:>7.2f}   {tag}")


def run_grid(ctx, name, train, step, horizon):
    """Logistic hyperparameter sweep - shows accuracy plateauing (more training
    does NOT keep raising accuracy; the target's predictability is the ceiling)."""
    label_fn = EVENTS[name][0]
    print(f"\n{_B}HYPERPARAMETER GRID (logistic) on event '{name}'{_X}")
    print(f"  {'l2':>6s}{'lr':>6s}{'epochs':>8s}{'bal_acc':>9s}{'F1':>7s}")
    best = None
    for l2, lr, epochs in itertools.product([0.3, 1.0, 3.0, 10.0], [0.2, 0.5], [200, 600]):
        res = evaluate_event(ctx, label_fn, train, step, horizon,
                             dict(l2=l2, lr=lr, epochs=epochs))
        d = res.get("Logistic")
        if not d:
            continue
        print(f"  {l2:>6.1f}{lr:>6.1f}{epochs:>8d}{d['bal_acc']:>9.1%}{d['f1']:>7.2f}")
        if best is None or d["bal_acc"] > best[1]:
            best = ((l2, lr, epochs), d["bal_acc"])
    if best:
        print(f"\n  best: l2={best[0][0]}, lr={best[0][1]}, epochs={best[0][2]} "
              f"-> bal_acc {best[1]:.1%}")
        print("  Note how bal_acc plateaus across configs: once the useful features are in,")
        print("  more training/regularisation tweaking barely moves it. The TARGET sets the ceiling.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--demo", action="store_true", help="cached/synthetic 15-asset panel")
    src.add_argument("--ticker", help="a single Yahoo symbol via yfinance")
    ap.add_argument("--event", default="all", help="event name or 'all' (" + ", ".join(EVENTS) + ")")
    ap.add_argument("--vol-window", type=int, default=5)
    ap.add_argument("--med-window", type=int, default=63)
    ap.add_argument("--train", type=int, default=500)
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--horizon", type=int, default=1)
    ap.add_argument("--grid", action="store_true", help="run the logistic hyperparameter grid")
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
        px = close.dropna().to_numpy(dtype=float)[:, None]
        from data import MarketData, log_returns
        md = MarketData(P=px, R=log_returns(px), tickers=[args.ticker],
                        dates=close.dropna().index, source="yfinance")
        label = args.ticker
    else:
        md = load_market_data()
        label = f"{md.n}-asset panel ({md.source})"

    ctx = build_context(md, args.vol_window, args.med_window)
    print(f"\n{_B}Event prediction - {label}{_X}")
    print("  metric to trust = BALANCED accuracy (and skill = bal_acc - 0.5). Plain accuracy")
    print("  is inflated for rare events; recall/precision/F1 show if positives are caught.")

    if args.grid:
        ename = args.event if args.event in EVENTS else "vol_state"
        run_grid(ctx, ename, args.train, args.step, args.horizon)
        return

    names = list(EVENTS) if args.event == "all" else [args.event]
    for name in names:
        if name not in EVENTS:
            print(f"  unknown event {name!r}; choices: {', '.join(EVENTS)}")
            continue
        fn, desc = EVENTS[name]
        res = evaluate_event(ctx, fn, args.train, args.step, args.horizon,
                             dict(l2=1.0, lr=0.3, epochs=300))
        if res:
            print_event(name, desc, res)

    print(f"\n{_B}How to read it:{_X}")
    print("  skill = balanced accuracy - 0.5. >0 means the model beats a base-rate guesser")
    print("  even accounting for class imbalance. Persistent events (drawdown, trend, vol_state)")
    print("  score high; TRANSITIONS and tail events are genuinely hard -> skill near 0 is honest,")
    print("  not a bug. High plain 'acc' with ~0 recall = the model just predicts 'no event'.\n")


if __name__ == "__main__":
    main()

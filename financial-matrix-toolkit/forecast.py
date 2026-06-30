#!/usr/bin/env python3
"""forecast.py - honest h-step-ahead forecasting of ANY single time series.

Predict the LEVEL of a series H steps ahead - e.g. oil ~1 month out (21 trading
days), or next-quarter inflation - and HONESTLY report whether the forecast beats
the naive random-walk ("no change") null, with an uncertainty band.

Why the honesty matters: a price LEVEL is highly autocorrelated, so "next month =
this month" is already ~95% accurate. Quoting that 95% would fool you. The only
number that means anything is SKILL vs the random-walk null. For asset prices
skill is usually ~0 (you can't beat "no change"); for persistent macro series
(inflation, unemployment) skill is genuinely positive - that's where real
forecasting accuracy lives.

DATA SOURCES (run locally with open network):
  --ticker CL=F        a Yahoo symbol via yfinance     (CL=F/BZ=F oil, USO, AAPL, ^GSPC)
  --fred  CPIAUCSL     a FRED series via pandas_datareader (inflation, UNRATE, GDP, ...)
  --csv   data.csv     a CSV with a date column + a value column
  --demo  macro|oil    built-in synthetic series (NO network) to see the lesson

EXAMPLES:
  python forecast.py --demo macro --horizon 6
  python forecast.py --demo oil   --horizon 21
  python forecast.py --ticker CL=F --horizon 21           # oil ~1 month ahead
  python forecast.py --fred CPIAUCSL --horizon 12 --transform yoy   # CPI inflation, 1yr
  python forecast.py --csv mydata.csv --value-col rate --horizon 6

Dependencies: numpy/scipy/pandas (core). yfinance for --ticker, pandas_datareader
for --fred (install only if you use them).
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from core import rmse, set_global_seed, skill
from harness import _newey_west_tstat

Z80 = 1.2816  # 80% normal quantile for the prediction interval


# --------------------------------------------------------------------------- #
# Forecasting models: each takes a 1-D training array y and horizon h, returns
# the predicted LEVEL h steps after the last training point.
# --------------------------------------------------------------------------- #
def f_random_walk(y, h):
    """Null: no change. Best naive guess of a level is the last value."""
    return float(y[-1])


def f_drift(y, h):
    """Random walk + drift: extrapolate the average step."""
    return float(y[-1] + h * np.mean(np.diff(y)))


def _ridge_ar(y, h, p=6, ridge=1.0, mode="change"):
    """Direct h-step ridge autoregression.

    mode="level":  regress y[i+h]      on the last p levels.
    mode="change": regress y[i+h]-y[i] on the last p levels, then add to y[-1]
                   (usually better for trending / persistent series).
    """
    y = np.asarray(y, dtype=float)
    n = len(y)
    if n < p + h + 10:
        return f_random_walk(y, h)
    X, t = [], []
    for i in range(p, n - h):
        X.append(y[i - p:i][::-1])
        t.append(y[i + h] if mode == "level" else y[i + h] - y[i])
    X = np.array(X)
    t = np.array(t)
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Xz = (X - mu) / sd
    Xd = np.hstack([np.ones((len(Xz), 1)), Xz])
    beta = np.linalg.solve(Xd.T @ Xd + ridge * np.eye(Xd.shape[1]), Xd.T @ t)
    xlast = (y[-p:][::-1] - mu) / sd
    pred = float(np.concatenate([[1.0], xlast]) @ beta)
    return pred if mode == "level" else float(y[-1] + pred)


def f_ar_level(y, h):
    return _ridge_ar(y, h, mode="level")


def f_ar_change(y, h):
    return _ridge_ar(y, h, mode="change")


def f_kalman_trend(y, h, q_over_r=0.02):
    """Local linear trend (level + slope) Kalman filter; project h steps."""
    y = np.asarray(y, dtype=float)
    obs_var = np.var(np.diff(y)) + 1e-12
    level, slope = y[0], 0.0
    Pl, Ps = obs_var, obs_var
    R = obs_var
    Q = q_over_r * obs_var
    for t in range(1, len(y)):
        # predict
        level = level + slope
        Pl = Pl + Ps + Q
        Ps = Ps + Q
        # update on observation
        K = Pl / (Pl + R)
        resid = y[t] - level
        level = level + K * resid
        slope = slope + 0.1 * K * resid
        Pl = (1 - K) * Pl
    return float(level + h * slope)


MODELS = {
    "RandomWalk(null)": f_random_walk,
    "RW+drift": f_drift,
    "RidgeAR-level": f_ar_level,
    "RidgeAR-change": f_ar_change,
    "Kalman-trend": f_kalman_trend,
}


# --------------------------------------------------------------------------- #
# Walk-forward evaluation, h steps ahead
# --------------------------------------------------------------------------- #
def walk_forward_1d(y, h, model_fn, train_min, step):
    """Rolling out-of-sample: at each origin fit on y[:i], forecast y[i-1+h]."""
    y = np.asarray(y, dtype=float)
    n = len(y)
    preds, acts, rw = [], [], []
    i = train_min
    while i - 1 + h < n:
        tr = y[:i]
        try:
            preds.append(model_fn(tr, h))
        except Exception:
            i += step
            continue
        acts.append(y[i - 1 + h])
        rw.append(tr[-1])  # the random-walk reference for this origin
        i += step
    return np.array(preds), np.array(acts), np.array(rw)


def mape(pred, act):
    act = np.asarray(act, dtype=float)
    pred = np.asarray(pred, dtype=float)
    mask = np.abs(act) > 1e-9
    if not mask.any():
        return float("nan")
    return float(np.mean(np.abs((pred[mask] - act[mask]) / act[mask])))


# --------------------------------------------------------------------------- #
# Data loaders
# --------------------------------------------------------------------------- #
def load_ticker(sym, start="2005-01-01"):
    import yfinance as yf
    raw = yf.download(sym, start=start, progress=False, auto_adjust=True)
    if raw is None or len(raw) == 0:
        raise RuntimeError(f"yfinance returned no data for {sym!r}.")
    s = raw["Close"]
    s = s.iloc[:, 0] if isinstance(s, pd.DataFrame) else s
    return s.dropna()


def load_fred(series, start="1990-01-01"):
    from pandas_datareader import data as pdr
    s = pdr.DataReader(series, "fred", start=start)
    return s.iloc[:, 0].dropna()


def load_csv(path, date_col, value_col):
    df = pd.read_csv(path)
    if date_col and date_col in df.columns:
        df[date_col] = pd.to_datetime(df[date_col])
        df = df.sort_values(date_col).set_index(date_col)
    col = value_col or df.columns[-1]
    return df[col].dropna()


def demo_series(kind, n=600, seed=42):
    """Synthetic series so the lesson is visible with no network."""
    rng = np.random.default_rng(seed)
    if kind in ("oil", "price", "asset"):
        # driftless random-walk price: model skill should be ~0 (unpredictable level)
        n = max(n, 1500)
        r = rng.normal(0.0, 0.02, n)
        s = 70 * np.exp(np.cumsum(r))
        idx = pd.bdate_range("2010-01-01", periods=n)
        return pd.Series(s, index=idx, name="demo_oil(random-walk price)")
    # persistent, mean-reverting macro rate (e.g. inflation): skill should be > 0
    level = np.zeros(n)
    level[0] = 2.0
    for t in range(1, n):
        level[t] = 2.0 + 0.96 * (level[t - 1] - 2.0) + rng.normal(0, 0.15)
    idx = pd.period_range("1990-01", periods=n, freq="M").to_timestamp()
    return pd.Series(level, index=idx, name="demo_macro(persistent rate)")


def apply_transform(s, transform, periods_per_year):
    if transform == "none":
        return s
    if transform == "diff":
        return s.diff().dropna()
    if transform == "yoy":  # year-over-year % change (e.g. CPI index -> inflation)
        return (s.pct_change(periods_per_year) * 100).dropna()
    raise ValueError(f"unknown transform {transform!r}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--ticker", help="Yahoo symbol via yfinance (e.g. CL=F, USO, AAPL)")
    src.add_argument("--fred", help="FRED series id via pandas_datareader (e.g. CPIAUCSL)")
    src.add_argument("--csv", help="path to a CSV with a date + value column")
    src.add_argument("--demo", choices=["macro", "oil"], help="built-in synthetic series")
    ap.add_argument("--horizon", type=int, default=21, help="steps ahead to forecast (21d ~ 1 month)")
    ap.add_argument("--value-col", default=None, help="value column name for --csv")
    ap.add_argument("--date-col", default="Date", help="date column name for --csv")
    ap.add_argument("--transform", default="none", choices=["none", "diff", "yoy"],
                    help="'yoy' turns a price index into a % rate (e.g. CPI -> inflation)")
    ap.add_argument("--ppy", type=int, default=12, help="periods per year (for --transform yoy)")
    ap.add_argument("--train-min", type=int, default=None, help="min training points per window")
    ap.add_argument("--step", type=int, default=1, help="walk-forward step")
    args = ap.parse_args()
    set_global_seed(42)

    # --- load ---
    if args.ticker:
        s = load_ticker(args.ticker); name = args.ticker
    elif args.fred:
        s = load_fred(args.fred); name = f"FRED:{args.fred}"
    elif args.csv:
        s = load_csv(args.csv, args.date_col, args.value_col); name = args.csv
    else:
        s = demo_series(args.demo); name = s.name

    s = apply_transform(s, args.transform, args.ppy)
    y = s.to_numpy(dtype=float)
    h = args.horizon
    if len(y) < 60:
        raise SystemExit(f"Series too short ({len(y)} points) to forecast {h} ahead.")
    train_min = args.train_min or max(40, min(len(y) // 2, 250))

    print(f"\n\033[1mForecasting {name}\033[0m")
    print(f"  points={len(y)}  horizon={h} steps ahead  transform={args.transform}")
    print(f"  last observed value = {y[-1]:.4f}\n")

    # --- walk-forward skill table (with significance vs the no-change null) ---
    # h-step forecasts use OVERLAPPING windows, so the per-origin error series is
    # strongly autocorrelated; we test skill with a Newey-West t-test, lag >= h,
    # on the (rw_sq_error - model_sq_error) series. A positive point-estimate skill
    # that is not significant is reported as WITHIN NOISE, not as an edge.
    print(f"  {'model':<20s}{'MAPE':>9s}{'RMSE':>11s}{'skill vs RW':>13s}   verdict")
    nw_lag = max(5, h)
    n_candidates = sum(1 for m in MODELS if not m.startswith("RandomWalk"))
    alpha = 0.05 / max(1, n_candidates)  # Bonferroni across candidate models
    results = {}
    rw_rmse = None
    rw_sq = None
    # first pass: the null, to get its squared-error series
    for mname, fn in MODELS.items():
        pred, act, rwf = walk_forward_1d(y, h, fn, train_min, args.step)
        if len(pred) < 8:
            continue
        results[mname] = dict(fn=fn, pred=pred, act=act,
                              sq=(pred - act) ** 2, resid=(pred - act),
                              rmse=rmse(pred, act), mape=mape(pred, act))
        if mname.startswith("RandomWalk"):
            rw_rmse = results[mname]["rmse"]
            rw_sq = results[mname]["sq"]
    if rw_rmse is None or rw_sq is None:
        raise SystemExit("Could not evaluate the random-walk null.")

    best_name, best_sk = None, -1e9
    for mname, r in results.items():
        sk = skill(r["rmse"], rw_rmse)
        is_null = mname.startswith("RandomWalk")
        # significance of the loss reduction vs the no-change null
        d = rw_sq - r["sq"]  # positive => model has smaller squared error
        t_stat, p_val = (0.0, 1.0) if is_null else _newey_west_tstat(d, lag=nw_lag)
        r["skill"], r["t"], r["p"], r["significant"] = sk, t_stat, p_val, (sk > 0 and p_val < alpha)
        if not is_null and r["significant"] and sk > best_sk:
            best_sk, best_name = sk, mname
        if is_null:
            verdict = "\033[90m(this is the null)\033[0m"
        elif r["significant"]:
            verdict = f"\033[92mbeats no-change, SIGNIFICANT (p={p_val:.3f})\033[0m"
        elif sk > 0:
            verdict = f"\033[93m+{sk:.1%} but WITHIN NOISE (p={p_val:.2f})\033[0m"
        else:
            verdict = f"\033[91mworse than no-change ({sk:+.1%})\033[0m"
        print(f"  {mname:<20s}{r['mape']:>8.1%}{r['rmse']:>11.4g}{sk:>12.1%}   {verdict}")

    # --- actual forecast from the best SIGNIFICANT model (else the null) ---
    print()
    chosen = best_name if best_name else "RandomWalk(null)"
    fn = results[chosen]["fn"]
    point = fn(y, h)
    resid = results[chosen]["resid"]
    sd = float(np.std(resid)) if len(resid) else 0.0
    lo, hi = point - Z80 * sd, point + Z80 * sd
    print(f"  \033[1mFORECAST ({h} steps ahead), model = {chosen}:\033[0m")
    print(f"     point estimate : {point:.4f}")
    print(f"     80% interval   : [{lo:.4f} , {hi:.4f}]   (±{Z80*sd:.4f})")
    print(f"     no-change ref  : {y[-1]:.4f}")

    # --- honest interpretation ---
    print(f"\n  \033[1mHow to read this:\033[0m")
    if best_name is None:
        print("  No model SIGNIFICANTLY beats the no-change (random-walk) null. The best honest")
        print("  forecast is essentially the last value; the 80% band shows the real uncertainty.")
        print("  A small MAPE here is NOT skill - 'no change' achieves the same. Typical for asset")
        print("  PRICE LEVELS: the level is near-unpredictable beyond 'about the same as today'.")
        print("  (A positive but WITHIN-NOISE skill is sampling luck, not an edge - don't trust it.)")
    else:
        print(f"  '{best_name}' beats no-change by {best_sk:.1%} and it is STATISTICALLY SIGNIFICANT")
        print(f"  (p={results[best_name]['p']:.3f}, Newey-West lag {nw_lag}), so this series carries real,")
        print("  exploitable persistence/mean-reversion - typical of MACRO statistics. Trust the")
        print("  point estimate, but respect the 80% band.")
    print("  MAPE = average % error (looks good for levels but can be misleading).")
    print("  skill vs RW = % better than the naive 'no change' guess; only believe it if SIGNIFICANT.\n")


if __name__ == "__main__":
    main()

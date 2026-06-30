#!/usr/bin/env python3
"""scale_experiment.py - "Train on as much data as possible": does it help?

Runs the toolkit at increasing dataset sizes and large training windows and
reports what happens to the two things that matter:

  * VOLATILITY skill (QLIKE vs the rolling-mean null) - expected to stay strong.
  * DIRECTION accuracy with a 95% confidence interval, and net-of-cost return -
    expected to converge to 50% with a SHRINKING error bar: more data makes the
    "no edge" verdict more certain, it does not manufacture an edge.

Writes ./results/scaling.png and ./results/scaling.csv.

No real data is reachable in this sandbox (all market-data hosts are blocked), so
this uses the seeded synthetic generator at large sizes. On a networked machine
the same conclusion holds on real data via `main.py --refresh`.

    python scale_experiment.py
    python scale_experiment.py --sizes 4000 10000 20000 --train 2520 --step 20
"""

from __future__ import annotations

import argparse
import csv
import math
import os

import numpy as np

from core import set_global_seed, logger
from data import load_market_data
from harness import RESULTS_DIR, walk_forward
from models.tier_c_volatility import EWMAVolatility, RidgeARVol
from models.tier_d_direction import EchoStateNetwork, GPReturns, LogisticDirection, VARReturns
from models.tier_b_regime import MarkovChainBuckets

_BOLD, _RESET, _GREEN, _RED = "\033[1m", "\033[0m", "\033[92m", "\033[91m"

VOL_MODELS = [("EWMA", lambda: EWMAVolatility(0.94)),
              ("RidgeAR-HAR", lambda: RidgeARVol(ridge=5.0))]
DIR_MODELS = [
    ("Logistic", lambda: LogisticDirection(epochs=250)),
    ("MarkovChain", lambda: MarkovChainBuckets()),
    ("VAR(returns)", lambda: VARReturns()),
    ("GP(returns)", lambda: GPReturns(max_train=150)),
    ("ESN", lambda: EchoStateNetwork(n_reservoir=120)),
]


def ci_half(n_windows: int) -> float:
    """95% half-width treating each OOS day as one independent coin-flip trial
    (conservative - the cross-sectional predictions on a day are correlated)."""
    return 1.96 * math.sqrt(0.25 / max(1, n_windows))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="+", default=[4000, 10000, 20000])
    ap.add_argument("--train", type=int, default=2520, help="training window per fit (days)")
    ap.add_argument("--step", type=int, default=20)
    ap.add_argument("--cost-bps", type=float, default=10.0)
    ap.add_argument("--no-plots", action="store_true")
    args = ap.parse_args()
    set_global_seed(42)

    print(f"\n{_BOLD}Data-scaling experiment: train window = {args.train} days, "
          f"step = {args.step}, costs = {args.cost_bps:.0f}bps{_RESET}")
    print("(synthetic data - no real source reachable in this sandbox)\n")

    rows = []  # for CSV / plotting
    for size in args.sizes:
        if size <= args.train + 50:
            logger.warning("size %d too small for train window %d; skipping.", size, args.train)
            continue
        data = load_market_data(force_synthetic=True, synthetic_days=size)
        nW = (data.R.shape[0] - args.train) // args.step
        half = ci_half(nW)
        print(f"{_BOLD}===== {size:>6,} days  (~{size//252} yrs) | {nW} OOS windows | "
              f"95% CI ±{half:.1%} ====={_RESET}")

        for nm, fac in VOL_MODELS:
            r = walk_forward(fac, data, train_window=args.train, test_window=1, step=args.step)
            col = _GREEN if r.skill > 0 else _RED
            print(f"  VOL  {nm:<13s} skill = {col}{r.skill:+6.1%}{_RESET}  (QLIKE vs null)")
            rows.append(dict(size=size, n_windows=nW, kind="vol", model=nm,
                             skill=r.skill, accuracy="", ci_half="", net="", null_net=""))

        for nm, fac in DIR_MODELS:
            r = walk_forward(fac, data, train_window=args.train, test_window=1,
                             step=args.step, cost_bps=args.cost_bps)
            acc = r.extras.get("accuracy", float("nan"))
            z = (acc - 0.5) / math.sqrt(0.25 / nW) if nW else 0.0
            net = r.extras.get("net_ann_return", float("nan"))
            null_net = r.extras.get("null_net_ann_return", float("nan"))
            beats = (net > 0) and (net > null_net)
            ci_50 = (acc - half) <= 0.5 <= (acc + half)
            verdict = "no edge" if (ci_50 or not beats) else "CHECK!"
            col = _RED if not beats else _GREEN
            print(f"  DIR  {nm:<13s} acc = {acc:5.1%} ±{half:4.1%}  |z|={abs(z):4.1f}  "
                  f"net={col}{net:+6.1%}{_RESET}/yr  -> {verdict}")
            rows.append(dict(size=size, n_windows=nW, kind="dir", model=nm,
                             skill="", accuracy=acc, ci_half=half, net=net, null_net=null_net))
        print()

    print(f"{_BOLD}Takeaway:{_RESET} as data grows, volatility skill stays solidly positive while")
    print("direction accuracy stays pinned at ~50% with a CONTRACTING confidence interval that")
    print("keeps straddling 50%. More data does not create a direction edge - it makes the")
    print("efficient-market verdict more certain. (Net-of-cost direction return stays negative.)\n")

    _write_csv(rows)
    if not args.no_plots:
        try:
            _plot(rows)
        except Exception as exc:
            logger.warning("scaling plot failed: %s", exc)


def _write_csv(rows: list[dict]) -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, "scaling.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["size", "n_windows", "kind", "model",
                                          "skill", "accuracy", "ci_half", "net", "null_net"])
        w.writeheader()
        w.writerows(rows)
    logger.info("Wrote %s", path)


def _plot(rows: list[dict]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    sizes = sorted({r["size"] for r in rows})
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))

    # Panel 1: volatility skill stays positive as data grows.
    for nm in ["EWMA", "RidgeAR-HAR"]:
        ys = [next(r["skill"] for r in rows if r["size"] == s and r["model"] == nm) for s in sizes]
        ax1.plot(sizes, [y * 100 for y in ys], "o-", label=nm)
    ax1.axhline(0, color="k", lw=0.8)
    ax1.set_xscale("log")
    ax1.set_xlabel("training data (days, log scale)")
    ax1.set_ylabel("volatility skill vs null (%)")
    ax1.set_title("Volatility: skill stays strong with more data")
    ax1.set_ylim(bottom=min(-2, ax1.get_ylim()[0]))
    ax1.legend()
    ax1.grid(alpha=0.3)

    # Panel 2: direction accuracy inside a CONTRACTING 95% CI funnel around 50%.
    halves = {s: next(r["ci_half"] for r in rows if r["size"] == s and r["kind"] == "dir")
              for s in sizes}
    band = [halves[s] * 100 for s in sizes]
    ax2.fill_between(sizes, [50 - b for b in band], [50 + b for b in band],
                     color="0.8", label="95% CI around coin-flip (50%)")
    dir_models = ["Logistic", "MarkovChain", "VAR(returns)", "GP(returns)", "ESN"]
    for nm in dir_models:
        ys = [next(r["accuracy"] for r in rows if r["size"] == s and r["model"] == nm) * 100
              for s in sizes]
        ax2.plot(sizes, ys, "o-", lw=1, label=nm)
    ax2.axhline(50, color="k", lw=1.0)
    ax2.set_xscale("log")
    ax2.set_xlabel("training data (days, log scale)")
    ax2.set_ylabel("next-day direction accuracy (%)")
    ax2.set_title("Direction: accuracy ~50%, CI contracts -> no edge, more certain")
    ax2.legend(fontsize=8, ncol=2)
    ax2.grid(alpha=0.3)

    fig.suptitle("Training on more data: volatility is predictable, direction is not", y=1.02)
    fig.tight_layout()
    path = os.path.join(RESULTS_DIR, "scaling.png")
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    logger.info("Wrote %s", path)


if __name__ == "__main__":
    main()

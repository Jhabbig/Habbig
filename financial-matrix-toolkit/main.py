#!/usr/bin/env python3
"""main.py - Run the full 20-model matrix toolkit end to end.

    python main.py                 # use cached/synthetic data
    python main.py --refresh       # fetch real prices via yfinance (open network)
    python main.py --train 378 --step 5 --cost-bps 10

Pipeline: load prices -> log-returns -> walk-forward every forecasting model
against its NULL -> rank by skill -> plot to ./results/ -> print the honest
closing summary about direction predictability and the efficient-market floor.
"""

from __future__ import annotations

import argparse

import numpy as np

from core import set_global_seed, logger
from data import load_market_data
from harness import compare_all, print_result, walk_forward

# Tier C - volatility
from models.tier_c_volatility import (
    EWMAVolatility,
    RidgeARVol,
    RollingCovariance,
    VARLogVol,
)

# Tier A - structure (decomposition / detectors)
from models.tier_a_structure import (
    CorrelationChangeDetector,
    GraphLaplacian,
    NMFAbsReturns,
    PCAReturns,
    RMTFilter,
    TruncatedSVDReturns,
)

# Tier B - regime / state
from models.tier_b_regime import (
    AbsorbingDrawdownChain,
    DMDReturns,
    GaussianHMMRegime,
    KalmanLocalLevel,
    MarkovChainBuckets,
    MarkovSwitchingMeanVar,
)

# Tier D - direction (honesty tier)
from models.tier_d_direction import (
    EchoStateNetwork,
    GPReturns,
    LogisticDirection,
    VARReturns,
)

_BOLD = "\033[1m"
_RESET = "\033[0m"
_RED = "\033[91m"
_GREEN = "\033[92m"

# Forecasting models: (factory, tier). Run through walk_forward vs NULL.
FORECASTING_MODELS = [
    # Tier C - volatility (genuinely works)
    (lambda: EWMAVolatility(0.94), "C"),
    (lambda: RollingCovariance(63), "C"),
    (lambda: VARLogVol(21), "C"),
    (lambda: RidgeARVol(ridge=5.0), "C"),
    # Tier B - regime / state
    (lambda: GaussianHMMRegime(n_iter=40), "B"),
    (lambda: MarkovSwitchingMeanVar(n_iter=40), "B"),
    (lambda: MarkovChainBuckets(), "B"),
    (lambda: DMDReturns(rank=5), "B"),
    (lambda: KalmanLocalLevel(), "B"),
    # Tier D - direction (honesty tier)
    (lambda: LogisticDirection(epochs=250), "D"),
    (lambda: VARReturns(), "D"),
    (lambda: GPReturns(max_train=150), "D"),
    (lambda: EchoStateNetwork(n_reservoir=120), "D"),
]

# Decomposition / diagnostic models: fit, then report state() (no forecast).
DECOMP_MODELS = [
    (lambda: PCAReturns(), "A"),
    (lambda: RMTFilter(), "A"),
    (lambda: TruncatedSVDReturns(), "A"),
    (lambda: CorrelationChangeDetector(window=63), "A"),
    (lambda: GraphLaplacian(), "A"),
    (lambda: NMFAbsReturns(k=3), "A"),
    (lambda: AbsorbingDrawdownChain(), "B"),
]


def run_decompositions(data) -> None:
    print(f"\n{_BOLD}===== DECOMPOSITION / DETECTOR MODELS (no forecast; diagnostics) ====={_RESET}")
    for factory, tier in DECOMP_MODELS:
        try:
            m = factory().fit(data)
        except Exception as exc:
            logger.warning("%s failed: %s", factory().name, exc)
            continue
        st = m.state()
        print(f"\n  [{tier}] {_BOLD}{m.name}{_RESET}")
        _summarize_state(m.name, st)


def _summarize_state(name: str, st: dict) -> None:
    keys_of_interest = {
        "PCA(returns)": ["market_factor_share", "n_factors_90pct", "explained_variance_ratio"],
        "RMT-filter(MP)": ["lambda_plus", "n_signal_eigenvalues", "noise_fraction"],
        "TruncatedSVD": ["rank_90pct"],
        "CorrChangeDetector": ["n_events", "is_break_now", "last_z", "max_distance"],
        "GraphLaplacian": ["fiedler_value", "suggested_clusters", "cluster_labels"],
        "NMF(|returns|)": ["k", "relative_error", "cluster_membership"],
        "AbsorbingChain(recovery)": ["current_drawdown", "expected_recovery_days_now",
                                     "expected_recovery_days_by_depth"],
    }
    for k in keys_of_interest.get(name, list(st.keys())[:4]):
        if k not in st:
            continue
        v = st[k]
        if isinstance(v, np.ndarray):
            if v.size <= 16:
                print(f"        {k}: {np.round(v, 4)}")
            else:
                print(f"        {k}: array{v.shape}")
        elif isinstance(v, float):
            print(f"        {k}: {v:.4f}")
        else:
            print(f"        {k}: {v}")


def closing_summary(results) -> None:
    """The honest verdict: how many Tier-D direction models beat the null AFTER
    transaction costs. The expected, correct answer is ~zero."""
    tier_d = [r for r in results if r.tier == "D"]
    beat_after_cost = []
    for r in tier_d:
        e = r.extras
        if "net_ann_return" in e and not e.get("edge_below_cost", True):
            beat_after_cost.append(r.name)

    # volatility tier headline
    tier_c = [r for r in results if r.tier == "C"]
    vol_beats = sum(1 for r in tier_c if np.isfinite(r.skill) and r.skill > 0)

    print(f"\n{_BOLD}========================= CLOSING SUMMARY ============================{_RESET}")
    print(f"  Volatility (Tier C): {vol_beats}/{len(tier_c)} models beat the vol null "
          f"(volatility magnitude IS predictable - it clusters).")
    print(f"  Direction  (Tier D): {len(beat_after_cost)}/{len(tier_d)} models beat the "
          f"coin-flip / random-walk null AFTER {int(tier_d[0].extras.get('cost_bps', 10)) if tier_d else 10}bps costs.")
    if not beat_after_cost:
        print(f"\n  {_GREEN}{_BOLD}This is the correct, expected result.{_RESET}")
        print("  Zero direction models beat the null after costs. Return DIRECTION is")
        print("  near-unpredictable: this is the EFFICIENT-MARKET FLOOR, not a bug in the")
        print("  models. The toolkit's value is in the predictable structure it DID find -")
        print("  cross-asset correlation, market regime, and volatility magnitude.")
    else:
        print(f"\n  {_RED}{_BOLD}{len(beat_after_cost)} direction model(s) appear to beat the null "
              f"after costs: {beat_after_cost}.{_RESET}")
        print("  Treat with extreme suspicion: re-check for look-ahead bias, an under-")
        print("  powered null, or overfitting before believing any direction edge is real.")
    print(f"{_BOLD}====================================================================={_RESET}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the 20-model financial matrix toolkit.")
    ap.add_argument("--refresh", action="store_true", help="fetch real prices via yfinance")
    ap.add_argument("--train", type=int, default=252, help="walk-forward training window (days)")
    ap.add_argument("--step", type=int, default=5, help="walk-forward step (days)")
    ap.add_argument("--cost-bps", type=float, default=10.0, help="transaction cost per trade (bps)")
    ap.add_argument("--no-plots", action="store_true", help="skip matplotlib plots")
    args = ap.parse_args()

    set_global_seed(42)
    data = load_market_data(refresh=args.refresh)
    print(f"\n{_BOLD}Loaded {data.n} assets x {data.T} days | source={data.source} "
          f"| {data.meta.get('start')}..{data.meta.get('end')}{_RESET}")
    if data.source == "synthetic":
        print(f"  {_RED}NOTE: synthetic data (no network/cache). Run with --refresh on an "
              f"open network for real prices.{_RESET}")

    print(f"\n{_BOLD}===== FORECASTING MODELS (walk-forward, out-of-sample vs NULL) ====={_RESET}")
    results = []
    for factory, tier in FORECASTING_MODELS:
        try:
            res = walk_forward(factory, data, train_window=args.train,
                               test_window=1, step=args.step, cost_bps=args.cost_bps)
            results.append(res)
            print_result(res)
        except Exception as exc:
            logger.warning("%s failed and was skipped: %s", factory().name, exc)

    run_decompositions(data)
    compare_all(results, make_plots=not args.no_plots)
    closing_summary(results)


if __name__ == "__main__":
    main()

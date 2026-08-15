#!/usr/bin/env python3
"""predict_live.py - serve the trained pipeline: today's event probabilities.

Loads the readouts trained by pipeline.py (./trained/readout_*.npz) plus the
matching manifest, rebuilds the CURRENT feature vector (stage-1 track signals +
raw features at the latest origin), and prints each asset's probability for every
event - with the tuned operating threshold and an AUC-based trust flag.

    python predict_live.py --demo
    python predict_live.py --demo --event drawdown

Run pipeline.py first to create ./trained/. On a networked machine, retrain on
real data (pipeline.py --refresh once that flag is wired) before trusting output.
"""

from __future__ import annotations

import argparse
import json
import os

import numpy as np

from calibration import apply_calibrator, calibrator_from_arrays
from core import set_global_seed
from data import load_market_data
from pipeline import TRAINED_DIR, build_context, build_raw_at_origins, build_tracks
from predict_events import predict_proba_logistic

_B, _G, _Y, _R, _X = "\033[1m", "\033[92m", "\033[93m", "\033[91m", "\033[0m"


def load_manifest():
    path = os.path.join(TRAINED_DIR, "pipeline_manifest.json")
    if not os.path.exists(path):
        raise SystemExit("No trained pipeline found. Run:  python pipeline.py --demo")
    with open(path) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="use the cached/synthetic panel")
    ap.add_argument("--refresh", action="store_true", help="fetch real prices via yfinance first")
    ap.add_argument("--event", default="all", help="event name or 'all'")
    args = ap.parse_args()
    set_global_seed(42)

    man = load_manifest()
    md = load_market_data(refresh=args.refresh)
    # rebuild the CURRENT hybrid feature vector at the latest origin
    origins, X, _ = build_tracks(md, man["window"], man["stride"], verbose=False)
    ctx = build_context(md, vol_window=5, med_window=63)
    Xraw = build_raw_at_origins(ctx, origins)
    Xhyb = np.concatenate([X, Xraw], axis=2)
    x_now = Xhyb[-1]            # (n_assets, k) - features as of the latest origin
    t_now = origins[-1]
    asof = md.dates[t_now] if md.dates is not None else f"index {t_now}"

    events = list(man["events"]) if args.event == "all" else [args.event]
    print(f"\n{_B}Live event probabilities - {md.n} assets, as of {asof} (source={md.source}){_X}")
    print(f"  horizon={man['horizon']} day(s) ahead. 'flag' fires when P >= the tuned threshold.\n")

    for name in events:
        info = man["events"].get(name)
        if not info or not info.get("readout_file"):
            print(f"  {name}: no trained model"); continue
        d = np.load(os.path.join(TRAINED_DIR, info["readout_file"]), allow_pickle=True)
        raw = predict_proba_logistic(d["w"], d["mu"], d["sd"], x_now)
        prob = apply_calibrator(calibrator_from_arrays(d), raw)   # calibrated probabilities
        thr = info.get("tuned_threshold", 0.5)
        auc = info.get("auc", float("nan"))
        sig = info.get("auc_significant", False)
        # Two DIFFERENT questions, and AUC only answers the first. Ranking skill
        # (AUC) says the ordering of assets is meaningful; Brier skill says the
        # PROBABILITY itself beats climatology and is safe to size a bet on. An
        # event can pass the first and fail the second - vol_transition does.
        bss = info.get("brier_skill_score", float("nan"))
        bss_sig = info.get("brier_skill_significant", False)
        rank = (f"{_G}rank AUC {auc:.2f}{_X}" if sig
                else f"{_R}rank AUC {auc:.2f} (not significant){_X}")
        if bss_sig:
            prob_trust = f"{_G}P usable (BSS {bss:+.2f}){_X}"
        elif np.isfinite(bss) and bss > 0:
            prob_trust = f"{_Y}P WITHIN NOISE (BSS {bss:+.2f}){_X}"
        else:
            prob_trust = f"{_R}P NO better than base rate (BSS {bss:+.2f}){_X}"
        n_flag = int(np.sum(prob >= thr))
        print(f"  {_B}{name}{_X}  base rate {info['base_rate']:.0%}, threshold {thr:.2f}"
              f"  ->  {rank} | {prob_trust}")
        order = np.argsort(prob)[::-1]
        top = order[:5]
        cells = "  ".join(
            f"{md.tickers[a]}:{(_R if prob[a] >= thr else '')}{prob[a]:.0%}{_X if prob[a] >= thr else ''}"
            for a in top)
        print(f"     highest P: {cells}    ({n_flag}/{md.n} assets flagged)")
    print("\n  Read the two verdicts separately. Green rank AUC = the ORDERING across assets")
    print("  is meaningful, so use it to pick which names to watch. Green BSS = the PROBABILITY")
    print("  itself beats climatology, so it is safe to size on. An event can pass the first and")
    print("  fail the second: then rank with it, but do NOT treat its P(event) as a real number.")
    print("  A rare-event probability of 20% can still be the top signal.\n")


if __name__ == "__main__":
    main()

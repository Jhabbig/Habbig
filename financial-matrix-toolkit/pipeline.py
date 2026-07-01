#!/usr/bin/env python3
"""pipeline.py - the two-stage architecture: TRACK models -> EVENT readout.

Stage 1 (TRACKS): the matrix models (EWMA vol, rolling covariance, Gaussian-HMM
regime, PCA, RMT, DMD) are fit walk-forward and emit their forecasts/states at
each origin t - the predictable "trajectory" signals of the market, using only
data up to t (causal).

Stage 2 (EVENT READOUT): a class-weighted logistic reads those stage-1 signals as
features and predicts whether an event happens at t+h. Trained on PAST (track,
label) pairs only, with a purge gap so a label is never seen before it is realised.

This trains every model end-to-end, compares the model-output "track" features
against the raw-feature baseline, saves the trained pipeline to ./trained/, and
reports honest out-of-sample metrics per event.

    python pipeline.py --demo                 # train + evaluate on cached/synthetic panel
    python pipeline.py --demo --event big_move
    python pipeline.py --demo --stride 5 --window 504 --horizon 1
"""

from __future__ import annotations

import argparse
import json
import os

import numpy as np

from core import logger, set_global_seed
from data import load_market_data
from harness import slice_market_data
from models.tier_c_volatility import EWMAVolatility, RollingCovariance
from models.tier_b_regime import GaussianHMMRegime, DMDReturns
from models.tier_a_structure import PCAReturns, RMTFilter
from eventmetrics import best_threshold, bootstrap_auc_ci, prob_metrics
from predict_events import (
    EVENTS,
    build_context,
    clf_metrics,
    fit_logistic_weighted,
    predict_logistic,
    predict_proba_logistic,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
TRAINED_DIR = os.path.join(_HERE, "trained")
_B, _G, _Y, _R, _X = "\033[1m", "\033[92m", "\033[93m", "\033[91m", "\033[0m"


# --------------------------------------------------------------------------- #
# Stage 1 - build the TRACK signal matrix (causal, one fit per origin)
# --------------------------------------------------------------------------- #
TRACK_FEATURES = [
    # per-asset
    "ewma_var", "rollcov_var", "pca_loading", "dmd_pred_ret", "drawdown",
    "momentum21", "vol_rel",
    # market-level (broadcast to every asset at that origin)
    "hmm_crisis_prob", "pca_market_share", "rmt_noise_frac",
    "dmd_spectral_radius", "rollcov_frob_speed", "ewma_avg_corr",
]

# raw predict_events feature columns (order matches build_context's F)
RAW_FEATURES = [
    "logvol", "logvol_5", "logvol_21", "log_absr", "vol_rel_raw", "state",
    "state_5", "state_21", "mom5", "mom21", "drawdown_raw", "ma_ratio", "absr",
]


def _avg_offdiag(M):
    n = M.shape[0]
    if n < 2:
        return 0.0
    iu = np.triu_indices(n, 1)
    return float(np.nanmean(M[iu]))


def build_tracks(md, window, stride, hmm_iter=25, verbose=True):
    """Fit the stage-1 models at each origin and return the TRACK matrix.

    Returns origins (list of R-indices t), X (len(origins), n, k) track features,
    and the fitted final-window models (fit on the last window) for saving.
    """
    R = md.R
    T, n = R.shape
    origins, rows = [], []
    last_models = None
    t = window
    while t < T:
        sub = slice_market_data(md, t - window, t)  # returns R[t-window:t], causal
        try:
            ewma = EWMAVolatility(0.94).fit(sub); es = ewma.state()
            rc = RollingCovariance(63).fit(sub); rcs = rc.state()
            hmm = GaussianHMMRegime(n_states=2, n_iter=hmm_iter).fit(sub)
            pca = PCAReturns().fit(sub); ps = pca.state()
            rmt = RMTFilter().fit(sub); rs = rmt.state()
            dmd = DMDReturns(rank=5).fit(sub); dpred = np.atleast_2d(dmd.predict(1))[0]
        except Exception as exc:
            logger.warning("stage-1 fit failed at t=%d (%s); skipping origin.", t, exc)
            t += stride
            continue

        # market-level scalars (same for all assets at this origin)
        crisis_prob = float(hmm._hmm["filtered_last"][-1])  # last state = highest vol = crisis
        pca_share = float(ps["market_factor_share"])
        rmt_noise = float(rs["noise_fraction"])
        dmd_sr = float(dmd.state()["spectral_radius"])
        frob = float(rcs.get("frobenius_speed", 0.0) or 0.0)
        ewma_corr = _avg_offdiag(es["correlation"])

        # per-asset signals
        ewma_var = es["variance"]
        rc_var = rcs["variance"]
        load = ps["market_eigenportfolio"]
        load = load * np.sign(np.sum(load))  # orient so the market loads positive
        P = sub.P
        dd = 1.0 - P[-1] / np.maximum.accumulate(P, axis=0)[-1]
        mom = sub.R[-21:].mean(axis=0) if sub.R.shape[0] >= 21 else sub.R.mean(axis=0)
        vol = sub.R[-5:].std(axis=0)
        volrel = np.log(vol + 1e-8) - np.log(np.median(np.abs(sub.R[-63:]), axis=0) + 1e-8)

        feat = np.stack([
            ewma_var, rc_var, load, dpred, dd, mom, volrel,
            np.full(n, crisis_prob), np.full(n, pca_share), np.full(n, rmt_noise),
            np.full(n, dmd_sr), np.full(n, frob), np.full(n, ewma_corr),
        ], axis=1)  # (n, k)
        origins.append(t)
        rows.append(feat)
        last_models = dict(ewma=ewma, rc=rc, hmm=hmm, pca=pca, rmt=rmt, dmd=dmd)
        if verbose and len(origins) % 25 == 0:
            logger.info("stage-1 tracks: %d origins fit (t=%d/%d)", len(origins), t, T)
        t += stride
    X = np.stack(rows, axis=0)  # (n_origins, n, k)
    return origins, X, last_models


# --------------------------------------------------------------------------- #
# Stage 2 - purged walk-forward event readout on the track features
# --------------------------------------------------------------------------- #
def readout_event(origins, X, S, horizon, stride, min_train=60, logistic_cfg=None, purge=6):
    """Train the stage-2 readout on PAST track rows only (purged) and evaluate OOS.

    origins[i] = R-index t_i. The test feature at origin i is as-of index t_i-1
    (its window ends at t_i-1). A training label S[t_j + horizon] only becomes
    knowable at index t_j + horizon. To avoid look-ahead we require the training
    label to be realised strictly before the test feature time:

        origins[j] + horizon + purge <= t_i

    ``purge`` (default 6 >= the feature/label realisation lag: the vol_window of 5
    plus the 1-day feature offset) guarantees causality for ANY stride, including
    stride=1. Larger-than-needed purge only drops a few near-boundary training
    rows; it never leaks.
    """
    logistic_cfg = logistic_cfg or dict(l2=1.0, lr=0.3, epochs=250)
    origins = np.asarray(origins)
    n_orig, n, k = X.shape
    # label per (origin, asset)
    lab = np.full((n_orig, n), np.nan)
    for i, t in enumerate(origins):
        if t + horizon < S.shape[0]:
            lab[i] = S[t + horizon]

    yt, yprob, ythr = [], [], []
    for i in range(n_orig):
        t_i = origins[i]
        # purge: only rows whose label was knowable before the test feature time
        train_mask = origins + horizon + purge <= t_i
        if train_mask.sum() < min_train:
            continue
        Xtr, ytr = [], []
        for j in np.where(train_mask)[0]:
            m = np.isfinite(X[j]).all(1) & np.isfinite(lab[j])
            if m.any():
                Xtr.append(X[j][m]); ytr.append(lab[j][m])
        if not Xtr:
            continue
        Xtr = np.vstack(Xtr); ytr = np.concatenate(ytr)
        if len(np.unique(ytr)) < 2:
            continue
        w, mu, sd = fit_logistic_weighted(Xtr, ytr, **logistic_cfg)
        # tune the operating threshold on TRAINING predictions only (no leak)
        thr = best_threshold(ytr, predict_proba_logistic(w, mu, sd, Xtr), metric="bal")
        valid = np.isfinite(X[i]).all(1) & np.isfinite(lab[i])
        if valid.any():
            yt.append(lab[i][valid])
            yprob.append(predict_proba_logistic(w, mu, sd, X[i][valid]))
            ythr.append(np.full(valid.sum(), thr))
    if not yt:
        return None
    y = np.concatenate(yt); p = np.concatenate(yprob)
    thr = float(np.mean(np.concatenate(ythr)))          # avg tuned threshold (report)
    # thresholded metrics use each fit's own tuned threshold
    pred = (p >= np.concatenate(ythr)).astype(float)
    m = prob_metrics(y, p, threshold=0.5)               # threshold-free bundle (auc/ap/brier)
    tuned = prob_metrics(y, np.where(pred == 1, 1.0, 0.0), threshold=0.5)
    m["bal_acc"] = tuned["bal_acc"]; m["precision"] = tuned["precision"]
    m["recall"] = tuned["recall"]; m["f1"] = tuned["f1"]; m["threshold"] = thr
    lo, hi = bootstrap_auc_ci(y, p)
    m["auc_lo"], m["auc_hi"] = lo, hi
    return m


def readout_baserate(origins, S, horizon):
    """Base-rate null on the same origins/labels (majority class, expanding)."""
    origins = np.asarray(origins)
    yt, yp = [], []
    labels = [S[t + horizon] if t + horizon < S.shape[0] else np.full(S.shape[1], np.nan)
              for t in origins]
    for i in range(len(origins)):
        past = np.concatenate([labels[j][np.isfinite(labels[j])] for j in range(i)]) if i else np.array([])
        if past.size < 30 or not np.isfinite(labels[i]).any():
            continue
        base = 1.0 if past.mean() >= 0.5 else 0.0
        v = np.isfinite(labels[i])
        yt.append(labels[i][v]); yp.append(np.full(v.sum(), base))
    if not yt:
        return None
    return clf_metrics(np.concatenate(yt), np.concatenate(yp))


# --------------------------------------------------------------------------- #
# Raw-feature baseline (predict_events features) for the same purged scheme
# --------------------------------------------------------------------------- #
def build_raw_at_origins(ctx, origins):
    """The raw predict_events features sampled at the same origins (n_orig, n, k)."""
    F = ctx["F"]
    return np.stack([F[t] for t in origins], axis=0)


# --------------------------------------------------------------------------- #
# Train + report + save
# --------------------------------------------------------------------------- #
def train_and_save(md, origins, X, last_models, ctx, args):
    os.makedirs(TRAINED_DIR, exist_ok=True)
    Xraw = build_raw_at_origins(ctx, origins)
    Xhyb = np.concatenate([X, Xraw], axis=2)           # tracks + raw = full readout
    hyb_names = TRACK_FEATURES + RAW_FEATURES
    names = list(EVENTS) if args.event == "all" else [args.event]

    print(f"\n{_B}Two-stage event readout (HYBRID = tracks + raw), probabilistic metrics{_X}")
    print(f"  horizon={args.horizon}, window={args.window}, stride={args.stride}. AUC is threshold-free")
    print(f"  (0.5 = chance); AP = PR-AUC (baseline = base rate); Brier lower is better; the")
    print(f"  operating threshold is tuned on TRAINING data only.\n")
    print(f"  {'event':<15s}{'rate':>6s}{'AUC':>6s}{'AUC 95% CI':>14s}{'AP':>6s}"
          f"{'Brier':>7s}{'balAcc':>8s}{'recall':>8s}{'F1':>6s}")

    manifest = {"window": args.window, "stride": args.stride, "horizon": args.horizon,
                "track_features": TRACK_FEATURES, "raw_features": RAW_FEATURES, "events": {}}
    for name in names:
        if name not in EVENTS:
            print(f"  unknown event {name!r}"); continue
        S = EVENTS[name][0](ctx)
        track = readout_event(origins, X, S, args.horizon, args.stride)
        raw = readout_event(origins, Xraw, S, args.horizon, args.stride)
        hyb = readout_event(origins, Xhyb, S, args.horizon, args.stride)
        if hyb is None:
            print(f"  {name:<15s}  (insufficient data)"); continue
        sig = np.isfinite(hyb["auc_lo"]) and hyb["auc_lo"] > 0.5
        col = _G if sig else _R
        ci = f"[{hyb['auc_lo']:.2f},{hyb['auc_hi']:.2f}]"
        print(f"  {name:<15s}{hyb['base_rate']:>6.0%}{col}{hyb['auc']:>6.2f}{_X}{ci:>14s}"
              f"{hyb['ap']:>6.2f}{hyb['brier']:>7.3f}{hyb['bal_acc']:>8.1%}"
              f"{hyb['recall']:>8.0%}{hyb['f1']:>6.2f}")

        # train the FINAL readout on ALL rows using the hybrid features, save it + importances
        w, mu, sd = _fit_final(Xhyb, origins, S, args.horizon)
        importances = None
        if w is not None:
            path = os.path.join(TRAINED_DIR, f"readout_{name}.npz")
            np.savez(path, w=w, mu=mu, sd=sd, feature_names=np.array(hyb_names))
            coef = np.abs(w[1:])
            order = np.argsort(coef)[::-1][:5]
            importances = [(hyb_names[j], round(float(w[1 + j]), 3)) for j in order]
        manifest["events"][name] = {
            "readout_file": f"readout_{name}.npz" if w is not None else None,
            "base_rate": round(hyb["base_rate"], 4),
            "auc": round(hyb["auc"], 4),
            "auc_ci": [round(hyb["auc_lo"], 4), round(hyb["auc_hi"], 4)],
            "auc_significant": bool(sig),
            "average_precision": round(hyb["ap"], 4),
            "brier": round(hyb["brier"], 4),
            "tuned_balanced_accuracy": round(hyb["bal_acc"], 4),
            "tuned_recall": round(hyb["recall"], 4),
            "tuned_f1": round(hyb["f1"], 4),
            "tuned_threshold": round(hyb["threshold"], 4),
            "track_auc": round(track["auc"], 4) if track else None,
            "raw_auc": round(raw["auc"], 4) if raw else None,
            "top_features": importances,
        }

    with open(os.path.join(TRAINED_DIR, "pipeline_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    logger.info("Saved trained readouts + manifest to %s", TRAINED_DIR)

    # feature-importance highlights
    print(f"\n{_B}Top drivers per event (signed standardised logistic weight):{_X}")
    for name in names:
        info = manifest["events"].get(name, {})
        if info.get("top_features"):
            tops = ", ".join(f"{f}({c:+.2f})" for f, c in info["top_features"][:4])
            print(f"  {name:<15s} {tops}")

    print(f"\n{_B}How to read it:{_X}")
    print("  AUC (green if its 95% CI clears 0.5) = ranking skill, the honest headline for rare")
    print("  events. AP vs base rate shows precision-recall lift. Brier = probability quality.")
    print("  balAcc/recall/F1 use a threshold tuned on TRAIN only. HYBRID (tracks+raw) is saved;")
    print("  the manifest also records track-only vs raw-only AUC and each event's top drivers.")
    print(f"  Trained readouts -> {os.path.relpath(TRAINED_DIR, _HERE)}/ (predict_live.py serves them).\n")


def _fit_final(X, origins, S, horizon):
    origins = np.asarray(origins)
    Xtr, ytr = [], []
    for i, t in enumerate(origins):
        if t + horizon >= S.shape[0]:
            continue
        lab = S[t + horizon]
        m = np.isfinite(X[i]).all(1) & np.isfinite(lab)
        if m.any():
            Xtr.append(X[i][m]); ytr.append(lab[m])
    if not Xtr:
        return None, None, None
    Xtr = np.vstack(Xtr); ytr = np.concatenate(ytr)
    if len(np.unique(ytr)) < 2:
        return None, None, None
    return fit_logistic_weighted(Xtr, ytr, l2=1.0, lr=0.3, epochs=400)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--demo", action="store_true", help="cached/synthetic 15-asset panel")
    src.add_argument("--ticker", help="(single-asset stacking is weak; panel recommended)")
    ap.add_argument("--event", default="all", help="event name or 'all'")
    ap.add_argument("--window", type=int, default=504, help="stage-1 training window (days)")
    ap.add_argument("--stride", type=int, default=5, help="origin spacing (days)")
    ap.add_argument("--horizon", type=int, default=1, help="predict the event this many days ahead")
    ap.add_argument("--hmm-iter", type=int, default=25, help="HMM EM iterations per origin")
    args = ap.parse_args()
    set_global_seed(42)

    md = load_market_data()
    print(f"\n{_B}Training two-stage pipeline on {md.n}-asset panel ({md.source}), "
          f"{md.T} days{_X}")
    print("  Stage 1: fitting EWMA, RollingCov, GaussianHMM, PCA, RMT, DMD at each origin...")
    origins, X, last_models = build_tracks(md, args.window, args.stride, args.hmm_iter)
    print(f"  Stage 1 done: {len(origins)} origins x {X.shape[1]} assets x {X.shape[2]} track features.")
    ctx = build_context(md, vol_window=5, med_window=63)
    train_and_save(md, origins, X, last_models, ctx, args)


if __name__ == "__main__":
    main()

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
from calibration import (
    apply_calibrator,
    calibrator_to_arrays,
    expected_calibration_error,
    fit_calibrator,
    select_calibrator,
    reliability,
)
from eventmetrics import (
    best_threshold,
    bootstrap_auc_ci,
    bootstrap_metric_ci,
    brier,
    brier_skill_score,
    lift_efficiency,
    log_loss,
    max_lift_at_k,
    mcc,
    murphy_decomposition,
    prob_metrics,
    spiegelhalter_z,
)
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
def readout_event(origins, X, S, horizon, stride, min_train=60, logistic_cfg=None, purge=6,
                  calibration="platt"):
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

    yt, yraw, ycal, ythr, ygrp, chosen = [], [], [], [], [], []
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
        # calibrate on TRAINING predictions only, then tune threshold on calibrated probs
        ptr = predict_proba_logistic(w, mu, sd, Xtr)
        # "auto" picks platt vs isotonic on an inner split of the TRAINING rows,
        # so the choice never sees the evaluation set.
        method = select_calibrator(ptr, ytr) if calibration == "auto" else calibration
        chosen.append(method)
        cal = fit_calibrator(ptr, ytr, method=method)
        thr = best_threshold(ytr, apply_calibrator(cal, ptr), metric="bal")
        valid = np.isfinite(X[i]).all(1) & np.isfinite(lab[i])
        if valid.any():
            praw = predict_proba_logistic(w, mu, sd, X[i][valid])
            yt.append(lab[i][valid])
            yraw.append(praw)
            ycal.append(apply_calibrator(cal, praw))
            ythr.append(np.full(valid.sum(), thr))
            ygrp.append(np.full(valid.sum(), i))   # cluster id = forecast origin
    if not yt:
        return None
    y = np.concatenate(yt); p_raw = np.concatenate(yraw); p_cal = np.concatenate(ycal)
    thr_arr = np.concatenate(ythr)
    grp = np.concatenate(ygrp)
    pred = (p_cal >= thr_arr).astype(float)
    m = prob_metrics(y, p_raw, threshold=0.5)           # AUC/AP from raw scores (rank-based)
    tuned = prob_metrics(y, pred, threshold=0.5)         # tuned-threshold operating point
    m["bal_acc"] = tuned["bal_acc"]; m["precision"] = tuned["precision"]
    m["recall"] = tuned["recall"]; m["f1"] = tuned["f1"]
    m["threshold"] = float(np.mean(thr_arr))
    m["brier_raw"] = brier(y, p_raw); m["brier_cal"] = brier(y, p_cal)
    m["ece_raw"] = expected_calibration_error(y, p_raw)
    m["ece_cal"] = expected_calibration_error(y, p_cal)
    # CLUSTER bootstrap by origin: the 15 assets scored at one origin share a
    # market factor, so resampling rows i.i.d. would understate the uncertainty.
    lo, hi = bootstrap_auc_ci(y, p_raw, groups=grp)
    m["auc_lo"], m["auc_hi"] = lo, hi
    m["n_origins"] = int(len(np.unique(grp)))
    # effectiveness metrics - every one anchored to a null (see eventmetrics.py)
    m["bss_cal"] = brier_skill_score(y, p_cal)           # > 0 beats the base-rate null
    m["bss_lo"], m["bss_hi"] = bootstrap_metric_ci(
        y, p_cal, brier_skill_score, groups=grp)         # is that > 0 real, or noise?
    m["log_loss_cal"] = log_loss(y, p_cal)
    dec = murphy_decomposition(y, p_cal)
    m["reliability_cal"] = dec["reliability"]
    m["resolution_cal"] = dec["resolution"]
    m["uncertainty"] = dec["uncertainty"]
    m["spieg_z"], m["spieg_p"] = spiegelhalter_z(y, p_cal)
    m["mcc_tuned"] = mcc(y, pred, threshold=0.5)         # MCC at the tuned operating point
    # ks / precision_at_10 / lift_at_10 come from the prob_metrics bundle (raw scores).
    # Raw lift has a base-rate ceiling, so also record how much of it was captured.
    m["max_lift_at_10"] = max_lift_at_k(y, 0.10)
    m["lift_efficiency_10"] = lift_efficiency(y, p_raw, 0.10)
    # which calibrator the per-origin fits actually selected (constant unless "auto")
    m["calibrator_used"] = (max(set(chosen), key=chosen.count) if chosen else calibration)
    m["y"] = y; m["p_cal"] = p_cal                        # for the reliability plot
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
                "calibration": args.calibration,
                "track_features": TRACK_FEATURES, "raw_features": RAW_FEATURES, "events": {}}
    reliab = []
    for name in names:
        if name not in EVENTS:
            print(f"  unknown event {name!r}"); continue
        S = EVENTS[name][0](ctx)
        cal = args.calibration
        track = readout_event(origins, X, S, args.horizon, args.stride, calibration=cal)
        raw = readout_event(origins, Xraw, S, args.horizon, args.stride, calibration=cal)
        hyb = readout_event(origins, Xhyb, S, args.horizon, args.stride, calibration=cal)
        if hyb is None:
            print(f"  {name:<15s}  (insufficient data)"); continue
        sig = np.isfinite(hyb["auc_lo"]) and hyb["auc_lo"] > 0.5
        bss_sig = np.isfinite(hyb["bss_lo"]) and hyb["bss_lo"] > 0.0
        col = _G if sig else _R
        ci = f"[{hyb['auc_lo']:.2f},{hyb['auc_hi']:.2f}]"
        print(f"  {name:<15s}{hyb['base_rate']:>6.0%}{col}{hyb['auc']:>6.2f}{_X}{ci:>14s}"
              f"{hyb['ap']:>6.2f}{hyb['brier_cal']:>7.3f}{hyb['bal_acc']:>8.1%}"
              f"{hyb['recall']:>8.0%}{hyb['f1']:>6.2f}")
        reliab.append((name, hyb["y"], hyb["p_cal"]))

        # train the FINAL readout on ALL rows (hybrid features) + a final calibrator; save
        w, mu, sd, fcal = _fit_final(Xhyb, origins, S, args.horizon, calibration=cal)
        importances = None
        if w is not None:
            path = os.path.join(TRAINED_DIR, f"readout_{name}.npz")
            np.savez(path, w=w, mu=mu, sd=sd, feature_names=np.array(hyb_names),
                     tuned_threshold=np.array(hyb["threshold"]),
                     **calibrator_to_arrays(fcal))
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
            "brier_raw": round(hyb["brier_raw"], 4),
            "brier_calibrated": round(hyb["brier_cal"], 4),
            "ece_raw": round(hyb["ece_raw"], 4),
            "ece_calibrated": round(hyb["ece_cal"], 4),
            "calibration": cal,
            "tuned_balanced_accuracy": round(hyb["bal_acc"], 4),
            "tuned_recall": round(hyb["recall"], 4),
            "tuned_f1": round(hyb["f1"], 4),
            "tuned_threshold": round(hyb["threshold"], 4),
            "tuned_mcc": round(hyb["mcc_tuned"], 4),
            "brier_skill_score": round(hyb["bss_cal"], 4),
            "brier_skill_ci": [round(hyb["bss_lo"], 4), round(hyb["bss_hi"], 4)],
            "brier_skill_significant": bool(bss_sig),
            "log_loss_calibrated": round(hyb["log_loss_cal"], 4),
            "murphy": {
                "reliability": round(hyb["reliability_cal"], 5),
                "resolution": round(hyb["resolution_cal"], 5),
                "uncertainty": round(hyb["uncertainty"], 5),
            },
            "calibrator_used": hyb.get("calibrator_used", cal),
            "spiegelhalter_z": round(hyb["spieg_z"], 3),
            "spiegelhalter_p": round(hyb["spieg_p"], 4),
            "calibration_consistent": bool(np.isfinite(hyb["spieg_p"]) and hyb["spieg_p"] >= 0.05),
            "ks": round(hyb["ks"], 4),
            "precision_at_10pct": round(hyb["precision_at_10"], 4),
            "lift_at_10pct": round(hyb["lift_at_10"], 4),
            "max_lift_at_10pct": round(hyb["max_lift_at_10"], 4),
            "lift_efficiency_at_10pct": round(hyb["lift_efficiency_10"], 4),
            "n_origins": hyb["n_origins"],
            "track_auc": round(track["auc"], 4) if track else None,
            "raw_auc": round(raw["auc"], 4) if raw else None,
            "track_brier_skill_score": round(track["bss_cal"], 4) if track else None,
            "raw_brier_skill_score": round(raw["bss_cal"], 4) if raw else None,
            "top_features": importances,
        }

    with open(os.path.join(TRAINED_DIR, "pipeline_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    logger.info("Saved trained readouts + manifest to %s", TRAINED_DIR)

    # calibration summary: Brier and ECE, raw -> calibrated (lower = better)
    print(f"\n{_B}Calibration ({args.calibration}): does P(event) mean what it says?{_X}")
    print(f"  {'event':<15s}{'Brier raw->cal':>18s}{'ECE raw->cal':>16s}")
    for name in names:
        info = manifest["events"].get(name, {})
        if "brier_raw" in info:
            print(f"  {name:<15s}{info['brier_raw']:>8.3f} -> {info['brier_calibrated']:<6.3f}"
                  f"{info['ece_raw']:>9.3f} -> {info['ece_calibrated']:<6.3f}")
    _plot_reliability(reliab)

    # effectiveness summary: every number anchored to a no-skill null, and every
    # verdict backed by a cluster-bootstrap CI rather than a point estimate
    print(f"\n{_B}Prediction effectiveness (vs the always-predict-base-rate null):{_X}")
    print(f"  {'event':<15s}{'BSS':>7s}{'BSS 95% CI':>17s}{'MCC':>6s}{'KS':>6s}"
          f"{'lift@10%':>10s}{'of max':>8s}   verdict")
    for name in names:
        info = manifest["events"].get(name, {})
        if "brier_skill_score" not in info:
            continue
        bss = info["brier_skill_score"]
        lo, hi = info["brier_skill_ci"]
        # three honest states, mirroring harness.py's after-cost edge verdict
        if bss <= 0:
            verdict = f"{_R}NO SKILL vs base rate{_X}"
        elif not info["brier_skill_significant"]:
            verdict = f"{_Y}positive but WITHIN NOISE{_X}"
        else:
            verdict = f"{_G}SIGNIFICANT skill{_X}"
        print(f"  {name:<15s}{bss:>7.2f}{f'[{lo:+.2f},{hi:+.2f}]':>17s}"
              f"{info['tuned_mcc']:>6.2f}{info['ks']:>6.2f}"
              f"{info['lift_at_10pct']:>9.1f}x{info['lift_efficiency_at_10pct']:>7.0%}"
              f"   {verdict}")
    print("  BSS = Brier skill vs climatology (0 = knowing only the base rate, 1 = perfect).")
    print("  Its 95% CI is a CLUSTER bootstrap over forecast origins - the 15 assets scored at")
    print("  one origin share a market factor, so an i.i.d. row bootstrap would be too narrow.")
    print("  A positive BSS whose CI still includes 0 is NOT skill; that is the yellow state.")
    print("  MCC at the tuned threshold (0 = chance under any imbalance). lift@10% = how many")
    print("  times more events the top-decile alert list catches than random flagging - but")
    print("  lift has a base-rate ceiling, so 'of max' is what compares across events.")

    print(f"\n{_B}Is the calibration honest? (Spiegelhalter z-test){_X}")
    print(f"  {'event':<15s}{'ECE':>7s}{'z':>8s}{'p':>8s}   verdict")
    for name in names:
        info = manifest["events"].get(name, {})
        if "spiegelhalter_z" not in info:
            continue
        ok = info["calibration_consistent"]
        v = (f"{_G}consistent with perfect calibration{_X}" if ok
             else f"{_Y}detectably miscalibrated (ECE is small but real){_X}")
        print(f"  {name:<15s}{info['ece_calibrated']:>7.3f}{info['spiegelhalter_z']:>8.2f}"
              f"{info['spiegelhalter_p']:>8.3f}   {v}")
    print("  A small ECE alone is not proof of honesty - only the test says whether the gap")
    print("  is bigger than sampling noise. Note the z-test assumes independent rows, so with")
    print("  correlated assets pooled it errs toward flagging miscalibration too eagerly.")

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


def _fit_final(X, origins, S, horizon, calibration="platt"):
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
        return None, None, None, None
    Xtr = np.vstack(Xtr); ytr = np.concatenate(ytr)
    if len(np.unique(ytr)) < 2:
        return None, None, None, None
    w, mu, sd = fit_logistic_weighted(Xtr, ytr, l2=1.0, lr=0.3, epochs=400)
    ptr_final = predict_proba_logistic(w, mu, sd, Xtr)
    method = select_calibrator(ptr_final, ytr) if calibration == "auto" else calibration
    fcal = fit_calibrator(ptr_final, ytr, method=method)
    return w, mu, sd, fcal


def _plot_reliability(reliab, outdir=None):
    """Reliability diagram (calibrated P vs observed frequency) per event."""
    if not reliab:
        return
    outdir = outdir or os.path.join(_HERE, "results")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return
    os.makedirs(outdir, exist_ok=True)
    cols = min(3, len(reliab))
    rows = int(np.ceil(len(reliab) / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(4 * cols, 3.4 * rows), squeeze=False)
    for ax, (name, y, p) in zip(axes.ravel(), reliab):
        mp, ob, cnt = reliability(y, p, n_bins=10)
        ax.plot([0, 1], [0, 1], "k--", lw=0.8, label="perfect")
        mask = cnt > 0
        ax.plot(mp[mask], ob[mask], "o-", color="#1f77b4", label="model")
        ece = expected_calibration_error(y, p)
        ax.set_title(f"{name}  (ECE={ece:.3f})", fontsize=9)
        ax.set_xlabel("predicted P"); ax.set_ylabel("observed freq")
        ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.legend(fontsize=7)
    for ax in axes.ravel()[len(reliab):]:
        ax.axis("off")
    fig.suptitle("Calibration reliability: points on the diagonal = trustworthy probabilities", y=1.0)
    fig.tight_layout()
    path = os.path.join(outdir, "reliability_pipeline.png")
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    logger.info("Wrote %s", path)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="cached/synthetic 15-asset panel")
    ap.add_argument("--refresh", action="store_true",
                    help="fetch REAL prices via yfinance (open network) and train on them")
    ap.add_argument("--event", default="all", help="event name or 'all'")
    ap.add_argument("--window", type=int, default=504, help="stage-1 training window (days)")
    ap.add_argument("--stride", type=int, default=5, help="origin spacing (days)")
    ap.add_argument("--horizon", type=int, default=1, help="predict the event this many days ahead")
    ap.add_argument("--hmm-iter", type=int, default=25, help="HMM EM iterations per origin")
    ap.add_argument("--calibration", default="auto", choices=["platt", "isotonic", "auto", "none"],
                    help="probability calibrator (fit on training data only); 'auto' picks platt vs isotonic per event on an inner training split")
    args = ap.parse_args()
    set_global_seed(42)

    md = load_market_data(refresh=args.refresh)
    if args.refresh and md.source != "yfinance":
        print(f"  {_R}NOTE: --refresh could not fetch real data (blocked network?); "
              f"using {md.source}.{_X}")
    print(f"\n{_B}Training two-stage pipeline on {md.n}-asset panel ({md.source}), "
          f"{md.T} days{_X}")
    print("  Stage 1: fitting EWMA, RollingCov, GaussianHMM, PCA, RMT, DMD at each origin...")
    origins, X, last_models = build_tracks(md, args.window, args.stride, args.hmm_iter)
    print(f"  Stage 1 done: {len(origins)} origins x {X.shape[1]} assets x {X.shape[2]} track features.")
    ctx = build_context(md, vol_window=5, med_window=63)
    train_and_save(md, origins, X, last_models, ctx, args)


if __name__ == "__main__":
    main()

"""eventmetrics.py - threshold-free, imbalance-aware metrics for event models.

For rare events, accuracy at a fixed 0.5 threshold is nearly useless. These are
the metrics that actually matter:

    roc_auc            - ranking quality, threshold-free (0.5 = chance)
    average_precision  - area under precision-recall (PR-AUC); the right summary
                         for rare positives (baseline = base rate, not 0.5)
    brier              - calibration + sharpness of the probabilities (lower better)
    best_threshold     - operating point that maximises balanced accuracy / F1,
                         chosen ON TRAINING DATA ONLY then applied out-of-sample
    bootstrap_auc_ci   - 95% CI on AUC to test whether ranking skill is real
    bootstrap_auc_ci_clustered - same, but resampling whole time-origin clusters
                         (in moving blocks) for correlated panel predictions
"""

from __future__ import annotations

import numpy as np


def roc_auc(y_true, scores) -> float:
    """Area under the ROC curve via the Mann-Whitney U statistic (tie-aware)."""
    y = np.asarray(y_true, dtype=float)
    s = np.asarray(scores, dtype=float)
    mask = np.isfinite(y) & np.isfinite(s)
    y, s = y[mask], s[mask]
    n_pos = float(np.sum(y == 1))
    n_neg = float(np.sum(y == 0))
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    try:
        from scipy.stats import rankdata
        r = rankdata(s)  # average ranks handle ties correctly
    except Exception:  # pragma: no cover - scipy always present here
        order = np.argsort(s, kind="mergesort")
        r = np.empty_like(s)
        r[order] = np.arange(1, len(s) + 1)
    return float((np.sum(r[y == 1]) - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def average_precision(y_true, scores) -> float:
    """Average precision (area under the precision-recall curve)."""
    y = np.asarray(y_true, dtype=float)
    s = np.asarray(scores, dtype=float)
    mask = np.isfinite(y) & np.isfinite(s)
    y, s = y[mask], s[mask]
    n_pos = float(np.sum(y == 1))
    if n_pos == 0:
        return float("nan")
    order = np.argsort(-s, kind="mergesort")
    y = y[order]
    tp = np.cumsum(y)
    fp = np.cumsum(1 - y)
    precision = tp / np.maximum(tp + fp, 1e-12)
    # AP = mean precision at the ranks where a true positive is retrieved
    return float(np.sum(precision * y) / n_pos)


def brier(y_true, proba) -> float:
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(proba, dtype=float)
    mask = np.isfinite(y) & np.isfinite(p)
    if not mask.any():
        return float("nan")
    return float(np.mean((p[mask] - y[mask]) ** 2))


def best_threshold(y_true, scores, metric: str = "bal", n_grid: int = 101) -> float:
    """Threshold that maximises balanced accuracy ('bal') or F1 ('f1').

    Uses a quantile grid of the scores for speed. MUST be called on TRAINING data
    only; applying the returned threshold to test data keeps the evaluation honest.
    """
    y = np.asarray(y_true, dtype=float)
    s = np.asarray(scores, dtype=float)
    mask = np.isfinite(y) & np.isfinite(s)
    y, s = y[mask], s[mask]
    if len(np.unique(y)) < 2:
        return 0.5
    grid = np.quantile(s, np.linspace(0.02, 0.98, n_grid))
    grid = np.unique(grid)
    best_t, best_v = 0.5, -np.inf
    P = np.sum(y == 1)
    N = np.sum(y == 0)
    for t in grid:
        pred = s >= t
        tp = np.sum(pred & (y == 1))
        fp = np.sum(pred & (y == 0))
        fn = P - tp
        tpr = tp / P if P else 0.0
        tnr = (N - fp) / N if N else 0.0
        if metric == "f1":
            prec = tp / (tp + fp) if (tp + fp) else 0.0
            val = 2 * prec * tpr / (prec + tpr) if (prec + tpr) else 0.0
        else:  # balanced accuracy
            val = 0.5 * (tpr + tnr)
        if val > best_v:
            best_v, best_t = val, float(t)
    return best_t


def bootstrap_auc_ci(y_true, scores, n_boot: int = 500, seed: int = 42, alpha: float = 0.05):
    """Percentile bootstrap CI for AUC. Returns (lo, hi). If lo > 0.5 the ranking
    skill is statistically distinguishable from chance."""
    y = np.asarray(y_true, dtype=float)
    s = np.asarray(scores, dtype=float)
    mask = np.isfinite(y) & np.isfinite(s)
    y, s = y[mask], s[mask]
    n = len(y)
    if n < 20 or len(np.unique(y)) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    aucs = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        yi, si = y[idx], s[idx]
        if len(np.unique(yi)) < 2:
            continue
        aucs.append(roc_auc(yi, si))
    if not aucs:
        return (float("nan"), float("nan"))
    lo = float(np.percentile(aucs, 100 * alpha / 2))
    hi = float(np.percentile(aucs, 100 * (1 - alpha / 2)))
    return (lo, hi)


def bootstrap_auc_ci_clustered(y_true, scores, groups, n_boot: int = 500, seed: int = 42,
                               alpha: float = 0.05, block: int = 1):
    """Cluster/moving-block bootstrap CI for AUC on panel predictions.

    Pooled panel predictions are NOT i.i.d.: every asset observed at the same
    time origin shares that day's market conditions, and nearby origins are
    serially dependent, so resampling observations independently (as
    ``bootstrap_auc_ci`` does) understates the CI width by up to
    sqrt(cluster size). Here whole clusters -- ``groups`` gives the time-origin
    id of each observation -- are resampled together, in moving blocks of
    ``block`` consecutive origins (origin order = sorted group ids), mirroring
    the block bootstrap used for the Sharpe test in backtest.py.

    Returns (lo, hi); lo > 0.5 means the ranking skill is distinguishable from
    chance under the panel's dependence structure.
    """
    y = np.asarray(y_true, dtype=float)
    s = np.asarray(scores, dtype=float)
    g = np.asarray(groups)
    mask = np.isfinite(y) & np.isfinite(s)
    y, s, g = y[mask], s[mask], g[mask]
    if len(y) < 20 or len(np.unique(y)) < 2:
        return (float("nan"), float("nan"))
    uniq = np.unique(g)  # sorted ascending = time order for origin indices
    idx_by_group = [np.where(g == u)[0] for u in uniq]
    G = len(uniq)
    block = int(max(1, min(block, G)))
    n_blocks = int(np.ceil(G / block))
    rng = np.random.default_rng(seed)
    aucs = []
    for _ in range(n_boot):
        starts = rng.integers(0, G - block + 1, n_blocks)
        take = np.concatenate([np.arange(st, st + block) for st in starts])[:G]
        idx = np.concatenate([idx_by_group[j] for j in take])
        yi, si = y[idx], s[idx]
        if len(np.unique(yi)) < 2:
            continue
        aucs.append(roc_auc(yi, si))
    if not aucs:
        return (float("nan"), float("nan"))
    lo = float(np.percentile(aucs, 100 * alpha / 2))
    hi = float(np.percentile(aucs, 100 * (1 - alpha / 2)))
    return (lo, hi)


def prob_metrics(y_true, proba, threshold: float = 0.5) -> dict:
    """Full probabilistic + thresholded metric bundle for one event model."""
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(proba, dtype=float)
    pred = (p >= threshold).astype(float)
    tp = float(np.sum((pred == 1) & (y == 1)))
    tn = float(np.sum((pred == 0) & (y == 0)))
    fp = float(np.sum((pred == 1) & (y == 0)))
    fn = float(np.sum((pred == 0) & (y == 1)))
    tpr = tp / (tp + fn) if (tp + fn) else float("nan")
    tnr = tn / (tn + fp) if (tn + fp) else float("nan")
    prec = tp / (tp + fp) if (tp + fp) else float("nan")
    f1 = (2 * prec * tpr / (prec + tpr)
          if (prec and tpr and np.isfinite(prec) and np.isfinite(tpr)) else 0.0)
    return dict(
        auc=roc_auc(y, p),
        ap=average_precision(y, p),
        brier=brier(y, p),
        bal_acc=np.nanmean([tpr, tnr]),
        precision=prec, recall=tpr, f1=f1,
        base_rate=float(np.mean(y)), threshold=threshold,
    )

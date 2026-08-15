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

Effectiveness metrics - every one is anchored to a null or a significance test,
so a number is only ever "good" relative to what a no-skill forecaster scores:

    log_loss             - cross-entropy; the other proper scoring rule (lower better)
    brier_skill_score    - 1 - Brier/Brier(always-predict-base-rate); > 0 means the
                           probabilities BEAT the climatology null, < 0 means the
                           model is worse than knowing nothing but the base rate
    murphy_decomposition - Brier = reliability - resolution + uncertainty; separates
                           "is it honest?" (reliability, lower better) from "does it
                           discriminate?" (resolution, higher better) from the
                           irreducible difficulty of the target (uncertainty)
    spiegelhalter_z      - significance test of calibration: |z| < 1.96 means the
                           probabilities are statistically indistinguishable from
                           perfectly calibrated
    mcc                  - Matthews correlation at a threshold; 0 = chance even under
                           heavy class imbalance (a base-rate guesser scores 0)
    ks_statistic         - max separation between the score distributions of the two
                           classes (0 = none, 1 = perfect)
    precision_at_k       - precision inside the top-k% highest scores: "of the days
                           the model flags loudest, how many were real?"
    lift_at_k            - precision_at_k / base rate; 1.0 = no better than random
                           flagging, the honest multiplier for rare-event alerts
    lift_efficiency      - lift_at_k / its base-rate-dependent CEILING; the only
                           form of lift that is comparable ACROSS events

None of these is trusted as a point estimate. ``bootstrap_metric_ci`` puts a
confidence interval on any of them, and - because the readout pools correlated
assets at each origin - it takes a ``groups`` argument so whole origins are
resampled together. An i.i.d. row bootstrap over pooled assets returns a CI that
is too narrow and would let noise pass as skill.
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


def bootstrap_metric_ci(y_true, scores, stat, groups=None, n_boot: int = 500,
                        seed: int = 42, alpha: float = 0.05):
    """Percentile bootstrap CI for ANY metric ``stat(y, s) -> float``.

    ``groups`` turns this into a CLUSTER bootstrap: pass one group id per
    observation (here, the forecast origin) and whole groups are resampled
    together, so observations that share a group keep their dependence.

    This matters. The event readout pools every asset at every origin into one
    flat vector, but those assets share a market factor (mean pairwise
    correlation ~0.30 on the cached panel), so rows from the same origin are far
    from independent. Resampling rows i.i.d. pretends the sample carries more
    information than it does and returns a CI that is too NARROW - the classic
    way to crown noise as skill, which is exactly what this toolkit exists to
    refuse. Always pass ``groups`` when the rows are pooled across assets.
    """
    y_all = np.asarray(y_true, dtype=float)
    s_all = np.asarray(scores, dtype=float)
    mask = np.isfinite(y_all) & np.isfinite(s_all)
    y, s = y_all[mask], s_all[mask]
    n = len(y)
    if n < 20 or len(np.unique(y)) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)

    if groups is None:
        def draw():                                   # i.i.d. rows
            return rng.integers(0, n, n)
    else:
        g = _masked_groups(y_true, scores, groups)     # drop the same rows as y/s
        members = [np.where(g == u)[0] for u in np.unique(g)]
        n_g = len(members)
        if n_g < 2:
            return (float("nan"), float("nan"))

        def draw():                                   # whole clusters together
            return np.concatenate([members[j] for j in rng.integers(0, n_g, n_g)])

    vals = []
    for _ in range(n_boot):
        idx = draw()
        yi, si = y[idx], s[idx]
        if len(np.unique(yi)) < 2:
            continue
        v = stat(yi, si)
        if np.isfinite(v):
            vals.append(v)
    if not vals:
        return (float("nan"), float("nan"))
    lo = float(np.percentile(vals, 100 * alpha / 2))
    hi = float(np.percentile(vals, 100 * (1 - alpha / 2)))
    return (lo, hi)


def paired_metric_diff_ci(y_true, scores_a, scores_b, stat, groups=None,
                          n_boot: int = 500, seed: int = 42, alpha: float = 0.05):
    """CI for stat(y, a) - stat(y, b) on the SAME rows, resampled together.

    Comparing two models by their separate point estimates says nothing: the
    question "does adding these features help?" is about the DIFFERENCE, and the
    difference has its own sampling error. Because both models are scored on
    identical rows their errors are highly correlated, so the paired interval is
    much tighter than differencing two independent intervals - and it is the only
    one that answers the question. Returns (diff, lo, hi)."""
    y_all = np.asarray(y_true, dtype=float)
    a_all = np.asarray(scores_a, dtype=float)
    b_all = np.asarray(scores_b, dtype=float)
    if not (len(y_all) == len(a_all) == len(b_all)):
        raise ValueError("y_true, scores_a and scores_b must be the same length")
    mask = np.isfinite(y_all) & np.isfinite(a_all) & np.isfinite(b_all)
    y, a, b = y_all[mask], a_all[mask], b_all[mask]
    n = len(y)
    if n < 20 or len(np.unique(y)) < 2:
        return (float("nan"), float("nan"), float("nan"))
    point = float(stat(y, a) - stat(y, b))
    rng = np.random.default_rng(seed)
    if groups is None:
        def draw():
            return rng.integers(0, n, n)
    else:
        g = np.asarray(groups)
        if g.shape[0] != mask.shape[0]:
            raise ValueError("groups must carry one id per observation")
        g = g[mask]
        members = [np.where(g == u)[0] for u in np.unique(g)]
        n_g = len(members)
        if n_g < 2:
            return (point, float("nan"), float("nan"))

        def draw():
            return np.concatenate([members[j] for j in rng.integers(0, n_g, n_g)])

    vals = []
    for _ in range(n_boot):
        idx = draw()
        yi = y[idx]
        if len(np.unique(yi)) < 2:
            continue
        v = stat(yi, a[idx]) - stat(yi, b[idx])
        if np.isfinite(v):
            vals.append(v)
    if not vals:
        return (point, float("nan"), float("nan"))
    return (point,
            float(np.percentile(vals, 100 * alpha / 2)),
            float(np.percentile(vals, 100 * (1 - alpha / 2))))


def bootstrap_auc_ci(y_true, scores, n_boot: int = 500, seed: int = 42,
                     alpha: float = 0.05, groups=None):
    """Percentile bootstrap CI for AUC. Returns (lo, hi). If lo > 0.5 the ranking
    skill is statistically distinguishable from chance. Pass ``groups`` (one id
    per forecast origin) for a cluster bootstrap - see ``bootstrap_metric_ci``."""
    return bootstrap_metric_ci(y_true, scores, roc_auc, groups=groups,
                               n_boot=n_boot, seed=seed, alpha=alpha)


def _clean(y_true, scores):
    y = np.asarray(y_true, dtype=float)
    s = np.asarray(scores, dtype=float)
    mask = np.isfinite(y) & np.isfinite(s)
    return y[mask], s[mask]


def _masked_groups(y_true, scores, groups):
    """Drop the same rows from ``groups`` that ``_clean`` drops from y/scores."""
    mask = (np.isfinite(np.asarray(y_true, dtype=float))
            & np.isfinite(np.asarray(scores, dtype=float)))
    g = np.asarray(groups)
    if g.shape[0] != mask.shape[0]:
        raise ValueError(
            f"groups has length {g.shape[0]} but there are {mask.shape[0]} "
            "observations; it must carry one id per row of y_true/scores")
    return g[mask]


def log_loss(y_true, proba, eps: float = 1e-12) -> float:
    """Mean cross-entropy. Like Brier, a proper scoring rule, but it punishes
    confident wrong answers much harder (a 99% call that misses costs ~4.6)."""
    y, p = _clean(y_true, proba)
    if len(y) == 0:
        return float("nan")
    p = np.clip(p, eps, 1 - eps)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def brier_skill_score(y_true, proba, base_rate: float | None = None) -> float:
    """1 - Brier / Brier(climatology), where climatology always predicts the base
    rate. This is THE effectiveness number for probabilities: 0 = no better than
    knowing the base rate, 1 = perfect, negative = the model actively hurts.
    Pass the TRAINING base rate for a fully out-of-sample reference; default uses
    the in-sample rate (the standard, slightly stricter convention)."""
    y, p = _clean(y_true, proba)
    if len(y) == 0:
        return float("nan")
    ref = float(np.mean(y)) if base_rate is None else float(base_rate)
    brier_ref = float(np.mean((ref - y) ** 2))
    if brier_ref <= 0:
        return float("nan")  # degenerate target (all one class)
    return float(1.0 - np.mean((p - y) ** 2) / brier_ref)


def murphy_decomposition(y_true, proba, n_bins: int = 10) -> dict:
    """Murphy (1973) decomposition: Brier = reliability - resolution + uncertainty
    (up to a small within-bin variance term from binning).

        reliability - distance between stated probability and observed frequency
                      per bin (lower better; 0 = perfectly honest)
        resolution  - how far the bin outcome rates stray from the base rate
                      (higher better; 0 = the forecasts carry no information)
        uncertainty - base_rate * (1 - base_rate); a property of the TARGET, the
                      Brier score of the perfect climatology forecaster

    A model is only effective if resolution > reliability: it must earn back more
    than its dishonesty costs."""
    y, p = _clean(y_true, proba)
    n = len(y)
    if n == 0:
        return dict(reliability=float("nan"), resolution=float("nan"),
                    uncertainty=float("nan"), n_bins=n_bins)
    ybar = float(np.mean(y))
    edges = np.linspace(0, 1, n_bins + 1)
    idx = np.clip(np.digitize(p, edges) - 1, 0, n_bins - 1)
    rel = res = 0.0
    for b in range(n_bins):
        m = idx == b
        nb = float(m.sum())
        if nb == 0:
            continue
        pb, ob = float(np.mean(p[m])), float(np.mean(y[m]))
        rel += nb * (pb - ob) ** 2
        res += nb * (ob - ybar) ** 2
    return dict(reliability=rel / n, resolution=res / n,
                uncertainty=ybar * (1 - ybar), n_bins=n_bins)


def _two_sided_p(z) -> float:
    from math import erfc
    return float(erfc(abs(z) / np.sqrt(2.0)))


def spiegelhalter_z(y_true, proba, groups=None) -> tuple:
    """Spiegelhalter's z-test of calibration. Returns (z, two_sided_p).

    Under the null "the probabilities are perfectly calibrated", z ~ N(0,1); a
    p-value >= 0.05 means the stated probabilities are statistically consistent
    with the outcomes (you cannot prove them dishonest on this sample).

    Pass ``groups`` (the forecast origin) to use a CLUSTER-ROBUST variance
    sum_g (sum_{i in g} u_i)^2 instead of the textbook i.i.d. one. The closed
    form assumes independent rows; pooling 15 co-moving assets per origin
    violates that and makes the test anti-conservative, i.e. too eager to call a
    forecaster dishonest.

    KNOWN BLIND SPOT: the statistic weights each residual by (1 - 2p), so for
    forecasts centred near 0.5 the weights cancel and the test is nearly blind to
    a uniform over- or under-forecast - exactly the bias that matters for sizing.
    Always read it together with ``calibration_in_the_large``, which is built to
    catch precisely that."""
    y, p = _clean(y_true, proba)
    if len(y) == 0:
        return (float("nan"), float("nan"))
    # A forecast of exactly 0 or 1 contributes to the numerator whenever it is
    # wrong but contributes ZERO variance (p(1-p)=0), so a single confident miss
    # would send |z| to infinity. Clip, as log_loss does. Isotonic calibration
    # emits exact 0s and 1s routinely, so this is a live case, not a nicety.
    p = np.clip(p, 1e-6, 1 - 1e-6)
    u = (y - p) * (1 - 2 * p)
    num = float(np.sum(u))
    if groups is None:
        var = float(np.sum((1 - 2 * p) ** 2 * p * (1 - p)))
    else:
        g = _masked_groups(y_true, proba, groups)
        var = float(np.sum([np.sum(u[g == c]) ** 2 for c in np.unique(g)]))
    if var <= 0:
        return (float("nan"), float("nan"))
    z = num / np.sqrt(var)
    return (float(z), _two_sided_p(z))


def calibration_in_the_large(y_true, proba, groups=None) -> dict:
    """Is the forecaster biased ON AVERAGE? Returns bias, z and p.

        bias = mean(forecast) - observed frequency

    This is the complement to ``spiegelhalter_z``, which is nearly blind to a
    uniform level shift when the forecasts sit near 0.5. Adding a constant to
    every probability leaves that test happy but moves this one immediately.
    ``groups`` gives a cluster-robust standard error over forecast origins;
    without it the SE assumes independent rows and will be too small."""
    y, p = _clean(y_true, proba)
    n = len(y)
    if n == 0:
        return dict(bias=float("nan"), z=float("nan"), p_value=float("nan"))
    d = p - y                                    # per-row signed error
    bias = float(np.mean(d))
    if groups is None:
        var = float(np.var(d, ddof=1) / n) if n > 1 else 0.0
    else:
        g = _masked_groups(y_true, proba, groups)
        uniq = np.unique(g)
        means = np.array([np.mean(d[g == c]) for c in uniq])
        n_g = len(uniq)
        var = float(np.var(means, ddof=1) / n_g) if n_g > 1 else 0.0
    if var <= 0:
        return dict(bias=bias, z=float("nan"), p_value=float("nan"))
    z = bias / np.sqrt(var)
    return dict(bias=bias, z=float(z), p_value=_two_sided_p(z))


def mcc(y_true, scores, threshold: float = 0.5) -> float:
    """Matthews correlation coefficient at a threshold. Uses all four confusion
    cells, so it stays at 0 for base-rate guessing no matter how imbalanced the
    event - the single most abuse-resistant thresholded score."""
    y, s = _clean(y_true, scores)
    if len(y) == 0:
        return float("nan")
    pred = (s >= threshold).astype(float)
    tp = float(np.sum((pred == 1) & (y == 1)))
    tn = float(np.sum((pred == 0) & (y == 0)))
    fp = float(np.sum((pred == 1) & (y == 0)))
    fn = float(np.sum((pred == 0) & (y == 1)))
    denom = np.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    if denom == 0:
        return 0.0  # a constant predictor has no correlation with the outcome
    return float((tp * tn - fp * fn) / denom)


def ks_statistic(y_true, scores) -> float:
    """Kolmogorov-Smirnov distance between the score distributions of the two
    classes = max over thresholds of |TPR - FPR|. Threshold-free discrimination:
    0 = the classes' scores are indistinguishable, 1 = perfectly separable.

    TIE-AWARE, like ``roc_auc``. A threshold can only sit *between* distinct
    scores, so the ECDF gap is evaluated at the end of each tie group. Walking
    row by row instead would let the maximum land inside a block of equal scores
    and make the answer depend on row order: constant scores would score 1.0 on
    one permutation and 0.0 on another, when the truth is 0.0. That is not
    hypothetical here - isotonic calibration collapses thousands of rows onto a
    handful of distinct values."""
    y, s = _clean(y_true, scores)
    n_pos = float(np.sum(y == 1))
    n_neg = float(np.sum(y == 0))
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(-s, kind="mergesort")
    yo, so = y[order], s[order]
    tpr = np.cumsum(yo) / n_pos
    fpr = np.cumsum(1 - yo) / n_neg
    at_group_end = np.r_[np.diff(so) != 0, True]      # last row of each tie group
    return float(np.max(np.abs(tpr - fpr)[at_group_end]))


def precision_at_k(y_true, scores, frac: float = 0.10) -> float:
    """Precision inside the top-`frac` highest-scored samples - the alert-list
    view: if you only act on the model's loudest k% of days, what fraction of
    those alarms are real events?

    TIE-AWARE. When the k-th boundary falls inside a block of equal scores there
    is no principled way to pick which of them to alert on, so this returns the
    EXPECTED precision if the remaining slots were drawn uniformly from that
    block. Simply slicing the sorted order instead would break ties by array
    position - in the readout the rows are ordered (origin, asset), so the
    "winner" would be the earliest walk-forward origin and lowest-index ticker,
    a chronological artefact reported as alert-list quality. With constant scores
    this correctly returns the base rate (lift 1.0) rather than 0 or 1 depending
    on row order."""
    y, s = _clean(y_true, scores)
    n = len(y)
    if n == 0:
        return float("nan")
    k = min(max(1, int(np.ceil(frac * n))), n)
    order = np.argsort(-s, kind="mergesort")
    ys, ss = y[order], s[order]
    cut = ss[k - 1]                       # score at the boundary
    above = ss > cut                      # strictly better than the boundary
    n_above = int(above.sum())
    in_tie = ss == cut
    n_tie = int(in_tie.sum())
    tp = float(ys[above].sum())
    if n_tie:                             # share the remaining slots pro rata
        tp += float(ys[in_tie].sum()) * (k - n_above) / n_tie
    return float(tp / k)


def lift_at_k(y_true, scores, frac: float = 0.10) -> float:
    """precision_at_k divided by the base rate. 1.0 = flagging days at random;
    3.0 = an alert from this model is 3x more likely to be a real event. The
    honest usefulness multiplier for rare events, where even a skilled model's
    precision LOOKS low because the base rate is low.

    WARNING: lift has a base-rate-dependent CEILING (see ``max_lift_at_k``), so
    raw lift is NOT comparable across events. Use ``lift_efficiency`` for that."""
    y, s = _clean(y_true, scores)
    base = float(np.mean(y)) if len(y) else float("nan")
    if not np.isfinite(base) or base <= 0:
        return float("nan")
    return float(precision_at_k(y, s, frac) / base)


def max_lift_at_k(y_true, frac: float = 0.10) -> float:
    """The largest lift@k any forecaster could achieve on this target.

    A perfect ranker fills the top k slots with positives until it runs out of
    either slots or positives, so max precision = min(1, n_pos/k) and

        max lift = min(1/base_rate, 1/frac)

    For a common event this ceiling is brutally low: at an 85% base rate the best
    possible lift@10% is 1.18x. Reading a raw 1.2x as "weak" next to a rare
    event's 2.4x inverts the truth - the first is perfect, the second is not."""
    y = np.asarray(y_true, dtype=float)
    y = y[np.isfinite(y)]
    if len(y) == 0:
        return float("nan")
    base = float(np.mean(y))
    if base <= 0 or frac <= 0:
        return float("nan")
    n = len(y)
    k = max(1, int(np.ceil(frac * n)))
    return float(min(1.0, (base * n) / k) / base)


def lift_efficiency(y_true, scores, frac: float = 0.10) -> float:
    """How much of the ACHIEVABLE alert-list quality was captured, as a skill
    score anchored at both ends:

        (lift - 1) / (max_lift - 1)      0 = random flagging, 1 = the best list
                                         that could exist for this target

    Note it is NOT lift/max_lift. That form looks normalised but its floor is the
    base rate, not 0: a random ranker scores lift 1.0, which is 85% of drawdown's
    1.17x ceiling but only 10% of big_move's 10x ceiling. Dividing would report a
    coin flip as "85% of maximum" on a common event and "10%" on a rare one - the
    very cross-event incomparability the ceiling was introduced to remove.
    Subtracting the floor first is what makes the number mean the same thing on
    every event."""
    y, s = _clean(y_true, scores)
    ceil = max_lift_at_k(y, frac)
    lift = lift_at_k(y, s, frac)
    if not np.isfinite(ceil) or not np.isfinite(lift) or ceil <= 1.0:
        return float("nan")          # ceiling == 1 -> no headroom to measure
    return float((lift - 1.0) / (ceil - 1.0))


def prob_metrics(y_true, proba, threshold: float = 0.5) -> dict:
    """Full probabilistic + thresholded metric bundle for one event model."""
    # Mask first, like every other function here. Without this the confusion-matrix
    # half is computed over ALL rows while auc/ap/brier drop the non-finite ones,
    # so one bundle would describe two different samples.
    y, p = _clean(y_true, proba)
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
        log_loss=log_loss(y, p),
        bss=brier_skill_score(y, p),
        mcc=mcc(y, p, threshold),
        ks=ks_statistic(y, p),
        precision_at_10=precision_at_k(y, p, 0.10),
        lift_at_10=lift_at_k(y, p, 0.10),
    )

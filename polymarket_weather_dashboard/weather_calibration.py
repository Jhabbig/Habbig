"""Probability + sigma calibration for the weather dashboard.

Replaces the hand-tuned `lead_time_sigma_inflation` and the single-Gaussian
`compute_probability` with three things that earn their keep:

1.  Empirical residual std from `forecast_history` — a per-station sigma
    floor that catches under-dispersive ensembles (the well-known NWP
    failure mode where members agree more than reality does).
2.  Lead-time sigma curve fit to the same data (k * sqrt(days)) instead of
    the hand-tuned 0.12.
3.  Distribution-free probability path that reads directly off the empirical
    CDF of ensemble members, sidestepping the Gaussian assumption that
    misprices the tails where the dashboard is most aggressive.

All numbers come from data already in `forecast_history`. Nothing here makes
network calls.
"""

from __future__ import annotations

import math
import statistics
from typing import Optional

from scipy.stats import norm

from weather_pure import (
    c_to_f,
    empirical_cdf_above,
    empirical_cdf_below,
    empirical_cdf_between,
    safe_clamp_probability,
)


# ─── Empirical residual std (per-station / per-model sigma calibration) ──────

def fit_residual_std(rows) -> dict:
    """Compute residual std per model from forecast_history rows.

    Each input row must have ``model``, ``forecast_high``, ``observed_high``.
    Returns ``{model: {bias, residual_std, n}}`` for every model with at
    least 5 paired observations. Models with ``n < 5`` are dropped — std on
    too-few samples is noise.
    """
    by_model: dict[str, list[tuple[float, float]]] = {}
    for r in rows:
        f = r["forecast_high"] if isinstance(r, dict) else r[0]
        o = r["observed_high"] if isinstance(r, dict) else r[1]
        m = r["model"] if isinstance(r, dict) else r[2]
        if f is None or o is None or m is None:
            continue
        by_model.setdefault(m, []).append((float(f), float(o)))

    out: dict[str, dict] = {}
    for model, pairs in by_model.items():
        if len(pairs) < 5:
            continue
        residuals = [f - o for f, o in pairs]
        bias = statistics.mean(residuals)
        residual_std = statistics.stdev(residuals) if len(residuals) > 1 else None
        out[model] = {
            "bias": round(bias, 3),
            "residual_std": round(residual_std, 3) if residual_std is not None else None,
            "n": len(pairs),
        }
    return out


def consensus_sigma_floor(model_residuals: dict) -> Optional[float]:
    """Combine per-model residual stds into a single floor for the consensus.

    We take the median residual_std across available models — robust to
    outliers (one model with terrible coverage shouldn't blow up the floor).
    """
    stds = [v["residual_std"] for v in model_residuals.values()
            if v.get("residual_std") is not None]
    if not stds:
        return None
    stds_sorted = sorted(stds)
    n = len(stds_sorted)
    if n == 1:
        return stds_sorted[0]
    if n % 2 == 1:
        return stds_sorted[n // 2]
    return (stds_sorted[n // 2 - 1] + stds_sorted[n // 2]) / 2


def calibrated_sigma(ensemble_std: Optional[float],
                     empirical_floor: Optional[float],
                     lead_multiplier: float = 1.0) -> Optional[float]:
    """Return the sigma to use for probability scoring.

    `ensemble_std` is the spread inside today's NWP ensembles (often
    under-dispersive). `empirical_floor` is the median residual std across
    models from `forecast_history`. We take the larger of the two and then
    apply a lead-time inflation multiplier.

    If both inputs are missing, return None — the caller should refuse to
    fabricate a probability.
    """
    candidates = [s for s in (ensemble_std, empirical_floor)
                  if s is not None and s > 0]
    if not candidates:
        return None
    base = max(candidates)
    out = base * max(0.5, float(lead_multiplier or 1.0))
    return round(out, 3)


# ─── Lead-time sigma curve (fit, then apply) ──────────────────────────────────

def fit_leadtime_sigma_curve(rows, max_days: int = 14) -> dict:
    """Fit residual_std as a function of lead-time-in-days.

    Each row must have ``lead_days`` (integer >= 0) and a ``residual``
    (forecast - observed). Returns ``{k, intercept, by_lead, n}`` where the
    fitted form is

        sigma(d) = max(intercept, intercept + k * sqrt(d))

    with k estimated via simple least-squares between sqrt(lead) and the
    per-lead-bucket residual std. Falls back to a sensible default when
    fewer than 10 paired rows are available.
    """
    DEFAULT = {"k": 0.12, "intercept": 1.0, "by_lead": {}, "n": 0,
               "source": "default"}
    paired = []
    for r in rows:
        ld = r.get("lead_days") if isinstance(r, dict) else r[0]
        res = r.get("residual") if isinstance(r, dict) else r[1]
        if ld is None or res is None:
            continue
        ld = int(ld)
        if 0 <= ld <= max_days:
            paired.append((ld, float(res)))
    if len(paired) < 10:
        return DEFAULT

    by_lead: dict[int, list[float]] = {}
    for d, res in paired:
        by_lead.setdefault(d, []).append(res)

    bucket_stats = {}
    for d, residuals in by_lead.items():
        if len(residuals) < 3:
            continue
        bucket_stats[d] = {
            "n": len(residuals),
            "std": statistics.stdev(residuals) if len(residuals) > 1 else None,
        }
    if len(bucket_stats) < 3:
        return {**DEFAULT, "by_lead": bucket_stats, "n": len(paired)}

    # Fit sigma(d) = a + k*sqrt(d) by least squares
    xs = [math.sqrt(d) for d in sorted(bucket_stats.keys())]
    ys = [bucket_stats[d]["std"] for d in sorted(bucket_stats.keys())
          if bucket_stats[d]["std"] is not None]
    xs = xs[:len(ys)]
    if len(xs) < 3:
        return {**DEFAULT, "by_lead": bucket_stats, "n": len(paired)}

    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    k = num / den if den > 0 else 0.0
    intercept = my - k * mx

    return {
        "k": round(max(0.0, k), 3),
        "intercept": round(max(0.5, intercept), 3),
        "by_lead": bucket_stats,
        "n": len(paired),
        "source": "fitted",
    }


def leadtime_multiplier(curve: dict, lead_days: int, base_std: float = 3.0,
                        cap: float = 3.0) -> float:
    """Translate a fitted curve into a multiplier on the day-zero std.

    The multiplier is `sigma(d) / sigma(0)`, capped at `cap` so a single
    bad day at long lead doesn't produce a 5x blowup.
    """
    if lead_days is None or lead_days < 0:
        return 1.0
    k = float(curve.get("k", 0.12))
    intercept = float(curve.get("intercept", 1.0))
    sigma_0 = max(0.5, intercept)
    sigma_d = max(sigma_0, intercept + k * math.sqrt(lead_days))
    # Express as a multiplier on the *ensemble* std at day 0.
    # base_std lets the caller scale relative to a reasonable day-zero sigma.
    mult = sigma_d / max(0.5, sigma_0)
    return round(min(cap, mult), 3)


# ─── Probability scoring ──────────────────────────────────────────────────────

def gaussian_probability(threshold_info: dict, mean: float, std: float) -> Optional[float]:
    """Single-Gaussian probability, used as a fallback when we don't have
    raw ensemble members. `threshold_info` matches `parse_temperature`'s
    output shape."""
    if mean is None or std is None or std <= 0:
        return None
    is_celsius = (threshold_info.get("unit", "F") or "F").upper().startswith("C")
    threshold = threshold_info.get("threshold")
    is_over = threshold_info.get("is_over")
    lower = threshold_info.get("temp_lower")
    upper = threshold_info.get("temp_upper")
    if is_celsius:
        if threshold is not None:
            threshold = c_to_f(threshold)
        if lower is not None:
            lower = c_to_f(lower)
        if upper is not None:
            upper = c_to_f(upper)

    if threshold is not None and is_over is not None:
        if is_over:
            p = 1.0 - norm.cdf(threshold, loc=mean, scale=std)
        else:
            p = norm.cdf(threshold, loc=mean, scale=std)
        return safe_clamp_probability(p)
    if lower is not None and upper is not None:
        p = norm.cdf(upper + 0.5, loc=mean, scale=std) - norm.cdf(lower - 0.5, loc=mean, scale=std)
        return safe_clamp_probability(p)
    return None


def empirical_probability(threshold_info: dict, members) -> Optional[float]:
    """Distribution-free probability from raw ensemble members.

    Computes P(threshold satisfied) directly from the empirical CDF of the
    member temperatures. This captures fat tails the Gaussian fit misses,
    which is exactly where the dashboard generates aggressive signals.
    Needs at least 10 members to return a number.
    """
    if not members or len(members) < 10:
        return None
    is_celsius = (threshold_info.get("unit", "F") or "F").upper().startswith("C")
    threshold = threshold_info.get("threshold")
    is_over = threshold_info.get("is_over")
    lower = threshold_info.get("temp_lower")
    upper = threshold_info.get("temp_upper")
    if is_celsius:
        if threshold is not None:
            threshold = c_to_f(threshold)
        if lower is not None:
            lower = c_to_f(lower)
        if upper is not None:
            upper = c_to_f(upper)

    if threshold is not None and is_over is not None:
        if is_over:
            p = empirical_cdf_above(members, threshold)
        else:
            p = empirical_cdf_below(members, threshold)
        return safe_clamp_probability(p)
    if lower is not None and upper is not None:
        p = empirical_cdf_between(members, lower - 0.5, upper + 0.5)
        return safe_clamp_probability(p)
    return None


def blended_probability(threshold_info: dict, mean: float, std: float,
                        members=None) -> dict:
    """Score a market with both the Gaussian and empirical paths and return
    both — the dashboard exposes both so users can see whether they agree.

    The 'consensus' is the empirical reading when available, else Gaussian.
    Diff > 5pp between the two is a calibration-warning flag.
    """
    g = gaussian_probability(threshold_info, mean, std)
    e = empirical_probability(threshold_info, members) if members else None
    consensus = e if e is not None else g
    disagree = (g is not None and e is not None and abs(g - e) > 0.05)
    return {
        "probability": consensus,
        "gaussian": g,
        "empirical": e,
        "method": "empirical" if e is not None else ("gaussian" if g is not None else None),
        "tail_warning": disagree,
        "n_members": len(members) if members else 0,
    }


# ─── Forecast blending (persistence/analog at short lead) ────────────────────

def inverse_variance_blend(estimates) -> Optional[dict]:
    """Combine multiple (mean, std, weight_hint) estimates by inverse variance.

    Each input is a dict with at minimum ``mean`` and ``std``. Optional
    ``weight_hint`` lets the caller down-weight low-quality sources (e.g.
    persistence at long lead). Returns a single (mean, std) dict, or None
    if no valid estimates.

    Math: with independent Gaussian estimates, the MLE has
        precision = sum(1/sigma_i^2)
        mean      = sum(mu_i / sigma_i^2) / precision
        sigma     = 1 / sqrt(precision)
    """
    valid = []
    for e in estimates:
        if not e:
            continue
        m = e.get("mean")
        s = e.get("std")
        if m is None or s is None or s <= 0:
            continue
        w_hint = float(e.get("weight_hint", 1.0))
        if w_hint <= 0:
            continue
        valid.append((float(m), float(s), w_hint))
    if not valid:
        return None
    precision = sum(w / (s * s) for _m, s, w in valid)
    if precision <= 0:
        return None
    blended_mean = sum(m * w / (s * s) for m, s, w in valid) / precision
    blended_std = math.sqrt(1.0 / precision)
    return {
        "mean": round(blended_mean, 2),
        "std": round(blended_std, 2),
        "n_sources": len(valid),
    }


def persistence_weight_for_lead(lead_days: int) -> float:
    """How much weight to give a persistence forecast at a given lead.

    Persistence (yesterday's high) is competitive at lead 1 in stable
    regimes and useless past day 3. Linear ramp from 1.0 at lead 1 to 0.0
    at lead 4."""
    if lead_days is None or lead_days < 1:
        return 0.0
    if lead_days >= 4:
        return 0.0
    return max(0.0, 1.0 - (lead_days - 1) / 3.0)


def analog_weight_for_lead(lead_days: int) -> float:
    """Analog (same-day past 3y) weight. Stays useful across longer leads
    because it's regime-conditioning, not autocorrelation."""
    if lead_days is None or lead_days < 0:
        return 0.0
    if lead_days <= 7:
        return 0.3
    return 0.5  # at long lead, analog beats raw NWP


# ─── Calibration metrics ──────────────────────────────────────────────────────

def brier_score(predictions, outcomes) -> Optional[float]:
    """Mean squared error of probabilistic predictions. Lower is better.
    Constant 0.5 → 0.25; perfect → 0.0; always-wrong → 1.0."""
    pairs = [(p, o) for p, o in zip(predictions, outcomes)
             if p is not None and o is not None]
    if not pairs:
        return None
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def log_loss(predictions, outcomes, eps: float = 1e-9) -> Optional[float]:
    """Binary log loss. Predictions clipped to [eps, 1-eps] to avoid -inf
    on overconfident wrong calls."""
    pairs = [(max(eps, min(1 - eps, p)), o) for p, o in zip(predictions, outcomes)
             if p is not None and o is not None]
    if not pairs:
        return None
    total = 0.0
    for p, o in pairs:
        total += -(o * math.log(p) + (1 - o) * math.log(1 - p))
    return total / len(pairs)


def reliability_diagram(predictions, outcomes, n_bins: int = 10) -> list:
    """Empirical calibration: per probability bucket, the avg predicted prob
    vs the actual hit rate. Returns a list of dicts the frontend can plot."""
    buckets = [[] for _ in range(n_bins)]
    for p, o in zip(predictions, outcomes):
        if p is None or o is None:
            continue
        idx = min(int(p * n_bins), n_bins - 1)
        buckets[idx].append((p, int(bool(o))))
    out = []
    for i, bucket in enumerate(buckets):
        if not bucket:
            continue
        avg_p = sum(p for p, _ in bucket) / len(bucket)
        hit = sum(o for _, o in bucket) / len(bucket)
        out.append({
            "bin_lo": round(i / n_bins, 2),
            "bin_hi": round((i + 1) / n_bins, 2),
            "n": len(bucket),
            "avg_predicted": round(avg_p, 4),
            "actual_rate": round(hit, 4),
            "miscalibration": round(avg_p - hit, 4),
        })
    return out


def bootstrap_sharpe(pnls, n_resamples: int = 1000, seed: int = 42) -> dict:
    """Bootstrap a Sharpe CI on a list of trade PnLs.

    Per-trade Sharpe (no annualization) — useful as an apples-to-apples
    figure. The point estimate alone hides the tiny-sample variance that
    makes 90-trade Sharpe figures unreliable.
    """
    import random
    pnls = [float(p) for p in pnls if p is not None]
    n = len(pnls)
    if n < 5:
        return {"point": None, "lo": None, "hi": None, "n": n}

    def _sharpe(arr):
        if len(arr) < 2:
            return 0.0
        m = statistics.mean(arr)
        s = statistics.stdev(arr)
        return m / s if s > 0 else 0.0

    rng = random.Random(seed)
    samples = []
    for _ in range(n_resamples):
        resampled = [pnls[rng.randrange(n)] for _ in range(n)]
        samples.append(_sharpe(resampled))
    samples.sort()
    return {
        "point": round(_sharpe(pnls), 3),
        "lo": round(samples[int(n_resamples * 0.05)], 3),
        "hi": round(samples[int(n_resamples * 0.95)], 3),
        "n": n,
        "n_resamples": n_resamples,
    }

"""harness.py - Out-of-sample evaluation, NULL comparison and reporting.

This is the most important component of the toolkit. No model is trusted on its
own word: every forecasting model is run through a ROLLING walk-forward (never a
single 80/20 split) and compared against a NULL benchmark. We report:

    * model error
    * null  error
    * SKILL = % improvement over the null   (flagged red if <= 0)
    * for direction/return models: performance AFTER transaction costs.

An edge smaller than transaction costs is not an edge - and the harness says so.
"""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass, field
from typing import Callable

import numpy as np

from core import (
    TARGET_DIRECTION,
    TARGET_PRICE,
    TARGET_RETURNS,
    TARGET_VARIANCE,
    directional_accuracy,
    logger,
    null_base_rate_direction,
    null_random_walk_price,
    null_rolling_mean_variance,
    null_zero_return,
    qlike,
    rmse,
    skill,
)
from data import MarketData

_HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(_HERE, "results")

# ANSI colours for the terminal report.
_RED = "\033[91m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _flag(s: float) -> str:
    if s is None or not np.isfinite(s):
        return f"{_YELLOW}  n/a {_RESET}"
    if s <= 0:
        return f"{_RED}{s:+6.1%}{_RESET}"
    return f"{_GREEN}{s:+6.1%}{_RESET}"


@dataclass
class WFResult:
    """Result of a walk-forward evaluation for one model."""

    name: str
    tier: str
    target: str
    n_windows: int
    error: float
    null_error: float
    skill: float
    metric: str
    extras: dict = field(default_factory=dict)
    # pooled series for plotting (mean across assets over time)
    forecast_series: np.ndarray | None = None
    actual_series: np.ndarray | None = None
    null_series: np.ndarray | None = None

    def to_row(self) -> dict:
        d = asdict(self)
        d.pop("forecast_series")
        d.pop("actual_series")
        d.pop("null_series")
        return d


def slice_market_data(md: MarketData, a: int, b: int) -> MarketData:
    """Return a MarketData covering return-rows ``R[a:b]`` with consistent prices.

    R row t is the move from P[t] to P[t+1], so the matching price window is
    ``P[a : b+1]``.
    """
    return MarketData(
        P=md.P[a : b + 1],
        R=md.R[a:b],
        tickers=md.tickers,
        dates=md.dates[a : b + 1],
        source=md.source,
        meta=md.meta,
    )


def _newey_west_tstat(d: np.ndarray, lag: int | None = None) -> tuple[float, float]:
    """Paired t-statistic and two-sided p-value for mean(d) != 0, with a
    Newey-West (Bartlett) HAC variance that corrects for autocorrelation.

    Used to test whether a strategy's per-period return EDGE over the null is
    distinguishable from zero. Returns (t_stat, p_value).
    """
    d = np.asarray(d, dtype=float)
    d = d[np.isfinite(d)]
    n = d.size
    if n < 10:
        return 0.0, 1.0
    mean = d.mean()
    dd = d - mean
    if lag is None:
        lag = max(1, int(4 * (n / 100.0) ** (2.0 / 9.0)))  # Newey-West rule of thumb
    s = float(np.mean(dd * dd))  # gamma_0
    for ell in range(1, lag + 1):
        w = 1.0 - ell / (lag + 1.0)
        s += 2.0 * w * float(np.mean(dd[ell:] * dd[:-ell]))
    se = math.sqrt(max(s, 0.0) / n)
    if se <= 0:
        return 0.0, 1.0
    t = mean / se
    try:
        from scipy import stats
        p = float(2.0 * (1.0 - stats.t.cdf(abs(t), df=n - 1)))
    except Exception:  # normal approximation if scipy unavailable
        p = float(2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0)))))
    return float(t), p


def _transaction_cost_report(
    pred_sign: np.ndarray,
    actual_ret: np.ndarray,
    null_sign: np.ndarray,
    cost_bps: float,
    alpha: float = 0.05,
) -> dict:
    """P&L of a daily long/short strategy before and after costs, WITH a
    significance test on the edge over the null.

    Position each day = sign(prediction) per asset, equal weight. A trade (and
    hence a cost) is incurred whenever a position flips. The same accounting is
    applied to the null strategy. A point-estimate "net > null_net" is NOT enough
    to claim an edge: we run a paired Newey-West t-test on the per-period
    (model - null) net-return series and require statistical significance too.
    """
    pred_sign = np.sign(pred_sign)
    null_sign = np.sign(null_sign)
    cost = cost_bps / 1e4

    def pnl(pos: np.ndarray):
        # pos, actual_ret: shape (W, n). Order preserved = time order.
        gross = pos * actual_ret
        turnover = np.zeros_like(pos)
        turnover[0] = np.abs(pos[0])  # entering the first position
        turnover[1:] = np.abs(pos[1:] - pos[:-1])
        net = gross - turnover * cost
        net_daily = np.nanmean(net, axis=1)  # per-period equal-weight portfolio return
        gross_ann = float(np.nanmean(gross) * 252)
        net_ann = float(np.nanmean(net) * 252)
        avg_trades = float(np.nanmean(turnover) / 2)  # fraction of names trading/day
        return gross_ann, net_ann, avg_trades, net_daily

    g_gross, g_net, g_to, g_daily = pnl(pred_sign)
    n_gross, n_net, _, n_daily = pnl(null_sign)

    # Significance of the after-cost edge over the null (paired, HAC-robust).
    t_stat, p_value = _newey_west_tstat(g_daily - n_daily)
    beats_null_pointest = (g_net > 0) and (g_net > n_net)
    significant = beats_null_pointest and (p_value < alpha)
    # A genuine edge requires positive net, beating the null, AND significance.
    has_edge = significant
    return {
        "gross_ann_return": g_gross,
        "net_ann_return": g_net,
        "null_net_ann_return": n_net,
        "daily_turnover_frac": g_to,
        "beats_null_pointest": bool(beats_null_pointest),
        "edge_tstat": t_stat,
        "edge_pvalue": p_value,
        "edge_significant": bool(significant),
        "edge_below_cost": bool(not has_edge),
        "cost_bps": cost_bps,
    }


def walk_forward(
    model_factory: Callable[[], object],
    data: MarketData,
    train_window: int = 252,
    test_window: int = 1,
    step: int = 1,
    cost_bps: float = 10.0,
    max_windows: int | None = None,
) -> WFResult:
    """Rolling out-of-sample evaluation of a forecasting model.

    Parameters
    ----------
    model_factory : zero-arg callable returning a FRESH, unfitted model. A new
        instance is fit on each training window (no leakage across windows).
    train_window  : number of return-rows in each training window.
    test_window   : forecast horizon per window (rows).
    step          : how far the window advances each iteration.
    cost_bps      : transaction cost per trade in basis points (direction models).
    max_windows   : optional cap on number of windows (speed).

    Returns a :class:`WFResult`. Raises if the model is a decomposition model
    (target is None) - those expose transform()/reconstruct(), not forecasts.
    """
    probe = model_factory()
    target = probe.target
    name = getattr(probe, "name", probe.__class__.__name__)
    tier = getattr(probe, "tier", "?")
    null_window = getattr(probe, "null_window", 63)
    if target is None:
        raise ValueError(f"{name} is a decomposition model; use transform()/reconstruct().")

    R = data.R
    P = data.P
    N = R.shape[0]
    n = R.shape[1]

    preds, actuals, nulls = [], [], []
    j = train_window
    count = 0
    skipped = 0
    while j + test_window <= N:
        if max_windows is not None and count >= max_windows:
            break
        train_md = slice_market_data(data, j - train_window, j)
        try:
            model = model_factory()
            model.fit(train_md)
            pred = np.atleast_2d(np.asarray(model.predict(test_window), dtype=float))
            if pred.shape[0] != test_window:
                # some models return (n,) for 1-step; reshape
                pred = pred.reshape(test_window, -1)
        except Exception as exc:  # never crash the whole run on one window
            skipped += 1
            if skipped <= 3:
                logger.warning("%s: window at j=%d skipped (%s).", name, j, exc)
            j += step
            continue

        train_R = R[j - train_window : j]
        if target == TARGET_PRICE:
            actual = P[j + 1 : j + 1 + test_window]
            null = null_random_walk_price(train_md.P, test_window)
        elif target == TARGET_VARIANCE:
            actual = R[j : j + test_window] ** 2
            null = null_rolling_mean_variance(train_R, null_window, test_window)
        elif target == TARGET_DIRECTION:
            actual = R[j : j + test_window]
            null = null_base_rate_direction(train_R, test_window)
        elif target == TARGET_RETURNS:
            actual = R[j : j + test_window]
            null = null_zero_return(n, test_window)
        else:
            raise ValueError(f"Unknown target {target!r}")

        preds.append(pred)
        actuals.append(actual)
        nulls.append(null)
        count += 1
        j += step

    if count == 0:
        raise RuntimeError(f"{name}: no usable walk-forward windows (data too short?).")
    if skipped:
        logger.warning("%s: %d/%d windows skipped.", name, skipped, count + skipped)

    preds = np.vstack(preds)
    actuals = np.vstack(actuals)
    nulls = np.vstack(nulls)

    extras: dict = {"skipped_windows": skipped}

    if target == TARGET_VARIANCE:
        # QLIKE is the primary loss for variance forecasts: it is far less noisy
        # than RMSE on the r^2 proxy and is minimised by the true variance.
        model_err = qlike(preds, actuals)
        null_err = qlike(nulls, actuals)
        extras["rmse_model"] = rmse(preds, actuals)
        extras["rmse_null"] = rmse(nulls, actuals)
        metric = "QLIKE(var)"
        fseries = np.nanmean(preds, axis=1)
        aseries = np.nanmean(actuals, axis=1)
        nseries = np.nanmean(nulls, axis=1)

    elif target == TARGET_PRICE:
        model_err = rmse(preds, actuals)
        null_err = rmse(nulls, actuals)
        metric = "RMSE(price)"
        fseries = np.nanmean(preds, axis=1)
        aseries = np.nanmean(actuals, axis=1)
        nseries = np.nanmean(nulls, axis=1)

    elif target == TARGET_RETURNS:
        model_err = rmse(preds, actuals)
        null_err = rmse(nulls, actuals)
        metric = "RMSE(ret)"
        # sign hit-rate of the return forecaster (useful for the direction story)
        extras["accuracy"] = directional_accuracy(preds, actuals)
        extras.update(_transaction_cost_report(np.sign(preds), actuals, nulls + 1e-9, cost_bps))
        fseries = np.nanmean(preds, axis=1)
        aseries = np.nanmean(actuals, axis=1)
        nseries = np.nanmean(nulls, axis=1)

    elif target == TARGET_DIRECTION:
        acc = directional_accuracy(preds, actuals)
        null_acc = directional_accuracy(nulls, actuals)
        model_err = 1.0 - acc
        null_err = 1.0 - null_acc
        extras["accuracy"] = acc
        extras["null_accuracy"] = null_acc
        extras.update(_transaction_cost_report(preds, actuals, nulls, cost_bps))
        metric = "1-accuracy"
        fseries = None
        aseries = None
        nseries = None
    else:
        raise ValueError(f"Unknown target {target!r}")

    return WFResult(
        name=name,
        tier=tier,
        target=target,
        n_windows=count,
        error=model_err,
        null_error=null_err,
        skill=skill(model_err, null_err),
        metric=metric,
        extras=extras,
        forecast_series=fseries,
        actual_series=aseries,
        null_series=nseries,
    )


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def print_result(res: WFResult) -> None:
    line = (
        f"  [{res.tier}] {res.name:<34s} {res.metric:<12s} "
        f"model={res.error:.4g}  null={res.null_error:.4g}  skill={_flag(res.skill)} "
        f"(n={res.n_windows})"
    )
    print(line)
    if res.target in (TARGET_DIRECTION, TARGET_RETURNS) and "net_ann_return" in res.extras:
        e = res.extras
        col = _RED if e["edge_below_cost"] else _GREEN
        # Three honest states: loses to null / beats null but within noise / real edge.
        if not e.get("beats_null_pointest", False):
            verdict = f"{_RED}EDGE < COST{_RESET}"
        elif not e.get("edge_significant", False):
            verdict = (f"{_YELLOW}beats null but WITHIN NOISE "
                       f"(t={e['edge_tstat']:+.2f}, p={e['edge_pvalue']:.2f}){_RESET}")
        else:
            verdict = (f"{_GREEN}edge > cost & SIGNIFICANT "
                       f"(t={e['edge_tstat']:+.2f}, p={e['edge_pvalue']:.3f}){_RESET}")
        print(
            f"        cost@{e['cost_bps']:.0f}bps -> gross={e['gross_ann_return']:+.2%}/yr "
            f"net={col}{e['net_ann_return']:+.2%}{_RESET}/yr  "
            f"null_net={e['null_net_ann_return']:+.2%}/yr  -> " + verdict
        )


def compare_all(results: list[WFResult], outdir: str = RESULTS_DIR, make_plots: bool = True) -> list[WFResult]:
    """Rank forecasting models by skill, print a table, and write plots."""
    ranked = sorted(results, key=lambda r: (-(r.skill if np.isfinite(r.skill) else -9), r.name))
    print()
    print(f"{_BOLD}================ RANKED BY SKILL (out-of-sample, vs NULL) ================{_RESET}")
    print(f"  {'rank':<5}{'tier':<5}{'model':<34s}{'metric':<12s}{'skill':>9s}")
    for i, r in enumerate(ranked, 1):
        print(f"  {i:<5}{r.tier:<5}{r.name:<34s}{r.metric:<12s}{_flag(r.skill):>9s}")
    print(f"{_BOLD}========================================================================={_RESET}")
    print()
    for r in ranked:
        print_result(r)

    if make_plots:
        try:
            _plot_skill_bars(ranked, outdir)
            _plot_forecast_panels(ranked, outdir)
        except Exception as exc:  # plotting must never break the run
            logger.warning("Plotting failed: %s", exc)
    return ranked


def _plot_skill_bars(ranked: list[WFResult], outdir: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(outdir, exist_ok=True)
    names = [r.name for r in ranked]
    skills = [r.skill if np.isfinite(r.skill) else 0.0 for r in ranked]
    colors = ["#2ca02c" if s > 0 else "#d62728" for s in skills]
    fig, ax = plt.subplots(figsize=(10, max(4, 0.4 * len(names))))
    ax.barh(names[::-1], [s * 100 for s in skills[::-1]], color=colors[::-1])
    ax.axvline(0, color="k", lw=0.8)
    ax.set_xlabel("Skill = % improvement over NULL (negative = worse than null)")
    ax.set_title("Out-of-sample skill by model (red = no edge)")
    fig.tight_layout()
    path = os.path.join(outdir, "skill_ranking.png")
    fig.savefig(path, dpi=120)
    plt.close(fig)
    logger.info("Wrote %s", path)


def _plot_forecast_panels(ranked: list[WFResult], outdir: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(outdir, exist_ok=True)
    panels = [r for r in ranked if r.forecast_series is not None][:6]
    if not panels:
        return
    rows = len(panels)
    fig, axes = plt.subplots(rows, 1, figsize=(11, 2.2 * rows), squeeze=False)
    for ax, r in zip(axes[:, 0], panels):
        x = np.arange(len(r.actual_series))
        ax.plot(x, r.actual_series, color="0.5", lw=0.8, label="actual")
        ax.plot(x, r.forecast_series, color="#1f77b4", lw=1.0, label="model")
        if r.null_series is not None:
            ax.plot(x, r.null_series, color="#d62728", lw=0.8, ls="--", label="null")
        ax.set_title(f"[{r.tier}] {r.name}  (skill {r.skill:+.1%})", fontsize=9)
        ax.legend(fontsize=7, loc="upper right")
    fig.suptitle("Walk-forward: model vs null vs actual (mean across assets)", y=1.0)
    fig.tight_layout()
    path = os.path.join(outdir, "forecast_panels.png")
    fig.savefig(path, dpi=120)
    plt.close(fig)
    logger.info("Wrote %s", path)

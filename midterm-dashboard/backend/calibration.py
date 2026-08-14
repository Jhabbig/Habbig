"""Per-confidence-bucket calibration tracking.

Two questions this module answers:

  1. **Reliability** — "Of races we called at 80-90% confidence, what
     fraction actually resolved that way?" A perfectly-calibrated forecast
     has reliability equal to confidence in every bucket.
  2. **Resolution** — how much sharper is our forecast than the climatology
     baseline (50-50)?

We bucket by ensemble confidence × P(D), not just by confidence: a 90%
confident D call and a 90% confident R call are both "high-confidence",
but they have different probabilities. The buckets are over the *forecast
probability* (5 buckets, evenly spaced), and within each bucket we report
the expected vs realized D-win rate.

Inputs are forecast snapshots joined to the curated historical-results
dataset. Snapshots are forward-looking when sourced from
``midterm_forecast_snapshots``; they're in-sample if derived on-the-fly
from divergence rows. The frontend exposes which mode is in use.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# 5 evenly-spaced probability buckets covering [0, 1].
BUCKET_EDGES = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
BUCKET_LABELS = [
    "0-20% D",
    "20-40% D",
    "40-60% D",
    "60-80% D",
    "80-100% D",
]


def _bucket_for(p: float) -> Optional[int]:
    """Return the index of the bucket containing ``p``, or None if invalid."""
    if p is None:
        return None
    try:
        p = float(p)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= p <= 1.0):
        return None
    # Find the bucket where edges[i] <= p < edges[i+1]; place exact 1.0 in last
    for i in range(len(BUCKET_EDGES) - 1):
        lo = BUCKET_EDGES[i]
        hi = BUCKET_EDGES[i + 1]
        if lo <= p < hi:
            return i
    return len(BUCKET_EDGES) - 2  # p == 1.0 falls into the top bucket


def calibration_table(samples: Iterable[dict]) -> dict:
    """Aggregate ``samples`` into a per-bucket calibration table.

    Each sample is ``{"forecast_d": float, "outcome_d": 0|1, ...}``.
    Returns::

        {
          "buckets": [
            {"label": "...", "low": 0.0, "high": 0.2, "n": int,
             "mean_forecast": float, "realized_d_rate": float,
             "diff_pp": float},
            ...
          ],
          "n_total": int,
          "brier_score": float,            # mean (forecast - outcome)^2
          "log_loss": float,               # mean -log p(observed)
        }
    """
    bucket_state = [
        {"n": 0, "fcast_sum": 0.0, "d_wins": 0}
        for _ in range(len(BUCKET_LABELS))
    ]
    n_total = 0
    brier_sum = 0.0
    log_loss_sum = 0.0
    log_loss_n = 0

    import math
    for s in samples:
        p = s.get("forecast_d")
        o = s.get("outcome_d")
        if p is None or o is None:
            continue
        try:
            p = float(p)
            o = int(o)
        except (TypeError, ValueError):
            continue
        if o not in (0, 1):
            continue

        b = _bucket_for(p)
        if b is None:
            continue
        bucket_state[b]["n"] += 1
        bucket_state[b]["fcast_sum"] += p
        bucket_state[b]["d_wins"] += o

        n_total += 1
        brier_sum += (p - o) ** 2

        # Log loss — clamp to avoid log(0).
        p_clamped = max(1e-6, min(1 - 1e-6, p))
        log_loss_sum += -(o * math.log(p_clamped) + (1 - o) * math.log(1 - p_clamped))
        log_loss_n += 1

    buckets = []
    for i, st in enumerate(bucket_state):
        n = st["n"]
        if n == 0:
            buckets.append({
                "label": BUCKET_LABELS[i],
                "low": BUCKET_EDGES[i],
                "high": BUCKET_EDGES[i + 1],
                "n": 0,
                "mean_forecast": None,
                "realized_d_rate": None,
                "diff_pp": None,
            })
            continue
        mean_fcast = st["fcast_sum"] / n
        realized = st["d_wins"] / n
        buckets.append({
            "label": BUCKET_LABELS[i],
            "low": BUCKET_EDGES[i],
            "high": BUCKET_EDGES[i + 1],
            "n": n,
            "mean_forecast": round(mean_fcast, 4),
            "realized_d_rate": round(realized, 4),
            "diff_pp": round((realized - mean_fcast) * 100, 2),
        })

    return {
        "buckets": buckets,
        "n_total": n_total,
        "brier_score": round(brier_sum / n_total, 4) if n_total else None,
        "log_loss": round(log_loss_sum / log_loss_n, 4) if log_loss_n else None,
    }


def calibration_over_time(samples: Iterable[dict], *, n_windows: int = 6) -> dict:
    """Group ``samples`` by timestamp into ``n_windows`` chronological buckets
    and report each window's Brier score + sample count.

    Useful for seeing whether the forecast is *getting* more or less
    calibrated as time progresses.
    """
    rows = sorted(
        (s for s in samples if s.get("snapshot_time")),
        key=lambda s: s["snapshot_time"],
    )
    if not rows:
        return {"windows": [], "n_total": 0}

    n = len(rows)
    chunk = max(1, n // n_windows)
    windows: list[dict] = []
    for i in range(0, n, chunk):
        slice_ = rows[i:i + chunk]
        if not slice_:
            continue
        table = calibration_table(slice_)
        windows.append({
            "start": slice_[0]["snapshot_time"],
            "end": slice_[-1]["snapshot_time"],
            "n": len(slice_),
            "brier_score": table["brier_score"],
            "log_loss": table["log_loss"],
        })
    return {"windows": windows, "n_total": n}

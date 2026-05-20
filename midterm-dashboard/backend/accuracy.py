from __future__ import annotations
"""Accuracy backtest: turn the historical predictions table into stats.

Three metrics, each pulling on a different intuition:

1. **Hit rate** — fraction of races where the source assigned ≥ 50% to the
   eventual winner. The simplest sanity check. "Did the market pick right?"

2. **Brier score** — mean squared error of the assigned probability for the
   winning outcome. 0 is perfect, 0.25 is a coinflip, 1.0 is maximally
   wrong. Penalises confident-wrong predictions hardest. The standard
   forecast scoring metric.

3. **Calibration buckets** — for predictions in the 40-60% range, the
   "toss-up" bucket, what fraction actually won? A well-calibrated source
   that says 50% should be right ~50% of the time on those races.

All metrics are computed from a single SQL join — there's no cached
intermediate state. Calls are cheap because the dataset is tiny (~50 rows).
"""

from typing import Optional


def _brier(predicted_prob_of_winner: float) -> float:
    """Brier score for a prediction whose outcome was 1 (winner won).

    Brier = (predicted - actual)^2. Since we always store the prob assigned
    to the *winning* outcome, ``actual`` is always 1, so this simplifies to
    (1 - prob)^2. Lower is better.
    """
    p = max(0.0, min(1.0, predicted_prob_of_winner))
    return (1.0 - p) ** 2


def _is_hit(prob_of_winner: float, threshold: float = 0.5) -> bool:
    """True if the source assigned more than 50% to the eventual winner —
    i.e. it picked the right side."""
    return prob_of_winner >= threshold


def compute_source_stats(
    predictions: list[dict],
    *,
    race_type: Optional[str] = None,
    min_year: Optional[int] = None,
) -> dict:
    """Compute per-source accuracy stats over the joined predictions table.

    Parameters
    ----------
    predictions: rows from ``Database.get_historical_predictions()`` — each
        row has at minimum ``source``, ``closing_prob``, ``race_type``,
        ``race_key`` (which encodes the year as the last underscore segment
        when present).
    race_type: optional filter (e.g. "senate" to compute Senate-only stats).
    min_year: optional minimum election year filter.

    Returns
    -------
    Dict mapping source name → stat dict with keys ``n``, ``hit_rate``,
    ``brier``, ``calibration_50``, ``best_call``, ``worst_call``.
    """
    if race_type:
        predictions = [p for p in predictions if p.get("race_type") == race_type]
    if min_year:
        predictions = [
            p for p in predictions
            if _year_from_race_key(p.get("race_key", "")) >= min_year
        ]

    by_source: dict[str, list[dict]] = {}
    for p in predictions:
        by_source.setdefault(p["source"], []).append(p)

    out = {}
    for source, rows in by_source.items():
        if not rows:
            continue
        n = len(rows)
        hits = sum(1 for r in rows if _is_hit(r["closing_prob"]))
        briers = [_brier(r["closing_prob"]) for r in rows]
        toss_ups = [r for r in rows if 0.40 <= r["closing_prob"] <= 0.60]
        toss_up_hits = sum(1 for r in toss_ups if _is_hit(r["closing_prob"]))
        best = max(rows, key=lambda r: r["closing_prob"])
        worst = min(rows, key=lambda r: r["closing_prob"])
        out[source] = {
            "n": n,
            "hit_rate": round(hits / n, 4),
            "brier": round(sum(briers) / n, 4),
            "calibration_50": (
                round(toss_up_hits / len(toss_ups), 4) if toss_ups else None
            ),
            "n_toss_ups": len(toss_ups),
            "best_call": {
                "race_key": best["race_key"],
                "winner": best.get("winner"),
                "closing_prob": round(best["closing_prob"], 4),
            },
            "worst_call": {
                "race_key": worst["race_key"],
                "winner": worst.get("winner"),
                "closing_prob": round(worst["closing_prob"], 4),
            },
        }
    return out


def _year_from_race_key(race_key: str) -> int:
    """Extract the year from a race_key like 'senate_GA_2020'. Returns 0 if
    the key doesn't end in a 4-digit year."""
    parts = race_key.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 4:
        return int(parts[1])
    return 0


def compute_summary(predictions: list[dict]) -> dict:
    """Full summary: overall + per-race-type breakdowns + recent-cycle view.

    This is what the ``/data/accuracy`` endpoint returns. The frontend
    chooses which slice to render in each badge.
    """
    race_types = sorted({p.get("race_type") for p in predictions if p.get("race_type")})
    summary = {
        "overall": compute_source_stats(predictions),
        "by_race_type": {
            rt: compute_source_stats(predictions, race_type=rt) for rt in race_types
        },
        "since_2022": compute_source_stats(predictions, min_year=2022),
        "since_2024": compute_source_stats(predictions, min_year=2024),
        "race_count": len({p["race_key"] for p in predictions}),
        "prediction_count": len(predictions),
        "sources_tracked": sorted({p["source"] for p in predictions}),
    }
    return summary


def seed_from_curated_dataset(db) -> tuple[int, int]:
    """Idempotent: load every row from accuracy_backfill.py into the DB.

    Returns (resolutions_upserted, predictions_upserted). Safe to call on
    every startup — uses ON CONFLICT upsert.
    """
    from accuracy_backfill import all_predictions, all_resolutions

    resolutions = all_resolutions()
    for r in resolutions:
        db.upsert_resolution(
            race_key=r["race_key"],
            race_type=r["race_type"],
            state=r["state"],
            winner=r["winner"],
            winning_party=r.get("winning_party"),
            notes="Seeded from accuracy_backfill.py",
        )

    pred_rows: list[tuple[str, str, float]] = []
    for entry in all_predictions():
        for source, prob in (entry.get("sources") or {}).items():
            try:
                p = float(prob)
            except (TypeError, ValueError):
                continue
            if not 0.0 <= p <= 1.0:
                continue
            pred_rows.append((entry["race_key"], source, p))

    db.upsert_historical_predictions_batch(pred_rows)
    return len(resolutions), len(pred_rows)

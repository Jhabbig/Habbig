"""Empirical recalibration (source='calibrated').

Learns each venue's measured miscalibration from the platform's OWN
resolution ledger (db.resolved_calibration_pairs) and emits corrected
probabilities: if events the market priced 90-100% historically resolved
YES only 79% of the time, a 0.95 baseline maps toward 0.79. The correction
is binned (10% buckets) and shrunk toward identity by bucket sample size
(Laplace-style), so with little history it stays close to the raw market
and sharpens as evidence accrues. Its rows are scored like any other
source — the accuracy page will show whether recalibration actually beats
the raw baseline.

Contract (model module): async compute(markets) -> rows for
db.insert_model_prob: {market_uid, source: 'calibrated', model_prob,
prob_method: 'empirical_recalibration', detail: <json str>}.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import db

log = logging.getLogger(__name__)

N_BINS = 10
DEFAULT_K = 50
DEFAULT_MIN_N = 200
MIN_DELTA = 0.005
CLAMP_LO, CLAMP_HI = 0.01, 0.99


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _k() -> int:
    return _env_int("FORECAST_CALIB_K", DEFAULT_K)


def _min_n() -> int:
    return _env_int("FORECAST_CALIB_MIN_N", DEFAULT_MIN_N)


def _uid(m: dict) -> str:
    return m.get("uid") or f"{m.get('venue')}:{m.get('venue_id')}"


def _scored_today_uids(conn) -> set[str]:
    from datetime import datetime, timezone

    day_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    rows = conn.execute(
        "SELECT DISTINCT market_uid FROM model_probs WHERE source = 'calibrated' AND ts >= ?",
        (day_start,),
    ).fetchall()
    return {r["market_uid"] for r in rows}


def _bin_index(p: float) -> int:
    return min(int(p * N_BINS), N_BINS - 1)


def _bin_stats(pairs: list[tuple[float, int]]) -> list[tuple[float, float, int] | None]:
    """Per-bin (mean predicted prob, hit rate, n); None for empty bins."""
    sums = [0.0] * N_BINS
    hits = [0] * N_BINS
    ns = [0] * N_BINS
    for p, outcome in pairs:
        b = _bin_index(p)
        sums[b] += p
        hits[b] += outcome
        ns[b] += 1
    return [(sums[b] / ns[b], hits[b] / ns[b], ns[b]) if ns[b] else None for b in range(N_BINS)]


def _pav(stats: list[tuple[float, float, int] | None]) -> list[tuple[float, float, int] | None]:
    """Pool-adjacent-violators over the non-empty bins' hit rates (bins are
    already ordered by predicted prob) so the learned mapping can never
    invert ordering across bin boundaries. Violating neighbours are merged
    into their n-weighted mean hit rate; mean_prob and n stay per-bin.
    """
    blocks: list[list] = []  # [weight, weighted_hit_sum, member_bin_indices]
    for i, s in enumerate(stats):
        if s is None:
            continue
        _, h, n = s
        blocks.append([float(n), h * n, [i]])
        while len(blocks) >= 2 and blocks[-2][1] / blocks[-2][0] > blocks[-1][1] / blocks[-1][0]:
            w, wh, members = blocks.pop()
            blocks[-1][0] += w
            blocks[-1][1] += wh
            blocks[-1][2].extend(members)
    out = list(stats)
    for w, wh, members in blocks:
        pooled = wh / w
        for i in members:
            mean_p, _, n = stats[i]
            out[i] = (mean_p, pooled, n)
    return out


def _venue_maps(venues: list[str]) -> tuple[dict[str, tuple[list, int]], set[str]]:
    """({venue: (pav'd bin stats, total resolved pairs)} for venues that
    clear the evidence gate, uids already scored today). One calibrated row
    per market per day — the calibration ledger is daily, and re-emitting
    every models pass only bloats model_probs."""
    min_n = _min_n()
    conn = db.get_conn()
    try:
        maps = {}
        for v in venues:
            pairs = db.resolved_calibration_pairs(conn, platform=v)
            if len(pairs) < min_n:
                continue
            maps[v] = (_pav(_bin_stats(pairs)), len(pairs))
        return maps, _scored_today_uids(conn)
    finally:
        conn.close()


def _compute_sync(markets: list[dict], venues: list[str]) -> list[dict]:
    maps, scored_today = _venue_maps(venues)
    if not maps:
        return []
    k = _k()
    rows = []
    for m in markets:
        entry = maps.get((m.get("venue") or "").lower())
        if entry is None:
            continue
        if _uid(m) in scored_today:
            continue
        p = m.get("baseline_prob")
        if p is None:
            continue
        stats, venue_n = entry
        b = _bin_index(p)
        s = stats[b]
        if s is None:
            continue
        mean_p, h, n = s
        w = n / (n + k)
        corrected = min(CLAMP_HI, max(CLAMP_LO, p + w * (h - mean_p)))
        if abs(corrected - p) < MIN_DELTA:
            continue
        rows.append(
            {
                "market_uid": _uid(m),
                "source": "calibrated",
                "model_prob": round(corrected, 4),
                "prob_method": "empirical_recalibration",
                "detail": json.dumps(
                    {
                        "bin": b,
                        "n_bin": n,
                        "bin_hit_rate": round(h, 4),
                        "bin_mean_prob": round(mean_p, 4),
                        "weight": round(w, 4),
                        "venue_n": venue_n,
                    }
                ),
            }
        )
    return rows


async def compute(markets: list[dict]) -> list[dict]:
    venues = sorted({(m.get("venue") or "").lower() for m in markets} - {"", "custom"})
    if not venues:
        return []
    return await asyncio.to_thread(_compute_sync, markets, venues)

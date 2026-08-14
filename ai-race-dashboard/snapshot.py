"""Daily snapshot store + delta computation.

Each day we drop a JSON file under `.snapshots/ai-race/YYYY-MM-DD.json`
containing the merged leaderboard, frontier series, and a few summary
counters. The server reads these to:

- power the "Recent score changes" panel (model × benchmark cells that
  moved the most since the previous snapshot)
- surface alert-worthy step-ups (≥2pp on a benchmark) to /api/alerts

Storage:
- File-per-day to keep things grep-able and human-inspectable.
- One snapshot per day max — re-takes within a day overwrite that file.
- We never delete snapshots automatically; operator can prune if needed.

This module is intentionally self-contained — no external deps beyond
the curated/merged data layer it already has.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import data as ai_data
import live_data

log = logging.getLogger(__name__)

SNAPSHOTS_DIR = Path(__file__).parent / ".snapshots" / "ai-race"
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _snapshot_path(date: str) -> Path:
    return SNAPSHOTS_DIR / f"{date}.json"


def take_snapshot(date: str | None = None, force: bool = False) -> dict:
    """Write today's snapshot (or `date`). Returns the snapshot payload."""
    date = date or _today()
    path = _snapshot_path(date)
    with _lock:
        if path.exists() and not force:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass  # corrupt → re-take

        merged = live_data.merged_models()
        # Strip score_meta from snapshot rows (we only need score, name, lab) —
        # keeps files small enough to diff cleanly.
        thin_models = [
            {
                "name": m["name"],
                "lab_key": m["lab_key"],
                "released": m.get("released"),
                "scores": m.get("scores", {}),
            }
            for m in merged["models"]
        ]
        payload = {
            "date": date,
            "taken_at": time.time(),
            "as_of": merged.get("as_of"),
            "models": thin_models,
            "benchmarks": [b["key"] for b in merged["benchmarks"]],
        }
        path.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
        log.info("snapshot written: %s (%d models)", path.name, len(thin_models))
        return payload


def list_snapshots() -> list[str]:
    return sorted(p.stem for p in SNAPSHOTS_DIR.glob("*.json") if len(p.stem) == 10)


def load_snapshot(date: str) -> dict | None:
    path = _snapshot_path(date)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("snapshot load failed %s: %s", date, e)
        return None


def _index_models(snap: dict) -> dict[str, dict]:
    return {m["name"]: m for m in (snap.get("models") or [])}


def compute_deltas(since: str, until: str | None = None, top_n: int = 30) -> list[dict]:
    """Return per (model, benchmark) deltas between two snapshots.

    `since` and `until` are YYYY-MM-DD. `until` defaults to the latest
    snapshot. Each row is signed (positive = score went up). Sorted by
    absolute delta descending. NaN / missing-on-either-side cells are
    omitted from the results.
    """
    snaps = list_snapshots()
    if not snaps:
        return []
    if until is None:
        until = snaps[-1]
    # Snap requested dates onto the nearest available snapshot ≤ date.
    def floor_to_existing(d: str) -> str | None:
        eligible = [s for s in snaps if s <= d]
        return eligible[-1] if eligible else None

    s_date = floor_to_existing(since)
    u_date = floor_to_existing(until)
    if not s_date or not u_date or s_date == u_date:
        return []

    s = load_snapshot(s_date) or {}
    u = load_snapshot(u_date) or {}
    s_models = _index_models(s)
    u_models = _index_models(u)
    out: list[dict] = []
    for name, u_row in u_models.items():
        s_row = s_models.get(name)
        if not s_row:
            continue
        for bench_key, curr in (u_row.get("scores") or {}).items():
            if curr is None:
                continue
            prev = (s_row.get("scores") or {}).get(bench_key)
            if prev is None:
                continue
            delta = curr - prev
            if delta == 0:
                continue
            out.append({
                "model": name,
                "lab_key": u_row.get("lab_key"),
                "benchmark": bench_key,
                "prev": prev,
                "curr": curr,
                "delta": delta,
            })
    out.sort(key=lambda r: abs(r["delta"]), reverse=True)
    out = out[:top_n]
    # Decorate with lab name/color for the UI.
    for r in out:
        lab = ai_data.lab_by_key(r["lab_key"]) or {}
        r["lab_name"] = lab.get("name", r["lab_key"])
        r["lab_color"] = lab.get("color", "#888")
    return out


def alerts(min_delta_by_scale: dict[str, float] | None = None) -> list[dict]:
    """Step-ups vs the previous snapshot (default thresholds: 2pp / 20 Elo).

    Returns the same shape as compute_deltas but filtered to alert-worthy
    moves and *only* positive deltas — useful for "frontier just moved" pings.
    """
    snaps = list_snapshots()
    if len(snaps) < 2:
        return []
    thresholds = min_delta_by_scale or {"pct": 2.0, "elo": 20.0}
    # Use the snapshot immediately before the latest one as the "since" anchor.
    deltas = compute_deltas(since=snaps[-2], until=snaps[-1], top_n=200)
    bench_scales = {b["key"]: b.get("scale", "") for b in ai_data.BENCHMARKS}
    out = []
    for r in deltas:
        if r["delta"] <= 0:
            continue
        scale_key = "elo" if bench_scales.get(r["benchmark"]) == "Elo" else "pct"
        if r["delta"] >= thresholds.get(scale_key, 0):
            out.append(r)
    return out

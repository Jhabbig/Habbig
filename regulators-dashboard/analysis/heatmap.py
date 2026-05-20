"""Per-week, per-regulator, per-type aggregation for the v0.3 heatmap.

Buckets every classified item into an ISO week (Monday-start), groups by
source code, and counts by `primary_tag`. The UI renders the output as a
strip of stacked weekly bars per regulator — surfaces patterns like
"FCA went quiet, SEC ramping in enforcement" by eye-balling row density.

A rectangular grid is emitted (every regulator × every week, zeros
included) so the UI can render without per-cell null checks. Regulator
order follows `sources_status` from `unified_feed` for stability across
calls.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

# Canonical render order for the stacked segments. Items with no positive
# tag from the classifier land in "other", same bucket the UI uses.
TAG_ORDER: tuple[str, ...] = (
    "enforcement",
    "rulemaking",
    "guidance",
    "speech",
    "personnel",
    "other",
)


def _week_start(iso: str) -> str | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_utc = dt.astimezone(timezone.utc).date()
    monday = dt_utc - timedelta(days=dt_utc.weekday())
    return monday.isoformat()


def aggregate(items: list[dict], sources_status: list[dict], weeks: int = 12) -> dict:
    """Build a per-regulator × per-week × per-tag count grid."""
    weeks = max(4, min(weeks, 52))
    today = datetime.now(timezone.utc).date()
    this_monday = today - timedelta(days=today.weekday())
    week_starts = [
        (this_monday - timedelta(days=7 * (weeks - 1 - i))).isoformat()
        for i in range(weeks)
    ]
    week_set = set(week_starts)

    # source_code → week → tag → count, lazily filled with zeros.
    counts: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: {w: {t: 0 for t in TAG_ORDER} for w in week_starts}
    )

    for it in items:
        src = it.get("source")
        if not src:
            continue
        wk = _week_start(it.get("published", ""))
        if wk is None or wk not in week_set:
            continue
        tag = it.get("primary_tag") or "other"
        if tag not in TAG_ORDER:
            tag = "other"
        counts[src][wk][tag] += 1

    regulators: list[dict] = []
    global_max = 0
    for s in sources_status:
        code = s["code"]
        per_week = counts.get(code, {w: {t: 0 for t in TAG_ORDER} for w in week_starts})
        by_week: list[dict] = []
        total = 0
        for w in week_starts:
            tags = per_week[w]
            wk_total = sum(tags.values())
            global_max = max(global_max, wk_total)
            total += wk_total
            by_week.append({"week": w, "total": wk_total, "by_tag": tags})
        regulators.append({
            "code": code,
            "name": s["name"],
            "jurisdiction": s["jurisdiction"],
            "total": total,
            "by_week": by_week,
        })

    return {
        "weeks": week_starts,
        "regulators": regulators,
        "global_max": global_max,
        "tags": list(TAG_ORDER),
    }


# --- Self-test --------------------------------------------------------------

if __name__ == "__main__":
    import json
    now = datetime.now(timezone.utc)
    fake_items = [
        # SEC: spread of types over recent weeks
        {"source": "SEC", "primary_tag": "enforcement", "published": (now - timedelta(days=2)).isoformat()},
        {"source": "SEC", "primary_tag": "enforcement", "published": (now - timedelta(days=3)).isoformat()},
        {"source": "SEC", "primary_tag": "rulemaking",  "published": (now - timedelta(days=10)).isoformat()},
        {"source": "SEC", "primary_tag": "speech",      "published": (now - timedelta(days=11)).isoformat()},
        # FCA: just two items
        {"source": "FCA", "primary_tag": "enforcement", "published": (now - timedelta(days=1)).isoformat()},
        {"source": "FCA", "primary_tag": "guidance",    "published": (now - timedelta(days=20)).isoformat()},
        # ESMA: silent
        # Out-of-window item — should not be counted
        {"source": "SEC", "primary_tag": "enforcement", "published": (now - timedelta(days=400)).isoformat()},
        # Missing data — should be skipped
        {"source": "SEC", "primary_tag": "enforcement", "published": ""},
        {"source": None,  "primary_tag": "enforcement", "published": now.isoformat()},
    ]
    fake_sources = [
        {"code": "SEC", "name": "SEC", "jurisdiction": "US"},
        {"code": "FCA", "name": "FCA", "jurisdiction": "UK"},
        {"code": "ESMA","name": "ESMA","jurisdiction": "EU"},
    ]
    out = aggregate(fake_items, fake_sources, weeks=8)
    print(f"weeks={len(out['weeks'])}  global_max={out['global_max']}")
    for r in out["regulators"]:
        print(f"  {r['code']:5s} total={r['total']}  "
              f"per_week_totals={[wk['total'] for wk in r['by_week']]}")
    # Spot-check: SEC total should be 4 (the 400-day-old item + missing + None-source are dropped)
    assert out["regulators"][0]["total"] == 4, out["regulators"][0]
    assert out["regulators"][1]["total"] == 2
    assert out["regulators"][2]["total"] == 0
    print("smoke OK")

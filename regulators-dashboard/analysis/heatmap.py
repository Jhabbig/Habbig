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


def aggregate(items: list[dict], sources_status: list[dict],
              weeks: int = 12, hide_empty: bool = True,
              group_by: str = "source") -> dict:
    """Build a per-{source|jurisdiction} × per-week × per-tag count grid.

    `group_by` ∈ {"source", "jurisdiction"}:
      - "source"       — one row per regulator code (default; 54 rows at v2.1)
      - "jurisdiction" — one row per ISO jurisdiction (collapses to ~13-34
                         rows; useful at scale for at-a-glance regional view)

    `hide_empty=True` (default) drops rows with total=0 across the
    window — essential once the source list passes ~20 bodies, otherwise
    the rendered heatmap is mostly blank rows. Pass `hide_empty=False`
    if a caller wants every row in the output."""
    if group_by not in ("source", "jurisdiction"):
        group_by = "source"
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

    # Build a code → jurisdiction lookup from sources_status so items with
    # source codes get bucketed correctly when group_by="jurisdiction".
    code_to_jx = {s["code"]: s["jurisdiction"] for s in sources_status}

    def bucket_key(item: dict) -> str:
        src = item.get("source")
        if not src:
            return ""
        if group_by == "jurisdiction":
            return code_to_jx.get(src, item.get("jurisdiction") or "OTHER")
        return src

    for it in items:
        key = bucket_key(it)
        if not key:
            continue
        wk = _week_start(it.get("published", ""))
        if wk is None or wk not in week_set:
            continue
        tag = it.get("primary_tag") or "other"
        if tag not in TAG_ORDER:
            tag = "other"
        counts[key][wk][tag] += 1

    # Build the response in the order of `sources_status` so row order is
    # stable across calls. For jurisdiction grouping, collapse to distinct
    # jurisdictions in first-appearance order.
    if group_by == "jurisdiction":
        ordered_keys: list[str] = []
        seen: set[str] = set()
        jx_to_codes: dict[str, list[str]] = defaultdict(list)
        for s in sources_status:
            jx = s["jurisdiction"]
            jx_to_codes[jx].append(s["code"])
            if jx not in seen:
                ordered_keys.append(jx)
                seen.add(jx)
        row_meta = {k: {"name": k, "jurisdiction": k, "sources": jx_to_codes[k]} for k in ordered_keys}
    else:
        ordered_keys = [s["code"] for s in sources_status]
        row_meta = {s["code"]: {"name": s["name"], "jurisdiction": s["jurisdiction"], "sources": [s["code"]]}
                    for s in sources_status}

    regulators: list[dict] = []
    global_max = 0
    for key in ordered_keys:
        per_week = counts.get(key, {w: {t: 0 for t in TAG_ORDER} for w in week_starts})
        by_week: list[dict] = []
        total = 0
        for w in week_starts:
            tags = per_week[w]
            wk_total = sum(tags.values())
            global_max = max(global_max, wk_total)
            total += wk_total
            by_week.append({"week": w, "total": wk_total, "by_tag": tags})
        meta = row_meta[key]
        regulators.append({
            "code": key,
            "name": meta["name"],
            "jurisdiction": meta["jurisdiction"],
            "member_sources": meta["sources"],
            "total": total,
            "by_week": by_week,
        })

    return {
        "weeks": week_starts,
        "regulators": (
            [r for r in regulators if r["total"] > 0]
            if hide_empty else regulators
        ),
        "group_by": group_by,
        "total_registered_regulators": len(regulators),
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
        # CFTC: also US, used to verify jurisdiction grouping collapses both
        {"source": "CFTC", "primary_tag": "enforcement", "published": (now - timedelta(days=4)).isoformat()},
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
        {"code": "CFTC", "name": "CFTC", "jurisdiction": "US"},
        {"code": "FCA", "name": "FCA", "jurisdiction": "UK"},
        {"code": "ESMA","name": "ESMA","jurisdiction": "EU"},
    ]
    print("--- group_by=source ---")
    out_src = aggregate(fake_items, fake_sources, weeks=8, group_by="source")
    print(f"  visible regulators: {[(r['code'], r['total']) for r in out_src['regulators']]}")
    assert out_src["regulators"][0]["code"] == "SEC"
    assert out_src["regulators"][0]["total"] == 4
    print("--- group_by=jurisdiction ---")
    out_jx = aggregate(fake_items, fake_sources, weeks=8, group_by="jurisdiction")
    print(f"  visible jurisdictions: {[(r['code'], r['total'], r['member_sources']) for r in out_jx['regulators']]}")
    us = next(r for r in out_jx["regulators"] if r["code"] == "US")
    assert us["total"] == 5  # SEC (4) + CFTC (1)
    assert set(us["member_sources"]) == {"SEC", "CFTC"}
    print("smoke OK")

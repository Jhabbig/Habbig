"""Stance ladder — score the latest statement per CB and rank hawkish→dovish."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ingestion import cb_statements

from . import historical_store, stance_scorer

log = logging.getLogger(__name__)


def compute() -> dict:
    feeds = cb_statements.get_cached()["feeds"]
    rows: list[dict] = []
    for f in feeds:
        latest = f["latest"]
        if not latest:
            rows.append({
                "cb": f["cb"],
                "cb_name": f["cb_name"],
                "title": None,
                "link": None,
                "published": None,
                "summary": None,
                "score": None,
                "error": "no statement available (RSS unreachable or no match)",
            })
            continue
        text = latest.get("scoring_text") or latest.get("summary") or ""
        result = stance_scorer.score(text)
        rows.append({
            "cb": f["cb"],
            "cb_name": f["cb_name"],
            "title": latest["title"],
            "link": latest["link"],
            "published": latest["published"],
            "summary": (latest.get("summary") or "")[:500],  # truncate for transport
            "scoring_source": latest.get("scoring_source", "summary"),
            "scoring_chars": len(text),
            **result.to_dict(),
        })

    # Sort hawkish (high) → dovish (low). Unknowns go last.
    rows.sort(key=lambda r: (
        1 if r.get("norm_score") is None else 0,
        -(r.get("norm_score") or 0),
    ))

    # Snapshot per-CB norm scores for delta queries — lets right_now show
    # "Fed stance hawkish (+0.4 vs 7d ago)" instead of a static reading.
    try:
        snap_items: list[tuple[str, float | None]] = []
        for r in rows:
            snap_items.append((f"stance.{r['cb']}.norm", r.get("norm_score")))
        historical_store.snapshot_many(snap_items)
    except Exception as exc:
        log.warning("history snapshot failed in stance: %s", exc)

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(compute(), indent=2)[:3000])

"""Daily-headlines snapshot — one row per UTC day capturing the top of the
dashboard at that moment in time. Cheap to build (just queries the existing
caches) so the worker overwrites today's row on every cycle; the row's
`created_at` reflects the most recent capture.

Pruning: handled implicitly by the days-window cutoff at read time. Even
running 30 days, the table only holds ~30 rows.
"""

from __future__ import annotations

import datetime as _dt
import logging

import cache
import edge
import index_calc
import surge_calc

log = logging.getLogger(__name__)


def today_utc() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%d")


def build_today_payload() -> dict:
    """Snapshot of the day's top signals — independent of past content."""
    idx = index_calc.compute()
    top_surges = [
        {"title": s["title"], "source": s["source"], "section": s["section"],
         "z_score": s.get("z_score")}
        for s in surge_calc.compute(limit=3)
    ]
    top_topics = [
        {"label": t["label"], "spread": t["spread"],
         "sources": t["sources"], "surge_signal": t.get("surge_signal")}
        for t in edge.compute_topics_with_markets(limit=6)
        if t.get("surge_signal") is not None or t["spread"] >= 3
    ][:3]
    top_news = [
        {"title": n["title"], "url": n.get("url"),
         "feed": (n.get("extra") or {}).get("feed")}
        for n in cache.get_section("news", limit=3)
    ]
    return {
        "overall": idx.get("overall"),
        "sections": {k: v.get("score") for k, v in idx.get("sections", {}).items()},
        "top_surges": top_surges,
        "top_topics": top_topics,
        "top_news": top_news,
    }


def write_today() -> dict:
    payload = build_today_payload()
    cache.upsert_daily_headline(today_utc(), payload)
    return payload

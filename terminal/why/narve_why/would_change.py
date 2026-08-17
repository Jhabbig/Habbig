"""What would change this number — 2 to 4 watch-list items.

Rank = driver weight x staleness. Evidence refs are opaque ids in v1 (no
parseable timestamps), so staleness falls back to driver weight order, which
extract_drivers already provides. Phrasing comes from a fixed per-tag table;
a tag that appears in several drivers advances to its next phrase, so the
list never repeats an item. Deterministic.
"""

from __future__ import annotations

from .drivers import Driver
from .schemas import ExplainRequest

__all__ = ["WOULD_CHANGE_PHRASES", "would_change"]

WOULD_CHANGE_PHRASES: dict[str, tuple[str, ...]] = {
    "governance": (
        "A leadership shake-up or major retirement announcement",
        "A rules or ballot-access change in a decisive state",
    ),
    "funding": (
        "A quarterly fundraising report that breaks the pattern",
        "A major outside-money commitment to the close races",
    ),
    "polling": (
        "New national generic-ballot print",
        "Any state-level poll after the next debate",
        "A methodological revision from a high-credibility pollster",
    ),
    "economy": (
        "A surprise in the next jobs or inflation print",
        "A sharp move in consumer sentiment",
    ),
    "legal": (
        "A court ruling that directly touches the question",
        "A new filing that changes who is on the ballot",
    ),
    "incident": (
        "A news-cycle-dominating incident tied to the race",
        "An official investigation opening or closing",
    ),
    "momentum": (
        "A sustained multi-week shift in the model trendline",
        "Two consecutive model updates moving the same direction",
    ),
    "market_structure": (
        "A large, liquid order repricing the market",
        "A new venue listing the same question at scale",
    ),
}


def would_change(req: ExplainRequest, drivers: list[Driver]) -> list[str]:
    """2-4 items, ranked by driver weight order (staleness fallback, see above)."""
    items: list[str] = []
    used: dict[str, int] = {}
    for driver in drivers:
        phrases = WOULD_CHANGE_PHRASES[driver.tag]
        index = used.get(driver.tag, 0)
        if index < len(phrases):
            items.append(phrases[index])
            used[driver.tag] = index + 1
        if len(items) == 4:
            break
    if len(items) < 2 and drivers:
        top_tag = drivers[0].tag
        phrases = WOULD_CHANGE_PHRASES[top_tag]
        index = used.get(top_tag, 0)
        while len(items) < 2 and index < len(phrases):
            items.append(phrases[index])
            index += 1
        used[top_tag] = index
    return items

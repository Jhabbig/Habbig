"""State-level mood proxy + tile-map layout.

The state-level mood is intentionally a single proxy — unemployment rate
inverted against a 3 → 9 percent band — because that's the only indicator
with comparable national coverage at monthly cadence from FRED. We surface
the 4-year change in pp alongside so the reader can tell whether the
absolute level is the headline or the trend is.

The tile-map layout is a hand-built 9×11 grid that places adjacent states
near each other without trying to be a real geographic map. It's the
standard "tilegram" trick used by news organisations to give readers a
quick visual scan without making Texas the size of Massachusetts.
"""

from __future__ import annotations


# Linear mood proxy: 3% unemployment → 100, 9%+ → 0.
def mood_from_unrate(unrate: float | None) -> float | None:
    if unrate is None:
        return None
    score = 100 - ((unrate - 3.0) * (100.0 / 6.0))
    return max(0.0, min(100.0, score))


# 9 rows × 11 cols. (row, col) chosen so that adjacent states cluster.
# AK and HI break the bottom-left convention; ME tucks into row 0 alone.
TILE_LAYOUT: dict[str, tuple[int, int]] = {
    "AK": (0, 0),                                                                           "ME": (0, 10),
    "WA": (1, 1), "ID": (1, 2), "MT": (1, 3), "ND": (1, 4), "MN": (1, 5), "WI": (1, 6),                  "MI": (1, 7), "NY": (1, 8), "VT": (1, 9), "NH": (1, 10),
    "OR": (2, 1), "NV": (2, 2), "WY": (2, 3), "SD": (2, 4), "IA": (2, 5), "IL": (2, 6),                  "IN": (2, 7), "OH": (2, 8), "PA": (2, 9), "NJ": (2, 10),
    "CA": (3, 1), "UT": (3, 2), "CO": (3, 3), "NE": (3, 4), "MO": (3, 5), "KY": (3, 6),                  "WV": (3, 7), "VA": (3, 8), "MD": (3, 9), "MA": (3, 10),
                  "AZ": (4, 2), "NM": (4, 3), "KS": (4, 4), "AR": (4, 5), "TN": (4, 6),                  "NC": (4, 7), "SC": (4, 8), "DC": (4, 9), "DE": (4, 10),
                                "OK": (5, 4), "LA": (5, 5), "MS": (5, 6), "AL": (5, 7), "GA": (5, 8),                                "RI": (5, 10),
                                "TX": (6, 4),                                                                                       "CT": (6, 10),
                                                                                            "FL": (7, 9),
    "HI": (8, 0),
}


def annotate(states: list[dict]) -> list[dict]:
    """Attach mood + tile (row, col) to each state row."""
    out: list[dict] = []
    for s in states:
        latest = s.get("latest")
        v = latest["value"] if latest else None
        tile = TILE_LAYOUT.get(s["postal"])
        out.append({
            **s,
            "mood": mood_from_unrate(v),
            "tile": {"row": tile[0], "col": tile[1]} if tile else None,
        })
    return out


def compose(states_payload: dict) -> dict:
    """Build the API response: annotated state list + national benchmark."""
    states = annotate(states_payload.get("states") or [])
    benchmark = states_payload.get("benchmark") or {}
    benchmark = {
        **benchmark,
        "national_mood_at_median": mood_from_unrate(benchmark.get("median")),
    }
    # Top 5 best / worst by unemployment (lower is better)
    rated = [s for s in states if s.get("latest")]
    rated.sort(key=lambda r: r["latest"]["value"])
    best = [s["postal"] for s in rated[:5]]
    worst = [s["postal"] for s in rated[-5:]][::-1]
    # Top 5 biggest 1y improvements / worsenings
    by_1y = [s for s in states if s.get("delta_1y_pp") is not None]
    by_1y.sort(key=lambda r: r["delta_1y_pp"])
    biggest_improvers = [s["postal"] for s in by_1y[:5]]
    biggest_worseners = [s["postal"] for s in by_1y[-5:]][::-1]
    return {
        "states": states,
        "benchmark": benchmark,
        "rankings": {
            "lowest_unemployment": best,
            "highest_unemployment": worst,
            "biggest_1y_improvers": biggest_improvers,
            "biggest_1y_worseners": biggest_worseners,
        },
    }

"""Region-specific mood score.

Computes the same pocketbook / jobs / sentiment composite the national
gauge uses, but with regional CPI YoY and the average state-unemployment
rate from states in that region. Sentiment (UMCSENT) is only published
nationally, so we use the same value for all four regions.

The bands are deliberately identical to those in `mood_index.py` — the
only thing that changes per region is the input numbers. That makes the
comparison "how does my region's mood differ from the national one"
clean: it's an apples-to-apples cut, not a different formula.
"""

from __future__ import annotations

from . import mood_index


def _avg(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _pocketbook_score(cpi_yoy: float | None) -> float | None:
    """Subset of the national pocketbook sub-score: we only have regional CPI
    here, so we rebuild the score from CPI YoY alone. Same 0/4/8 band."""
    if cpi_yoy is None:
        return None
    return max(0.0, min(100.0, 100 - cpi_yoy * 12.5))


def _jobs_score(avg_unrate: float | None) -> float | None:
    if avg_unrate is None:
        return None
    return max(0.0, min(100.0, 100 - (avg_unrate - 3.0) * (100.0 / 6.0)))


def compose(regional_cpi: list[dict],
            state_payload: dict,
            national_umich: float | None) -> dict:
    """Build the per-region mood payload.

    regional_cpi    : list from regional_cpi_client (region, latest, yoy_pct)
    state_payload   : raw dict from states_client (states list with region tags + latest)
    national_umich  : current UMich CSI value (used as the sentiment input)
    """
    states = state_payload.get("states") or []
    by_region: dict[str, list[float]] = {}
    for s in states:
        if not s.get("latest"):
            continue
        by_region.setdefault(s["region"], []).append(s["latest"]["value"])

    # National sentiment slot (constant across regions)
    sentiment_score: float | None = None
    if national_umich is not None:
        sentiment_score = max(0.0, min(100.0, (national_umich - 50.0) / 60.0 * 100.0))

    out: list[dict] = []
    for cpi in regional_cpi:
        region = cpi["region"]
        avg_unrate = _avg(by_region.get(region, []))
        pb = _pocketbook_score(cpi.get("yoy_pct"))
        jb = _jobs_score(avg_unrate)
        st = sentiment_score
        parts = [v for v in (pb, jb, st) if v is not None]
        overall = sum(parts) / len(parts) if parts else None
        out.append({
            "region": region,
            "overall": overall,
            "label": mood_index.label_for(overall),
            "pocketbook": {
                "score": pb,
                "cpi_yoy_pct": cpi.get("yoy_pct"),
                "cpi_latest": cpi.get("latest"),
            },
            "jobs": {
                "score": jb,
                "avg_state_unrate_pct": avg_unrate,
                "n_states": len(by_region.get(region, [])),
            },
            "sentiment": {
                "score": st,
                "umich_csi": national_umich,
                "note": "UMich consumer sentiment is published nationally only",
            },
        })

    # National baseline for "your region vs national"
    national_cpi_yoy_avg = _avg([c["yoy_pct"] for c in regional_cpi if c.get("yoy_pct") is not None])
    national_unrate_avg = _avg([s["latest"]["value"] for s in states if s.get("latest")])
    national_pb = _pocketbook_score(national_cpi_yoy_avg)
    national_jb = _jobs_score(national_unrate_avg)
    parts = [v for v in (national_pb, national_jb, sentiment_score) if v is not None]
    national_overall = sum(parts) / len(parts) if parts else None

    return {
        "regions": out,
        "national_baseline": {
            "overall": national_overall,
            "label": mood_index.label_for(national_overall),
            "pocketbook_score": national_pb,
            "jobs_score": national_jb,
            "sentiment_score": sentiment_score,
        },
    }

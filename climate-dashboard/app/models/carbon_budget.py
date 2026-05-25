"""Remaining carbon budget for IPCC warming targets.

IPCC AR6 WG1 Table 5.8 reports global remaining carbon budgets for limiting
peak warming with a given probability. We anchor on the IPCC values
(start-of-2020 budgets) and subtract cumulative global emissions since 2020
from OWID. Pure derivative of existing data — no new URLs.

Anchor budgets (start of 2020, GtCO2; IPCC AR6 Ch5 Table 5.8):
  +1.5°C, 50% probability:   500
  +1.5°C, 67% probability:   400
  +2.0°C, 67% probability:  1150

Years remaining = remaining / latest_annual_emissions. Both numbers are
expressed honestly — "if emissions stay at today's rate", not "until we
breach the target." The point is that the budgets are tighter than the
headline year of 2050 suggests.
"""
from __future__ import annotations

from typing import Optional

# (label, target_c, probability, GtCO2 at start of 2020)
ANCHORS = [
    {"label": "+1.5°C (50% chance)", "target_c": 1.5, "probability": 0.50, "budget_2020_gt": 500},
    {"label": "+1.5°C (67% chance)", "target_c": 1.5, "probability": 0.67, "budget_2020_gt": 400},
    {"label": "+2.0°C (67% chance)", "target_c": 2.0, "probability": 0.67, "budget_2020_gt": 1150},
]
ANCHOR_YEAR = 2020


def compute(emissions_parsed: Optional[dict]) -> Optional[dict]:
    """Build the per-target remaining-budget table.

    ``emissions_parsed`` is the OWID emissions parse output (must include
    the World row keyed under ``parsed["world_key"]``). Returns None if we
    can't find world data — frontend will then hide the card.
    """
    if not emissions_parsed or not emissions_parsed.get("countries"):
        return None
    world_key = emissions_parsed.get("world_key")
    world = emissions_parsed["countries"].get(world_key) if world_key else None
    if not world or not world.get("data"):
        return None
    data = world["data"]
    years = sorted(data.keys())
    latest_year = years[-1]
    # MT to Gt conversion: OWID stores total CO2 in million tonnes; budgets
    # are in Gt. 1 Gt = 1000 Mt.
    cumulative_since_anchor_mt = sum(
        data[y].get("co2_mt", 0) for y in years if y >= ANCHOR_YEAR
    )
    cumulative_since_anchor_gt = cumulative_since_anchor_mt / 1000.0
    latest_annual_gt = data[latest_year].get("co2_mt", 0) / 1000.0

    rows = []
    for a in ANCHORS:
        remaining = a["budget_2020_gt"] - cumulative_since_anchor_gt
        years_left = (remaining / latest_annual_gt) if (remaining > 0 and latest_annual_gt > 0) else None
        rows.append({
            "label": a["label"],
            "target_c": a["target_c"],
            "probability": a["probability"],
            "remaining_gt": round(remaining, 0),
            "years_at_current_rate": round(years_left, 1) if years_left is not None else None,
            "exhausted": remaining <= 0,
        })
    return {
        "anchor_year": ANCHOR_YEAR,
        "latest_year": latest_year,
        "cumulative_since_anchor_gt": round(cumulative_since_anchor_gt, 1),
        "latest_annual_gt": round(latest_annual_gt, 2),
        "budgets": rows,
        "source": "IPCC AR6 WG1 Table 5.8 (2020 anchors) + Our World in Data (cumulative emissions).",
    }

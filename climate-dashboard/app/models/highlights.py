"""Auto-derived "what's notable" findings from the existing data sources.

Highlights are pure functions of already-cached data — they don't fetch
anything new, they just surface the most interesting one-line takes the
dashboard would otherwise hide in the cards. Each finding has a kind
(``record`` / ``trend`` / ``alert`` / ``regime`` / ``milestone``) which the
frontend uses to colour-code the chip.
"""
from __future__ import annotations

from typing import Optional

from ..fetchers.oni import state_for
from .sea_ice import daily_record_check


def _enso_streak(oni_series: list[dict]) -> tuple[Optional[str], int]:
    """Return (current_state, consecutive_months_in_that_state). Counts back
    from the latest month while the state matches."""
    if not oni_series:
        return None, 0
    cur_state = state_for(oni_series[-1]["oni"])
    if cur_state == "Neutral":
        # We still count neutral streaks — they're sometimes notable too.
        pass
    n = 1
    for s in reversed(oni_series[:-1]):
        if state_for(s["oni"]) == cur_state:
            n += 1
        else:
            break
    return cur_state, n


def _years_above(annual: list[dict], threshold: float) -> int:
    """Consecutive years (counting back from the latest) with anomaly above
    ``threshold``."""
    n = 0
    for a in reversed(annual):
        if a["anomaly_c"] > threshold:
            n += 1
        else:
            break
    return n


def compute(*, gistemp: Optional[dict] = None,
            co2: Optional[dict] = None,
            methane: Optional[dict] = None,
            n2o: Optional[dict] = None,
            sea_ice: Optional[dict] = None,
            oni: Optional[dict] = None,
            zonal: Optional[dict] = None) -> list[dict]:
    """Build a list of {kind, text} chips. Order matters — most important
    first since the frontend may only show the top few."""
    out: list[dict] = []

    # 1) Was the last completed year a temperature record?
    if gistemp and gistemp.get("annual"):
        annual = [a for a in gistemp["annual"] if a.get("anomaly_c") is not None]
        if annual:
            latest = annual[-1]
            prior = annual[:-1]
            if prior and latest["anomaly_c"] > max(p["anomaly_c"] for p in prior):
                out.append({
                    "kind": "record",
                    "text": f"{latest['year']} set a new annual temperature record at +{latest['anomaly_c']:.2f}°C (vs 1951-1980).",
                })
            # How long since the anomaly dropped below 1.0°C?
            n_above_1_0 = _years_above(annual, 1.0)
            if n_above_1_0 >= 2:
                out.append({
                    "kind": "milestone",
                    "text": f"Annual temperature anomaly has exceeded +1.0°C for {n_above_1_0} consecutive years.",
                })
            # 1.5°C specifically
            n_above_1_5 = _years_above(annual, 1.5)
            if n_above_1_5 >= 1:
                out.append({
                    "kind": "milestone",
                    "text": f"Annual temperature anomaly has exceeded +1.5°C for {n_above_1_5} consecutive year{'s' if n_above_1_5 != 1 else ''}.",
                })

    # 2) 12-month change in CO₂ / CH₄ / N₂O — the "rate of accumulation" framing.
    for series, label, unit, key, dp in (
        (co2 and co2.get("monthly"), "CO₂", "ppm", "ppm", 2),
        (methane and methane.get("monthly"), "CH₄", "ppb", "ppb", 1),
        (n2o and n2o.get("monthly"), "N₂O", "ppb", "ppb", 2),
    ):
        if not series or len(series) < 13:
            continue
        latest_v = series[-1][key]
        prior_v = series[-13][key]
        delta = latest_v - prior_v
        sign = "+" if delta >= 0 else ""
        out.append({
            "kind": "trend",
            "text": f"{label} rose {sign}{delta:.{dp}f} {unit} over the last 12 months (now {latest_v:.{dp}f} {unit}).",
        })

    # 3) Arctic sea-ice rank for today's DOY — alert if top-3 lowest.
    if sea_ice:
        rec = daily_record_check(sea_ice)
        if rec and rec.get("rank_lowest_in_record"):
            r = rec["rank_lowest_in_record"]
            if r <= 3:
                out.append({
                    "kind": "alert",
                    "text": f"Arctic sea-ice extent today is the #{r} lowest on record for {rec['date'][5:]} (across {rec['history_years']} years).",
                })

    # 3b) Arctic-warms-faster framing. Pulls from GISTEMP zonal: warming of
    # the 64N-90N band relative to global mean. The 3-4× ratio is one of
    # the most-cited climate facts; surfacing it makes the dashboard's
    # framing match scientific consensus.
    if zonal:
        from ..fetchers.gistemp_zonal import warming_ratios
        ratios = warming_ratios(zonal)
        if ratios and "64N-90N" in ratios["bands"]:
            arctic = ratios["bands"]["64N-90N"]
            globe = ratios["bands"].get("Glob")
            if globe and arctic["ratio_vs_global"] >= 1.5:
                out.append({
                    "kind": "alert",
                    "text": (f"Arctic (64°N-90°N) has warmed +{arctic['anomaly_c']:.2f}°C "
                             f"since {ratios['baseline']} — {arctic['ratio_vs_global']:.1f}× the "
                             f"global rate (+{globe['anomaly_c']:.2f}°C)."),
                })

    # 4) ENSO state + streak.
    if oni and oni.get("monthly"):
        state, n = _enso_streak(oni["monthly"])
        if state and state != "Neutral" and n >= 2:
            out.append({
                "kind": "regime",
                "text": f"ENSO has been {state} for {n} consecutive month{'s' if n != 1 else ''} (latest ONI {oni['monthly'][-1]['oni']:+.2f}).",
            })

    return out

#!/usr/bin/env python3
"""Seed snapshots.db with historical year-end Love Index rankings.

Run once after deploy (or after wiping the DB) to give the time-series
insight rules — mover, trend_reversal, event_overlay — usable depth from
day one instead of waiting a year of live snapshots to accumulate.

What it does:
  - Pulls FULL time-series from Eurostat (marriage + divorce rates) and
    World Bank WDI (adolescent fertility) — the three feeds that have
    multi-year history without operator intervention.
  - Treats WHR / UN_DESA / loneliness / activity CSVs as point-in-time
    overlays — same value for every year, since the operator only drops
    one CSV. Honest: snapshot motion only reflects sources that actually
    moved.
  - For each year in [--start-year, this year - 1], builds subscore layers
    using only year-specific time-series values, runs `composite_from_layers`,
    and INSERT OR REPLACEs into snapshots.db with date = "YYYY-12-31".

Usage:
  python3 backfill.py --start-year 2010
  python3 backfill.py --start-year 2015 --dry-run     # show coverage, don't write
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import server
import snapshots as snapshots_module


log = logging.getLogger("backfill")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def filter_by_year(history: dict[str, dict[int, float]], year: int) -> dict[str, float]:
    out: dict[str, float] = {}
    for iso, by_year in history.items():
        if year in by_year:
            out[iso] = by_year[year]
    return out


def build_layers_for_year(year: int, histories: dict, static_layers: dict) -> dict:
    """Layers dict shaped like _build_subscore_layers but driven by `year`'s
    time-series slices, with static overlays for sources that aren't
    backfillable."""
    meta = static_layers["meta"]

    marriage_y = filter_by_year(histories["eurostat_marriage"], year)
    divorce_y  = filter_by_year(histories["eurostat_divorce"],  year)
    adolescent_y = filter_by_year(histories["wb_adolescent"], year)

    # Merge UN_DESA fallback (point-in-time) so non-EU countries still rank.
    marriage = server.merge_prefer_first(marriage_y, static_layers["un_marriage"])
    divorce  = server.merge_prefer_first(divorce_y,  static_layers["un_divorce"])

    # Subscore ranks within tier for THIS YEAR's marriage / divorce / fertility.
    partnership_pct = server.percentile_rank_within_tier(
        marriage, higher_is_better=True, cap_pct=server.PARTNERSHIP_CAP_PCT,
    )
    partnership_uncapped = server.percentile_rank_within_tier(marriage, higher_is_better=True)
    div_pct = server.percentile_rank_within_tier(divorce, higher_is_better=False)
    ado_pct = server.percentile_rank_within_tier(adolescent_y, higher_is_better=False)

    stability_pct: dict[str, float] = {}
    for iso in set(div_pct) | set(ado_pct):
        v = server.avg_present(div_pct.get(iso), ado_pct.get(iso))
        if v is not None:
            stability_pct[iso] = v

    return {
        "meta": meta,
        "subscores": {
            "connection":  static_layers["connection_pct"],
            "partnership": partnership_pct,
            "stability":   stability_pct,
            "activity":    static_layers["activity_pct"],
        },
        "extras": {"partnership_uncapped": partnership_uncapped},
        "raw": {
            "marriage_rate_per_1000":         marriage,
            "divorce_rate_per_1000":          divorce,
            "adolescent_fertility_per_1000":  adolescent_y,
            "whr_social_support_pct":         static_layers["whr_raw"],
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Seed snapshots.db with historical year-end rankings")
    parser.add_argument("--start-year", type=int, default=2010)
    parser.add_argument("--end-year",   type=int, default=datetime.utcnow().year - 1)
    parser.add_argument("--db", type=Path, default=server.SNAPSHOTS_DB)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log.info("backfill range: %d-%d -> %s", args.start_year, args.end_year, args.db)

    # Pull every history we can. These calls hit the live APIs once.
    log.info("fetching Eurostat marriage history…")
    eurostat_marriage = server.fetch_eurostat_crude_rate_history("demo_nind", "GMARRA")
    log.info("fetching Eurostat divorce history…")
    eurostat_divorce  = server.fetch_eurostat_crude_rate_history("demo_ndivind", "GDIVRT")
    log.info("fetching World Bank adolescent fertility history…")
    wb_adolescent     = server.fetch_wb_indicator_history("SP.ADO.TFRT")

    histories = {
        "eurostat_marriage": eurostat_marriage,
        "eurostat_divorce":  eurostat_divorce,
        "wb_adolescent":     wb_adolescent,
    }
    log.info("history coverage: eurostat_marriage=%d countries, eurostat_divorce=%d, wb_adolescent=%d",
             len(eurostat_marriage), len(eurostat_divorce), len(wb_adolescent))

    # Static overlays — same value across every backfill year (single-CSV feeds).
    log.info("loading static overlays (UN DESA, WHR, loneliness, activity)…")
    un_pair = server.fetch_un_marriage_divorce()
    un_marriage, un_divorce = un_pair if isinstance(un_pair, tuple) else ({}, {})
    whr_raw = server.fetch_whr_social_support()
    loneliness_inv = server.fetch_loneliness_data()
    activity_raw = server.fetch_activity_data()

    # Pre-compute the time-static subscore ranks (Connection + Activity) so we
    # don't recompute them once per year.
    whr_pct        = server.percentile_rank_within_tier(whr_raw, higher_is_better=True)
    loneliness_pct = server.percentile_rank_within_tier(loneliness_inv, higher_is_better=True)
    connection_pct: dict[str, float] = {}
    for iso in set(whr_pct) | set(loneliness_pct):
        v = server.avg_present(whr_pct.get(iso), loneliness_pct.get(iso))
        if v is not None:
            connection_pct[iso] = v
    activity_pct = server.percentile_rank_within_tier(activity_raw, higher_is_better=True)

    static_layers = {
        "meta":           server.get_country_meta(),
        "un_marriage":    un_marriage,
        "un_divorce":     un_divorce,
        "whr_raw":        whr_raw,
        "connection_pct": connection_pct,
        "activity_pct":   activity_pct,
    }

    written = 0
    for year in range(args.start_year, args.end_year + 1):
        layers = build_layers_for_year(year, histories, static_layers)
        countries = server.composite_from_layers(layers)
        ranked = [c for c in countries.values() if c.get("composite") is not None]
        if not ranked:
            log.warning("year %d: no ranked countries (skipping)", year)
            continue
        snap_date = f"{year}-12-31"
        if args.dry_run:
            log.info("year %d: would write %d snapshots (top: %s @ %.1f)",
                     year, len(ranked),
                     max(ranked, key=lambda c: c["composite"])["name"],
                     max(c["composite"] for c in ranked))
        else:
            n = snapshots_module.record_snapshot(ranked, args.db, snap_date=snap_date)
            log.info("year %d: wrote %d snapshots", year, n)
            written += n

    if args.dry_run:
        log.info("dry run complete; no writes")
    else:
        store = snapshots_module.n_snapshots(args.db)
        log.info("backfill complete; wrote %d rows; store now has %d dates / %d rows",
                 written, store["dates"], store["rows"])


if __name__ == "__main__":
    sys.exit(main())

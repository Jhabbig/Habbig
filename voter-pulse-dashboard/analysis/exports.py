"""CSV exports for the dashboard's underlying time-series.

Researchers and reporters want the raw data behind the gauge, not a
screenshot. This module turns the in-memory payloads into clean RFC-4180
CSV that fits in a spreadsheet — no auth required, no rate limit beyond
the response cache.

Three exports for now:

  mood    : monthly mood-index history (one row per month since 1978)
  life    : every FRED indicator in long format (series_id, date, value)
  states  : current state unemployment + 1y/4y pp deltas
"""

from __future__ import annotations

import csv
import io


def _csv_response(headers: list[str], rows: list[list[object]]) -> tuple[str, str]:
    """Return (csv_text, content_type) for a clean RFC-4180 dump."""
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    w.writerow(headers)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return buf.getvalue(), "text/csv; charset=utf-8"


def mood_history_csv(history: list[dict]) -> tuple[str, str]:
    """date, overall, misery_index — one row per month."""
    rows = [
        [h.get("date"), h.get("overall"), h.get("misery_index")]
        for h in (history or [])
    ]
    return _csv_response(["date", "mood_overall", "misery_index"], rows)


def life_long_csv(series_list: list[dict]) -> tuple[str, str]:
    """Long-format dump of every FRED indicator the dashboard tracks:
    series_id, label, units, date, value. Easy to pivot."""
    rows: list[list[object]] = []
    for s in series_list or []:
        sid = s.get("series_id")
        label = s.get("label")
        units = s.get("units")
        for p in (s.get("points") or []):
            rows.append([sid, label, units, p.get("date"), p.get("value")])
    return _csv_response(
        ["series_id", "label", "units", "date", "value"], rows
    )


def states_csv(states: list[dict]) -> tuple[str, str]:
    """postal, name, region, latest_date, unemployment_pct,
    delta_1y_pp, delta_4y_pp, mood_proxy."""
    rows = []
    for s in states or []:
        latest = s.get("latest") or {}
        rows.append([
            s.get("postal"),
            s.get("name"),
            s.get("region"),
            latest.get("date"),
            latest.get("value"),
            s.get("delta_1y_pp"),
            s.get("delta_4y_pp"),
            s.get("mood"),
        ])
    return _csv_response(
        ["postal", "name", "region", "latest_date",
         "unemployment_pct", "delta_1y_pp", "delta_4y_pp", "mood_proxy"],
        rows,
    )


def world_csv(countries: list[dict]) -> tuple[str, str]:
    """iso3, name, year, agri%, ind%, svc%, gdp_per_capita_usd,
    clark_fisher_bucket, clark_fisher_stage."""
    rows = []
    for c in countries or []:
        rows.append([
            c.get("iso3"),
            c.get("name"),
            c.get("year"),
            c.get("agriculture_pct"),
            c.get("industry_pct"),
            c.get("services_pct"),
            c.get("gdp_per_capita_usd"),
            c.get("bucket"),
            c.get("stage"),
        ])
    return _csv_response(
        ["iso3", "name", "year", "agriculture_pct", "industry_pct",
         "services_pct", "gdp_per_capita_usd",
         "clark_fisher_bucket", "clark_fisher_stage"],
        rows,
    )

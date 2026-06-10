"""FEMA OpenFEMA Disaster Declarations feed.

OpenFEMA exposes every major-disaster, emergency, and fire-management
declaration since 1953. Free, no key, JSON. We use it for two things:

  * ``recent_declarations(days)`` - what's been declared in the last N days.
    Useful for the "was Hurricane X officially declared?" question and as
    a feed in its own right.
  * ``ytd_count_projection(year)`` - count by declarationType so we can
    score Polymarket markets like "Will FEMA declare 50+ major disasters
    in 2026?".

Endpoint:
    https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries

OData-ish $filter syntax. ``$top=`` and ``$skip=`` for pagination.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

OPENFEMA_URL = "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"

# 2010-2024 mean for major-disaster declarations (DR) per year.
# Source: openfema-data-page tabulation. Used only as climo prior in the
# Poisson tail when no other signal is available.
DR_ANNUAL_CLIMO = 65


def _fetch(filter_clause: str, *, top: int = 1000, skip: int = 0) -> Optional[list[dict]]:
    params = {
        "$filter": filter_clause,
        "$top": str(top),
        "$skip": str(skip),
        "$select": "disasterNumber,declarationDate,declarationType,incidentType,state,declarationTitle,incidentBeginDate,incidentEndDate",
        "$orderby": "declarationDate desc",
    }
    r = http_get(OPENFEMA_URL, params=params, timeout=30)
    if not r:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return data.get("DisasterDeclarationsSummaries") or []


def recent_declarations(days: int = 30) -> dict:
    days = max(1, min(days, 365))
    cache_key = f"fema_recent_{days}"
    hit = _cache.get(cache_key, ttl_s=3600)  # 1 h
    if hit is not None:
        return hit
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    rows = _fetch(f"declarationDate ge '{cutoff}'", top=500)
    if rows is None:
        return {"error": "OpenFEMA fetch failed", "declarations": [], "count": 0}
    by_type: dict[str, int] = {}
    by_incident: dict[str, int] = {}
    by_state: dict[str, int] = {}
    seen_disasters: set = set()
    unique_rows: list[dict] = []
    for r in rows:
        # OpenFEMA emits one row per state-county for the same disasterNumber;
        # de-dupe at the disasterNumber level for the recent feed.
        dn = r.get("disasterNumber")
        if dn in seen_disasters:
            continue
        seen_disasters.add(dn)
        unique_rows.append(r)
        by_type[r.get("declarationType") or "?"] = by_type.get(r.get("declarationType") or "?", 0) + 1
        by_incident[r.get("incidentType") or "?"] = by_incident.get(r.get("incidentType") or "?", 0) + 1
        by_state[r.get("state") or "?"] = by_state.get(r.get("state") or "?", 0) + 1
    out = {
        "source": "OpenFEMA DisasterDeclarationsSummaries",
        "window_days": days,
        "count": len(unique_rows),
        "declarations": unique_rows[:80],
        "by_declaration_type": by_type,
        "by_incident_type": by_incident,
        "by_state": by_state,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


def ytd_count_projection(year: Optional[int] = None) -> dict:
    today = datetime.now(timezone.utc).date()
    if year is None:
        year = today.year
    cache_key = f"fema_ytd_{year}"
    hit = _cache.get(cache_key, ttl_s=3600)
    if hit is not None:
        return hit
    start = date(year, 1, 1).strftime("%Y-%m-%dT00:00:00Z")
    rows = _fetch(f"declarationDate ge '{start}'", top=2000)
    if rows is None:
        return {"error": "OpenFEMA fetch failed", "year": year}
    seen: set = set()
    dr_count = 0
    em_count = 0
    fm_count = 0
    by_incident: dict[str, int] = {}
    for r in rows:
        dn = r.get("disasterNumber")
        if dn in seen:
            continue
        seen.add(dn)
        dt = r.get("declarationType") or ""
        inc = r.get("incidentType") or "?"
        by_incident[inc] = by_incident.get(inc, 0) + 1
        if dt == "DR":
            dr_count += 1
        elif dt == "EM":
            em_count += 1
        elif dt == "FM":
            fm_count += 1
    days_into_year = (today - date(year, 1, 1)).days + 1
    days_in_year = 366 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365
    days_remaining = days_in_year - days_into_year
    rate_dr = dr_count / max(days_into_year, 1)
    proj_dr = dr_count + rate_dr * days_remaining
    out = {
        "source": "OpenFEMA YTD aggregation",
        "year": year,
        "as_of": today.isoformat(),
        "days_into_year": days_into_year,
        "days_remaining": days_remaining,
        "ytd_major_disasters_dr": dr_count,
        "ytd_emergency_em": em_count,
        "ytd_fire_management_fm": fm_count,
        "ytd_by_incident_type": by_incident,
        "rate_dr_per_day": round(rate_dr, 4),
        "lambda_dr_remaining": round(rate_dr * days_remaining, 2),
        "projected_year_end_dr_count": int(round(proj_dr)),
        "climo_dr_per_year": DR_ANNUAL_CLIMO,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(recent_declarations(days=30), indent=2)[:1500])
    print(json.dumps(ytd_count_projection(), indent=2))

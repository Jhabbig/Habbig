"""Build a GeoJSON FeatureCollection of every active threat for the map view.

Pulls each ingestion module's already-cached data and emits a unified
FeatureCollection keyed by category, with marker properties (severity,
magnitude, name, link) the front-end can render.

Categories emitted:
  - quake               (USGS recent + significant)
  - storm               (NHC + NRL ATCF)
  - tsunami             (NOAA tsunami unified - text-only, no geom)
  - wildfire_us         (NIFC active US incidents)
  - eonet_open          (NASA EONET wildfires + severeStorms + volcanoes + floods)
  - volcano             (Smithsonian GVP active list, where lat/lon known)
  - gdacs               (GDACS red/orange events with georss:point)

Features without a lat/lon are omitted (they show up in the table panels
instead).
"""
from __future__ import annotations

from datetime import datetime, timezone

from ingestion import (
    eonet_events,
    gdacs_alerts,
    jtwc_pacific,
    nhc_storms,
    nifc_fires,
    usgs_quakes,
    usgs_significant,
)


def _feature(lon: float, lat: float, props: dict) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
    }


def _safe_pt(lat, lon) -> bool:
    try:
        return -90 <= float(lat) <= 90 and -180 <= float(lon) <= 180
    except (TypeError, ValueError):
        return False


def build() -> dict:
    features: list[dict] = []

    # 1. Recent quakes M5+ (last 30d)
    recent = usgs_quakes.recent_quakes(min_magnitude=5.0, days=30)
    for q in (recent.get("quakes") or [])[:120]:
        if not _safe_pt(q.get("lat"), q.get("lon")):
            continue
        features.append(_feature(q["lon"], q["lat"], {
            "category": "quake",
            "mag": q.get("mag"),
            "label": f"M{q['mag']:.1f}" if q.get("mag") else "M?",
            "title": q.get("place") or "?",
            "time": q.get("time_iso"),
            "url": q.get("url"),
            "tsunami": q.get("tsunami"),
            "severity": "high" if (q.get("mag") or 0) >= 7 else "med" if (q.get("mag") or 0) >= 6 else "low",
        }))

    # 2. USGS significant (PAGER-coloured) - extra emphasis
    sig = usgs_significant.significant_recent("month")
    for q in (sig.get("events") or [])[:30]:
        if not _safe_pt(q.get("lat"), q.get("lon")):
            continue
        features.append(_feature(q["lon"], q["lat"], {
            "category": "quake_significant",
            "mag": q.get("mag"),
            "label": f"M{q['mag']:.1f}" if q.get("mag") else "M?",
            "title": q.get("place") or "?",
            "time": q.get("time_iso"),
            "url": q.get("url"),
            "alert": q.get("alert"),
            "felt": q.get("felt"),
            "severity": "critical" if q.get("alert") in ("red", "orange") else "high",
        }))

    # 3. Active named storms (NHC + NRL all-basins)
    nhc = nhc_storms.active_storms()
    for s in (nhc.get("storms") or []):
        if not _safe_pt(s.get("lat"), s.get("lon")):
            continue
        features.append(_feature(s["lon"], s["lat"], {
            "category": "storm",
            "label": s.get("name") or "?",
            "title": f"{s.get('name', '?')} - {s.get('classification', '?')}",
            "intensity_kt": s.get("intensity_kt"),
            "basin": s.get("basin"),
            "url": s.get("public_advisory_url"),
            "severity": "high",
        }))
    nrl = jtwc_pacific.active_storms_all_basins()
    for s in (nrl.get("storms") or []):
        if not _safe_pt(s.get("lat"), s.get("lon")):
            continue
        # Skip storms already surfaced by NHC (Atlantic + East Pac); NRL ATCF
        # uses 2-letter basin codes (AL/EP). Show only WP, IO, SH from NRL.
        if (s.get("basin") or "").upper() in {"AL", "EP"}:
            continue
        features.append(_feature(s["lon"], s["lat"], {
            "category": "storm",
            "label": s.get("name") or "?",
            "title": f"{s.get('name', '?')} - {s.get('classification', '?')} ({s.get('basin')})",
            "intensity_kt": s.get("intensity_kt"),
            "basin": s.get("basin"),
            "severity": "high",
        }))

    # 4. NIFC active US wildfires
    fires = nifc_fires.active_incidents()
    for f in (fires.get("incidents") or [])[:80]:
        if not _safe_pt(f.get("lat"), f.get("lon")):
            continue
        acres = f.get("daily_acres") or 0
        sev = "critical" if acres >= 50000 else "high" if acres >= 10000 else "med"
        features.append(_feature(f["lon"], f["lat"], {
            "category": "wildfire_us",
            "label": f.get("name") or "?",
            "title": f"{f.get('name', '?')} - {acres:,.0f} acres",
            "acres": acres,
            "containment": f.get("percent_contained"),
            "severity": sev,
        }))

    # 5. EONET open events (excluding earthquakes which we already have)
    eonet = eonet_events.open_events("all")
    for e in (eonet.get("events") or [])[:100]:
        if not _safe_pt(e.get("lat"), e.get("lon")):
            continue
        if "earthquakes" in (e.get("category_ids") or []):
            continue
        cats = e.get("categories") or []
        category_id = (e.get("category_ids") or ["other"])[0]
        features.append(_feature(e["lon"], e["lat"], {
            "category": "eonet_" + category_id,
            "label": (e.get("title") or "?")[:40],
            "title": e.get("title"),
            "categories": cats,
            "url": e.get("link"),
            "severity": "high",
        }))

    # 6. GDACS red/orange events
    gdacs = gdacs_alerts.active_events("Orange")
    for g in (gdacs.get("events") or [])[:60]:
        if not _safe_pt(g.get("lat"), g.get("lon")):
            continue
        sev = "critical" if g.get("alert_level") == "Red" else "high"
        features.append(_feature(g["lon"], g["lat"], {
            "category": "gdacs",
            "label": (g.get("title") or "?")[:40],
            "title": g.get("title"),
            "alert_level": g.get("alert_level"),
            "country": g.get("country"),
            "url": g.get("link"),
            "severity": sev,
        }))

    by_cat: dict[str, int] = {}
    for f in features:
        c = f["properties"]["category"]
        by_cat[c] = by_cat.get(c, 0) + 1

    return {
        "type": "FeatureCollection",
        "features": features,
        "by_category": by_cat,
        "count": len(features),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import json
    out = build()
    print(json.dumps({k: v for k, v in out.items() if k != "features"}, indent=2))
    print(f"first 3 features: {json.dumps(out['features'][:3], indent=2)}")

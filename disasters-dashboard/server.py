#!/usr/bin/env python3
"""Eco Disasters Dashboard — live ecological & environmental disaster feed.

Aggregates six free public feeds into one global timeline:

  - USGS earthquakes  — magnitude ≥ 2.5 in the last 24h (live JSON)
  - NASA EONET        — active natural events (wildfires, storms, volcanoes, ice, etc.)
  - GDACS             — global disaster alerts (RSS, multi-hazard)
  - NOAA NWS alerts   — US severe weather warnings + watches
  - NASA FIRMS        — active fire detections (VIIRS_SNPP_NRT, requires FIRMS_MAP_KEY)
  - ReliefWeb         — humanitarian disasters from OCHA (no key, polite appname)

Listens on :7060. Subdomain: disasters.narve.ai (registered in Habbig
gateway/config.json).
"""

from __future__ import annotations

import csv
import io
import logging
import math
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from flask import Flask, jsonify, send_from_directory

# ── Layered .env loader (matches the rest of the suite) ──────────────────────
try:
    from dotenv import load_dotenv as _dotenv_load
except ImportError:
    def _dotenv_load(p, override=False):
        for raw in Path(p).read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if not override and k in os.environ:
                continue
            os.environ[k] = v
        return True
_DASHBOARD_DIR = Path(__file__).resolve().parent
_GATEWAY_ENV = None
for _p in [_DASHBOARD_DIR, *_DASHBOARD_DIR.parents][:5]:
    _candidate = _p / "gateway" / ".env.production"
    if _candidate.is_file():
        _GATEWAY_ENV = _candidate
        break
_ENV_SEARCH = [Path.home() / ".gateway_env"]
if _GATEWAY_ENV is not None:
    _ENV_SEARCH.append(_GATEWAY_ENV)
_ENV_SEARCH.extend([_DASHBOARD_DIR / ".env.production", _DASHBOARD_DIR / ".env"])
_loaded_env_files: list[str] = []
for _f in _ENV_SEARCH:
    if _f.is_file():
        _dotenv_load(_f, override=False)
        _loaded_env_files.append(str(_f))
print(f"[disasters-dashboard] env files loaded: {len(_loaded_env_files)}", flush=True)
for _f in _loaded_env_files:
    print(f"  ✓ {_f}", flush=True)
if not os.getenv("GATEWAY_SSO_SECRET"):
    print("⚠ [disasters-dashboard] GATEWAY_SSO_SECRET missing — gateway-fronted requests will be rejected", flush=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("disasters")

app = Flask(__name__, static_folder="static")
try:
    from flask_compress import Compress
    Compress(app)
except ImportError:
    logger.warning("flask_compress not installed — responses won't be gzipped")

PORT = int(os.environ.get("PORT", "7060"))

# ── Cache (thread-safe, per-key TTL) ──────────────────────────────────────────
_cache: "OrderedDict[str, dict]" = OrderedDict()
_cache_lock = threading.Lock()
_TTL_DEFAULT = 30 * 60  # 30 min default per spec
_TTL = {
    "usgs":      5 * 60,    # USGS publishes every minute, 5 min cache is plenty
    "eonet":    15 * 60,    # NASA EONET refreshes hourly
    "gdacs":    10 * 60,    # GDACS RSS is multi-hour
    "nws":       5 * 60,    # NOAA NWS alerts are critical, fresher
    "firms":    30 * 60,    # FIRMS CSV is updated every few hours
    "reliefweb": 30 * 60,   # ReliefWeb disasters update slowly
    "summary":      60,     # in-memory summary
    "events:all":  120,     # unified feed
    "categories":  300,     # category list
}


def cache_get(key: str):
    with _cache_lock:
        e = _cache.get(key)
        if not e:
            return None
        if time.time() - e["t"] > _TTL.get(key, _TTL_DEFAULT):
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return e["data"]


def cache_set(key: str, data) -> None:
    with _cache_lock:
        _cache[key] = {"t": time.time(), "data": data}
        while len(_cache) > 64:
            _cache.popitem(last=False)


_USER_AGENT = "narve-disasters-dashboard/1.0 (+https://disasters.narve.ai)"
_HTTP_TIMEOUT = 10  # spec: all timeouts ≤10s

FIRMS_MAP_KEY = os.getenv("FIRMS_MAP_KEY", "").strip()


def _http_get(url, params=None, timeout=_HTTP_TIMEOUT):
    try:
        r = requests.get(url, params=params, timeout=timeout,
                         headers={"User-Agent": _USER_AGENT, "Accept": "application/json,*/*;q=0.5"})
        if r.status_code == 200:
            return r
        logger.warning("HTTP %d for %s", r.status_code, url)
        return None
    except Exception as e:
        logger.warning("HTTP error for %s: %s", url, e)
        return None


# ── USGS earthquakes ────────────────────────────────────────────────────────
USGS_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"


def fetch_earthquakes() -> Optional[dict]:
    cached = cache_get("usgs")
    if cached is not None:
        return cached
    r = _http_get(USGS_URL, timeout=10)
    if not r:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    events = []
    for f in data.get("features", []):
        props = f.get("properties", {})
        coords = (f.get("geometry") or {}).get("coordinates") or [None, None, None]
        events.append({
            "id": f.get("id"),
            "magnitude": props.get("mag"),
            "place": props.get("place"),
            "time": props.get("time"),  # ms unix
            "ts_utc": datetime.fromtimestamp(props["time"] / 1000, tz=timezone.utc).isoformat()
                      if props.get("time") else None,
            "lon": coords[0], "lat": coords[1], "depth_km": coords[2],
            "tsunami": bool(props.get("tsunami")),
            "url": props.get("url"),
            "alert": props.get("alert"),
        })
    events.sort(key=lambda e: e.get("time") or 0, reverse=True)
    out = {
        "source": "USGS Earthquake Hazards Program",
        "feed": "M2.5+ in last 24h",
        "count": len(events),
        "events": events,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set("usgs", out)
    return out


# ── NASA EONET ─────────────────────────────────────────────────────────────
EONET_URL = "https://eonet.gsfc.nasa.gov/api/v3/events"


def fetch_eonet() -> Optional[dict]:
    cached = cache_get("eonet")
    if cached is not None:
        return cached
    r = _http_get(EONET_URL, params={"status": "open", "limit": 80}, timeout=10)
    if not r:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    events = []
    for ev in data.get("events", []):
        cats = [c.get("title") for c in ev.get("categories", []) if c.get("title")]
        geos = ev.get("geometry") or []
        latest_geo = geos[-1] if geos else {}
        coord = latest_geo.get("coordinates")
        # EONET coords come as [lon, lat] for points, or polygon for areas — keep first point
        if isinstance(coord, list) and len(coord) >= 2 and isinstance(coord[0], (int, float)):
            lon, lat = coord[0], coord[1]
        elif isinstance(coord, list) and coord and isinstance(coord[0], list):
            # nested polygon
            pts = coord[0] if isinstance(coord[0][0], (int, float)) else coord[0][0]
            lon, lat = (pts[0], pts[1]) if isinstance(pts, list) and len(pts) >= 2 else (None, None)
        else:
            lon, lat = None, None
        events.append({
            "id": ev.get("id"),
            "title": ev.get("title"),
            "categories": cats,
            "lon": lon, "lat": lat,
            "date": latest_geo.get("date"),
            "url": (ev.get("link") or ((ev.get("sources") or [{}])[0].get("url"))),
        })
    out = {
        "source": "NASA EONET (Earth Observatory Natural Event Tracker)",
        "feed": "open events",
        "count": len(events),
        "events": events,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set("eonet", out)
    return out


# ── GDACS (RSS / XML) ───────────────────────────────────────────────────────
GDACS_URL = "https://www.gdacs.org/xml/rss.xml"


def fetch_gdacs() -> Optional[dict]:
    cached = cache_get("gdacs")
    if cached is not None:
        return cached
    r = _http_get(GDACS_URL, timeout=10)
    if not r:
        return None
    text = r.text
    # Lightweight parse — avoid xml deps; GDACS items follow a simple <item>…</item> shape.
    items = []
    for raw in re.findall(r"<item[^>]*>(.*?)</item>", text, flags=re.DOTALL):
        def grab(tag):
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", raw, flags=re.DOTALL)
            return (m.group(1).strip() if m else "")
        title = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", grab("title"), flags=re.DOTALL).strip()
        link = grab("link")
        pubdate = grab("pubDate")
        # gdacs:* fields
        m_alert = re.search(r"<gdacs:alertlevel[^>]*>(.*?)</gdacs:alertlevel>", raw)
        m_class = re.search(r"<gdacs:eventtype[^>]*>(.*?)</gdacs:eventtype>", raw)
        m_lat = re.search(r"<geo:lat[^>]*>(.*?)</geo:lat>", raw) or re.search(r"<gdacs:cap_latitude[^>]*>(.*?)</gdacs:cap_latitude>", raw)
        m_lon = re.search(r"<geo:long[^>]*>(.*?)</geo:long>", raw) or re.search(r"<gdacs:cap_longitude[^>]*>(.*?)</gdacs:cap_longitude>", raw)
        try:
            lat = float(m_lat.group(1)) if m_lat else None
            lon = float(m_lon.group(1)) if m_lon else None
        except Exception:
            lat, lon = None, None
        items.append({
            "title": title,
            "type": (m_class.group(1) if m_class else None),
            "alert_level": (m_alert.group(1) if m_alert else None),
            "lat": lat, "lon": lon,
            "url": link,
            "published": pubdate,
        })
    out = {
        "source": "GDACS (Global Disaster Alert and Coordination System)",
        "feed": "RSS active events",
        "count": len(items),
        "events": items,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set("gdacs", out)
    return out


# ── NOAA NWS US severe weather alerts ──────────────────────────────────────
NWS_URL = "https://api.weather.gov/alerts/active"


def fetch_nws_alerts() -> Optional[dict]:
    cached = cache_get("nws")
    if cached is not None:
        return cached
    r = _http_get(NWS_URL, params={"severity": "Severe,Extreme", "limit": 200}, timeout=10)
    if not r:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    events = []
    for f in data.get("features", []):
        p = f.get("properties", {}) or {}
        events.append({
            "id": p.get("id") or f.get("id"),
            "event": p.get("event"),
            "severity": p.get("severity"),
            "urgency": p.get("urgency"),
            "headline": p.get("headline"),
            "area": p.get("areaDesc"),
            "sent": p.get("sent"),
            "expires": p.get("expires"),
            "url": p.get("@id") or p.get("uri"),
        })
    out = {
        "source": "NOAA NWS (api.weather.gov)",
        "feed": "active severe/extreme alerts (US)",
        "count": len(events),
        "events": events,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set("nws", out)
    return out


# ── NASA FIRMS (active fires CSV) ──────────────────────────────────────────
# Requires a free MAP_KEY. Service degrades gracefully when absent.
FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
FIRMS_DATASET = "VIIRS_SNPP_NRT"


def fetch_firms() -> Optional[dict]:
    cached = cache_get("firms")
    if cached is not None:
        return cached
    if not FIRMS_MAP_KEY:
        # Graceful degradation — return an empty, well-formed payload.
        out = {
            "source": "NASA FIRMS (Fire Information for Resource Management System)",
            "feed": f"{FIRMS_DATASET} world / 1 day",
            "count": 0,
            "events": [],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "note": "FIRMS_MAP_KEY not set — fires feed disabled",
        }
        cache_set("firms", out)
        return out
    url = f"{FIRMS_BASE}/{FIRMS_MAP_KEY}/{FIRMS_DATASET}/world/1"
    r = _http_get(url, timeout=10)
    if not r:
        return None
    events: list[dict] = []
    try:
        reader = csv.DictReader(io.StringIO(r.text))
        for row in reader:
            try:
                lat = float(row.get("latitude") or 0)
                lon = float(row.get("longitude") or 0)
            except (TypeError, ValueError):
                continue
            acq_date = row.get("acq_date")
            acq_time = (row.get("acq_time") or "").zfill(4)
            ts = f"{acq_date}T{acq_time[:2]}:{acq_time[2:]}:00Z" if acq_date else None
            events.append({
                "id": f"firms-{acq_date}-{acq_time}-{lat:.4f}-{lon:.4f}",
                "lat": lat,
                "lon": lon,
                "bright_ti4": _safe_float(row.get("bright_ti4")),
                "bright_ti5": _safe_float(row.get("bright_ti5")),
                "frp": _safe_float(row.get("frp")),
                "confidence": row.get("confidence"),
                "daynight": row.get("daynight"),
                "satellite": row.get("satellite"),
                "instrument": row.get("instrument"),
                "ts_utc": ts,
            })
    except Exception as e:
        logger.warning("FIRMS CSV parse error: %s", e)
        return None
    out = {
        "source": "NASA FIRMS (Fire Information for Resource Management System)",
        "feed": f"{FIRMS_DATASET} world / 1 day",
        "count": len(events),
        "events": events,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set("firms", out)
    return out


def _safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── ReliefWeb (humanitarian disasters, OCHA) ──────────────────────────────
RELIEFWEB_URL = "https://api.reliefweb.int/v1/disasters"


def fetch_reliefweb() -> Optional[dict]:
    cached = cache_get("reliefweb")
    if cached is not None:
        return cached
    params = {
        "appname": "narve-ai",
        "filter[field]": "status",
        "filter[value]": "current",
        "limit": 50,
        "profile": "list",
    }
    r = _http_get(RELIEFWEB_URL, params=params, timeout=10)
    if not r:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    events: list[dict] = []
    for item in data.get("data", []):
        fields = item.get("fields", {}) or {}
        primary_country = (fields.get("primary_country") or {})
        country_name = primary_country.get("name")
        country_iso3 = primary_country.get("iso3")
        types = [t.get("name") for t in (fields.get("type") or []) if t.get("name")]
        events.append({
            "id": f"reliefweb-{item.get('id')}",
            "title": fields.get("name"),
            "description": (fields.get("description") or "")[:600],
            "status": fields.get("status"),
            "country": country_name,
            "country_iso3": country_iso3,
            "types": types,
            "url": fields.get("url") or item.get("href"),
            "date": (fields.get("date") or {}).get("created"),
        })
    out = {
        "source": "ReliefWeb (OCHA)",
        "feed": "current humanitarian disasters",
        "count": len(events),
        "events": events,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set("reliefweb", out)
    return out


# ── Unified normalization ─────────────────────────────────────────────────
# A single shape for /api/events, /api/categories, /api/by-region.
# Each item: {id, title, category, source, link, lat, lon, first_seen}

def _normalize_usgs(d: dict) -> list[dict]:
    items = []
    for e in d.get("events") or []:
        items.append({
            "id": f"usgs-{e.get('id')}",
            "title": (f"M{e.get('magnitude')} — {e.get('place')}"
                      if e.get("magnitude") is not None else (e.get("place") or "Earthquake")),
            "category": "Earthquake",
            "source": "USGS",
            "link": e.get("url"),
            "lat": e.get("lat"),
            "lon": e.get("lon"),
            "first_seen": e.get("ts_utc"),
            "severity": e.get("alert") or (
                "high" if (e.get("magnitude") or 0) >= 6 else
                "moderate" if (e.get("magnitude") or 0) >= 4.5 else "low"
            ),
        })
    return items


# EONET tags wildfire events as "Wildfires" (plural) — collapse to the
# canonical singular form used by GDACS + FIRMS so /api/categories doesn't
# split the same physical hazard across two buckets.
_EONET_CATEGORY_CANON = {
    "Wildfires": "Wildfire",
    "Severe Storms": "Storm",
    "Sea and Lake Ice": "Sea Ice",
}


def _normalize_eonet(d: dict) -> list[dict]:
    items = []
    for e in d.get("events") or []:
        raw_cat = (e.get("categories") or ["Unknown"])[0]
        items.append({
            "id": f"eonet-{e.get('id')}",
            "title": e.get("title"),
            "category": _EONET_CATEGORY_CANON.get(raw_cat, raw_cat),
            "source": "NASA EONET",
            "link": e.get("url"),
            "lat": e.get("lat"),
            "lon": e.get("lon"),
            "first_seen": e.get("date"),
        })
    return items


def _normalize_gdacs(d: dict) -> list[dict]:
    # GDACS event types: EQ, TC, FL, VO, DR, WF
    type_map = {"EQ": "Earthquake", "TC": "Tropical Cyclone", "FL": "Flood",
                "VO": "Volcano", "DR": "Drought", "WF": "Wildfire"}
    items = []
    for e in d.get("events") or []:
        t = (e.get("type") or "").upper()
        items.append({
            "id": f"gdacs-{e.get('url') or e.get('title')}",
            "title": e.get("title"),
            "category": type_map.get(t, t or "Disaster"),
            "source": "GDACS",
            "link": e.get("url"),
            "lat": e.get("lat"),
            "lon": e.get("lon"),
            "first_seen": e.get("published"),
            "severity": (e.get("alert_level") or "").lower() or None,
        })
    return items


def _normalize_firms(d: dict) -> list[dict]:
    items = []
    for e in d.get("events") or []:
        items.append({
            "id": e.get("id"),
            "title": f"Fire detection (FRP {e.get('frp')})" if e.get("frp") is not None else "Fire detection",
            "category": "Wildfire",
            "source": "NASA FIRMS",
            "link": "https://firms.modaps.eosdis.nasa.gov/map/",
            "lat": e.get("lat"),
            "lon": e.get("lon"),
            "first_seen": e.get("ts_utc"),
            "confidence": e.get("confidence"),
        })
    return items


def _normalize_reliefweb(d: dict) -> list[dict]:
    items = []
    for e in d.get("events") or []:
        items.append({
            "id": e.get("id"),
            "title": e.get("title"),
            "category": (e.get("types") or ["Humanitarian"])[0],
            "source": "ReliefWeb",
            "link": e.get("url"),
            "lat": None,
            "lon": None,
            "first_seen": e.get("date"),
            "country": e.get("country"),
        })
    return items


_NORMALIZERS = {
    "usgs":      (fetch_earthquakes, _normalize_usgs),
    "eonet":     (fetch_eonet,       _normalize_eonet),
    "gdacs":     (fetch_gdacs,       _normalize_gdacs),
    "firms":     (fetch_firms,       _normalize_firms),
    "reliefweb": (fetch_reliefweb,   _normalize_reliefweb),
}


def _collect_all_events() -> list[dict]:
    """Pull from every source, normalize, and concatenate. Failures are skipped."""
    out: list[dict] = []
    for name, (fetcher, normalize) in _NORMALIZERS.items():
        try:
            d = fetcher() or {}
            out.extend(normalize(d))
        except Exception as e:
            logger.warning("normalize %s failed: %s", name, e)
    return out


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ── Background warmup — refresh all 5 caches every 30 min in parallel ─────
_WARMUP_INTERVAL_S = 30 * 60
_warmup_thread: Optional[threading.Thread] = None


def _warmup_once() -> None:
    threads = []
    for name, (fetcher, _norm) in _NORMALIZERS.items():
        def _run(fn=fetcher, n=name):
            try:
                fn()
                logger.info("warmup ok: %s", n)
            except Exception as e:
                logger.warning("warmup %s failed: %s", n, e)
        t = threading.Thread(target=_run, name=f"warmup-{name}", daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=12)


def _warmup_loop() -> None:
    while True:
        try:
            _warmup_once()
        except Exception as e:
            logger.warning("warmup cycle failed: %s", e)
        time.sleep(_WARMUP_INTERVAL_S)


def start_warmup_thread() -> None:
    global _warmup_thread
    if _warmup_thread is not None and _warmup_thread.is_alive():
        return
    _warmup_thread = threading.Thread(target=_warmup_loop, name="warmup", daemon=True)
    _warmup_thread.start()
    logger.info("warmup thread started (every %ds)", _WARMUP_INTERVAL_S)


# ── Routes ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "service": "disasters-dashboard",
        "ts": time.time(),
        "env_files_loaded": _loaded_env_files,
    })


@app.route("/api/earthquakes")
def api_earthquakes():
    d = fetch_earthquakes()
    return (jsonify(d) if d else (jsonify({"error": "USGS fetch failed"}), 503))


@app.route("/api/eonet")
def api_eonet():
    d = fetch_eonet()
    return (jsonify(d) if d else (jsonify({"error": "EONET fetch failed"}), 503))


@app.route("/api/gdacs")
def api_gdacs():
    d = fetch_gdacs()
    return (jsonify(d) if d else (jsonify({"error": "GDACS fetch failed"}), 503))


@app.route("/api/nws")
def api_nws():
    d = fetch_nws_alerts()
    return (jsonify(d) if d else (jsonify({"error": "NWS fetch failed"}), 503))


@app.route("/api/firms")
def api_firms():
    d = fetch_firms()
    return (jsonify(d) if d else (jsonify({"error": "FIRMS fetch failed"}), 503))


@app.route("/api/reliefweb")
def api_reliefweb():
    d = fetch_reliefweb()
    return (jsonify(d) if d else (jsonify({"error": "ReliefWeb fetch failed"}), 503))


# ── Unified endpoints ─────────────────────────────────────────────────────
from flask import request as _request  # local import keeps the route block tidy


_SOURCE_ALIAS = {
    "eonet":     "eonet",
    "usgs":      "usgs",
    "gdacs":     "gdacs",
    "firms":     "firms",
    "fires":     "firms",
    "reliefweb": "reliefweb",
    "nws":       None,  # not yet in unified shape; kept on /api/nws
}


@app.route("/api/events")
def api_events():
    """Unified live feed across all sources.

    Query params:
      source: eonet | usgs | gdacs | firms | reliefweb | all (default: all)
      limit:  integer cap on returned events (default: 200)
    """
    source = (_request.args.get("source") or "all").strip().lower()
    try:
        limit = max(1, min(int(_request.args.get("limit", "200")), 1000))
    except ValueError:
        limit = 200

    if source == "all":
        items = _collect_all_events()
    else:
        key = _SOURCE_ALIAS.get(source)
        if not key or key not in _NORMALIZERS:
            return jsonify({"error": f"unknown source '{source}'",
                            "allowed": sorted([k for k, v in _SOURCE_ALIAS.items() if v])}), 400
        fetcher, normalize = _NORMALIZERS[key]
        try:
            items = normalize(fetcher() or {})
        except Exception as e:
            logger.warning("api_events %s failed: %s", key, e)
            items = []

    # newest first when first_seen is present
    items.sort(key=lambda e: e.get("first_seen") or "", reverse=True)
    items = items[:limit]
    return jsonify({
        "source": source,
        "count": len(items),
        "events": items,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/categories")
def api_categories():
    """Distinct categories surfaced from currently cached events."""
    items = _collect_all_events()
    counts: dict[str, int] = {}
    for e in items:
        c = e.get("category") or "Unknown"
        counts[c] = counts.get(c, 0) + 1
    ordered = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
    return jsonify({
        "count": len(ordered),
        "categories": ordered,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/sources")
def api_sources():
    """Per-source health snapshot — last-fetched timestamp, event count, status."""
    out = []
    for name, (fetcher, _norm) in _NORMALIZERS.items():
        try:
            d = fetcher() or {}
            out.append({
                "source": name,
                "ok": bool(d),
                "count": d.get("count", 0),
                "fetched_at": d.get("fetched_at"),
                "note": d.get("note"),
            })
        except Exception as e:
            out.append({"source": name, "ok": False, "error": str(e)})
    return jsonify({
        "sources": out,
        "firms_key_present": bool(FIRMS_MAP_KEY),
        "ts": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/by-region")
def api_by_region():
    """Geo-filtered events within radius_km of (lat, lng)."""
    try:
        lat = float(_request.args.get("lat"))
        lon = float(_request.args.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"error": "lat and lng query params are required (floats)"}), 400
    try:
        radius_km = float(_request.args.get("radius_km", "500"))
    except ValueError:
        radius_km = 500.0
    radius_km = max(1.0, min(radius_km, 20000.0))

    items = _collect_all_events()
    nearby = []
    for e in items:
        elat, elon = e.get("lat"), e.get("lon")
        if elat is None or elon is None:
            continue
        try:
            d = _haversine_km(lat, lon, float(elat), float(elon))
        except (TypeError, ValueError):
            continue
        if d <= radius_km:
            nearby.append({**e, "distance_km": round(d, 2)})
    nearby.sort(key=lambda e: e.get("distance_km") or 0)
    return jsonify({
        "lat": lat, "lng": lon, "radius_km": radius_km,
        "count": len(nearby),
        "events": nearby,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/summary")
def api_summary():
    """Single endpoint giving the front page everything in one shot."""
    eq = fetch_earthquakes() or {}
    eo = fetch_eonet() or {}
    gd = fetch_gdacs() or {}
    nws = fetch_nws_alerts() or {}
    firms = fetch_firms() or {}
    rw = fetch_reliefweb() or {}
    # Highlights for the headline cards
    biggest_quake = max(((eq.get("events") or [])), key=lambda e: e.get("magnitude") or 0, default=None)
    severe_alerts = [a for a in (nws.get("events") or []) if a.get("severity") == "Extreme"][:5]
    return jsonify({
        "earthquakes": {
            "count": eq.get("count", 0),
            "feed": eq.get("feed"),
            "biggest_24h": biggest_quake,
            "tsunami_warnings": [e for e in (eq.get("events") or []) if e.get("tsunami")][:5],
        },
        "eonet": {
            "count": eo.get("count", 0),
            "by_category": _bucket(eo.get("events") or [], lambda e: (e.get("categories") or ["Other"])[0]),
        },
        "gdacs": {
            "count": gd.get("count", 0),
            "by_alert_level": _bucket(gd.get("events") or [], lambda e: (e.get("alert_level") or "Green").capitalize()),
        },
        "nws": {
            "count": nws.get("count", 0),
            "extreme": severe_alerts,
        },
        "firms": {
            "count": firms.get("count", 0),
            "note": firms.get("note"),
        },
        "reliefweb": {
            "count": rw.get("count", 0),
            "by_country": _bucket(rw.get("events") or [], lambda e: e.get("country") or "Unknown"),
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


def _bucket(items, key_fn):
    out = {}
    for item in items:
        k = key_fn(item) or "Other"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# ── Main ────────────────────────────────────────────────────────────────────
# Kick off the warmup thread on import so gunicorn workers also pre-fill caches.
if os.getenv("DISASTERS_DISABLE_WARMUP") != "1":
    try:
        start_warmup_thread()
    except Exception as e:
        logger.warning("could not start warmup thread: %s", e)


if __name__ == "__main__":
    logger.info("Starting disasters dashboard on :%d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

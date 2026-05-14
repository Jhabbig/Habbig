#!/usr/bin/env python3
"""Eco Disasters Dashboard — live ecological & environmental disaster feed.

Aggregates four free, no-auth public feeds into one global timeline:

  - USGS earthquakes  — magnitude ≥ 2.5 in the last 24h (live JSON)
  - NASA EONET        — active natural events (wildfires, storms, volcanoes, ice, etc.)
  - GDACS             — global disaster alerts (RSS, multi-hazard)
  - NOAA NWS alerts   — US severe weather warnings + watches

ReliefWeb and NASA FIRMS are noted as future feeds — they require API keys
or have rate-limit subtleties that would gate the v1 ship.

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
_TTL_DEFAULT = 5 * 60
_TTL = {
    "usgs":     5 * 60,    # USGS publishes every minute, 5 min cache is plenty
    "eonet":    15 * 60,   # NASA EONET refreshes hourly
    "gdacs":    10 * 60,   # GDACS RSS is multi-hour
    "nws":      5 * 60,    # NOAA NWS alerts are critical, fresher
    "summary":  60,        # in-memory summary
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
_HTTP_TIMEOUT = 20


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
    r = _http_get(USGS_URL, timeout=20)
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
    r = _http_get(EONET_URL, params={"status": "open", "limit": 80}, timeout=20)
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
    r = _http_get(GDACS_URL, timeout=20)
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
    r = _http_get(NWS_URL, params={"severity": "Severe,Extreme", "limit": 200}, timeout=20)
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


@app.route("/api/summary")
def api_summary():
    """Single endpoint giving the front page everything in one shot."""
    eq = fetch_earthquakes() or {}
    eo = fetch_eonet() or {}
    gd = fetch_gdacs() or {}
    nws = fetch_nws_alerts() or {}
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
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


def _bucket(items, key_fn):
    out = {}
    for item in items:
        k = key_fn(item) or "Other"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# ── Main ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("Starting disasters dashboard on :%d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

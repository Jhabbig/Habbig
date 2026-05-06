#!/usr/bin/env python3
"""Religion & Cults Tracker — Flask backend.

Tracks the global religious landscape and a curated watchlist of new
religious movements (NRMs) and notable cults, with live signal from the
Polymarket religion-tagged markets and public news RSS feeds.

Endpoints:
  GET /                  → static index.html
  GET /api/health        → liveness
  GET /api/summary       → page-load payload (totals + freedom counts)
  GET /api/religions     → world religions adherent counts + sub-traditions
  GET /api/cults         → curated NRM / cult watchlist
  GET /api/freedom       → USCIRF designations (CPC / SWL / EPC)
  GET /api/markets       → Polymarket religion-related markets (live)
  GET /api/news          → aggregated religion news (RSS, live)

The world-religion and cult datasets are static and curated — see
religion_data.py for sourcing notes. Markets and news are fetched live
with TTL caching (5 min for markets, 10 min for news).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from defusedxml import ElementTree as DET
from flask import Flask, jsonify, request, send_from_directory

import religion_data as rd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("religion")

app = Flask(__name__, static_folder="static")

try:
    from flask_compress import Compress
    Compress(app)
except Exception:
    log.warning("flask_compress not available; responses will not be gzipped")

PORT = int(os.environ.get("PORT", "7062"))
HTML_PATH = Path(__file__).parent / "index.html"

# ─── Cache ────────────────────────────────────────────────────────────────────

_cache: "OrderedDict[str, dict]" = OrderedDict()
_cache_lock = threading.Lock()

_TTL_DEFAULT = 60 * 60
_TTL = {
    "polymarket": 60 * 5,
    "news":       60 * 10,
}


def cache_get(key: str):
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        ttl = _TTL.get(key, _TTL_DEFAULT)
        if time.time() - entry["t"] > ttl:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return entry["data"]


def cache_set(key: str, data) -> None:
    with _cache_lock:
        _cache[key] = {"t": time.time(), "data": data}
        while len(_cache) > 32:
            _cache.popitem(last=False)


# ─── HTTP helper ──────────────────────────────────────────────────────────────

_USER_AGENT = "religion-dashboard/1.0 (+https://religion.narve.ai)"


def _http_get(url: str, *, timeout: int = 12, params: Optional[dict] = None) -> Optional[requests.Response]:
    try:
        r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": _USER_AGENT})
        if r.status_code == 200:
            return r
        log.warning("HTTP %d for %s", r.status_code, url)
        return None
    except Exception as e:
        log.warning("HTTP error for %s: %s", url, e)
        return None


# ─── Polymarket fetcher ───────────────────────────────────────────────────────

GAMMA_BASE = "https://gamma-api.polymarket.com"


def _fetch_events_by_tag(tag_slug: str, seen: set, out: list, lock: threading.Lock) -> None:
    offset = 0
    for _ in range(6):
        r = _http_get(
            f"{GAMMA_BASE}/events",
            params={"tag_slug": tag_slug, "closed": "false", "limit": "100", "offset": str(offset)},
        )
        if not r:
            break
        try:
            events = r.json()
        except Exception:
            break
        if not events:
            break
        for ev in events:
            title = (ev.get("title") or "")
            tl = title.lower()
            if any(k in tl for k in rd.POLYMARKET_REJECT):
                continue
            tags = ev.get("tags", [])
            tag_labels = [t.get("label", "") for t in tags if isinstance(t, dict)]
            for m in ev.get("markets", []):
                mid = m.get("conditionId") or m.get("id") or ""
                if not mid:
                    continue
                with lock:
                    if mid in seen:
                        continue
                    seen.add(mid)
                    m["_event_title"] = title
                    m["_event_tags"] = tag_labels
                    out.append(m)
        offset += 100


def _safe_float(x) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _normalize_market(m: dict) -> Optional[dict]:
    """Trim a Polymarket market record to the fields we render."""
    question = (m.get("question") or m.get("_event_title") or "").strip()
    if not question:
        return None
    # Pull the YES side price. Polymarket exposes this as outcomePrices ('["0.30","0.70"]')
    # or lastTradePrice, depending on the endpoint version. Try both.
    yes_price = None
    op = m.get("outcomePrices")
    if isinstance(op, list) and op:
        yes_price = _safe_float(op[0])
    elif isinstance(op, str) and op.startswith("["):
        try:
            import json as _json
            arr = _json.loads(op)
            if arr:
                yes_price = _safe_float(arr[0])
        except Exception:
            pass
    if yes_price is None:
        yes_price = _safe_float(m.get("lastTradePrice"))
    volume = _safe_float(m.get("volumeNum")) or _safe_float(m.get("volume")) or 0.0
    liquidity = _safe_float(m.get("liquidityNum")) or _safe_float(m.get("liquidity")) or 0.0
    end_iso = m.get("endDate") or m.get("endDateIso") or ""
    slug = m.get("slug") or ""
    return {
        "id": m.get("conditionId") or m.get("id"),
        "question": question,
        "event_title": m.get("_event_title", ""),
        "yes_price": yes_price,
        "volume": volume,
        "liquidity": liquidity,
        "end_date": end_iso,
        "url": f"https://polymarket.com/event/{slug}" if slug else "",
        "tags": m.get("_event_tags", []),
    }


def fetch_markets() -> list[dict]:
    cached = cache_get("polymarket")
    if cached is not None:
        return cached
    seen: set = set()
    raw: list = []
    lock = threading.Lock()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_fetch_events_by_tag, slug, seen, raw, lock) for slug in rd.POLYMARKET_TAG_SLUGS]
        for f in futures:
            try:
                f.result()
            except Exception as e:
                log.warning("tag fetch error: %s", e)

    # Strict keyword filter — Polymarket's tag set is noisy and bleeds into
    # politics + crypto. Require a religion-keyword in either the market
    # question or the parent event title.
    out: list[dict] = []
    for m in raw:
        question = (m.get("question") or "")
        ev_title = (m.get("_event_title") or "")
        blob = (question + " " + ev_title).lower()
        if not any(k in blob for k in rd.POLYMARKET_KEYWORDS):
            continue
        norm = _normalize_market(m)
        if norm:
            out.append(norm)

    out.sort(key=lambda x: x.get("volume") or 0, reverse=True)
    cache_set("polymarket", out)
    return out


# ─── News RSS fetcher ────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    return _TAG_RE.sub("", s or "").strip()


def _parse_rss(xml: str, source: str) -> list[dict]:
    out: list[dict] = []
    try:
        root = DET.fromstring(xml)
    except Exception as e:
        log.warning("RSS parse failed for %s: %s", source, e)
        return out

    # Strip namespaces from tags so simple .find() works regardless of prefix.
    def _local(tag: str) -> str:
        return tag.split("}", 1)[1] if "}" in tag else tag

    # Walk RSS 2.0 (channel/item) and Atom (feed/entry).
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        title = ""
        link = ""
        published = ""
        summary = ""
        for child in el:
            t = _local(child.tag)
            text = (child.text or "").strip()
            if t == "title" and not title:
                title = text
            elif t == "link" and not link:
                link = text or child.attrib.get("href", "")
            elif t in ("pubDate", "published", "updated", "date") and not published:
                published = text
            elif t in ("description", "summary", "content") and not summary:
                summary = _strip_html(text)
        if not title or not link:
            continue
        out.append({
            "source": source,
            "title": title,
            "link": link,
            "published": published,
            "summary": (summary[:280] + "…") if len(summary) > 280 else summary,
        })
    return out


def fetch_news() -> list[dict]:
    cached = cache_get("news")
    if cached is not None:
        return cached
    items: list[dict] = []
    from concurrent.futures import ThreadPoolExecutor

    def _one(name: str, url: str) -> list[dict]:
        r = _http_get(url, timeout=10)
        if not r:
            return []
        return _parse_rss(r.text, name)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_one, name, url) for name, url in rd.NEWS_RSS_FEEDS]
        for f in futures:
            try:
                items.extend(f.result())
            except Exception as e:
                log.warning("news fetch error: %s", e)

    # De-duplicate by link.
    seen: set = set()
    deduped: list[dict] = []
    for it in items:
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        deduped.append(it)

    # Sort newest-first when we can parse the date.
    def _ts(it: dict) -> float:
        p = it.get("published") or ""
        for fmt in (
            "%a, %d %b %Y %H:%M:%S %z",
            "%a, %d %b %Y %H:%M:%S %Z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%SZ",
        ):
            try:
                return datetime.strptime(p, fmt).timestamp()
            except ValueError:
                continue
        return 0.0

    deduped.sort(key=_ts, reverse=True)
    deduped = deduped[:60]
    cache_set("news", deduped)
    return deduped


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if not HTML_PATH.exists():
        return ("index.html missing", 500)
    return HTML_PATH.read_text(encoding="utf-8"), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/static/<path:path>")
def static_files(path):
    return send_from_directory(app.static_folder, path)


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "service": "religion-dashboard"})


@app.route("/api/religions")
def api_religions():
    return jsonify({
        "fetched_at": int(time.time()),
        "source": "Pew Research Center, baseline 2020 estimates",
        "religions": rd.WORLD_RELIGIONS,
        "subgroups": rd.RELIGION_SUBGROUPS,
    })


@app.route("/api/cults")
def api_cults():
    status = (request.args.get("status") or "").strip().lower()
    risk = (request.args.get("risk") or "").strip().lower()
    items = list(rd.CULTS_WATCHLIST)
    if status:
        items = [c for c in items if status in (c.get("status") or "").lower()]
    if risk:
        items = [c for c in items if (c.get("risk") or "").lower() == risk]
    return jsonify({
        "fetched_at": int(time.time()),
        "count": len(items),
        "groups": items,
    })


@app.route("/api/freedom")
def api_freedom():
    return jsonify({
        "fetched_at": int(time.time()),
        "source": "USCIRF Annual Report 2024",
        "designations": rd.USCIRF_2024,
        "counts": {
            "cpc": len(rd.USCIRF_2024["cpc"]),
            "swl": len(rd.USCIRF_2024["swl"]),
            "epc": len(rd.USCIRF_2024["epc"]),
        },
    })


@app.route("/api/markets")
def api_markets():
    markets = fetch_markets()
    return jsonify({
        "fetched_at": int(time.time()),
        "count": len(markets),
        "markets": markets,
    })


@app.route("/api/news")
def api_news():
    items = fetch_news()
    return jsonify({
        "fetched_at": int(time.time()),
        "count": len(items),
        "items": items,
    })


@app.route("/api/summary")
def api_summary():
    """Single payload for the page header — totals only, no live calls."""
    total_m = sum(r["adherents_m"] for r in rd.WORLD_RELIGIONS)
    cults = rd.CULTS_WATCHLIST
    return jsonify({
        "fetched_at": int(time.time()),
        "world_population_m": total_m,
        "religions_tracked": len(rd.WORLD_RELIGIONS),
        "cults_tracked": len(cults),
        "cults_active": sum(1 for c in cults if "active" in (c.get("status") or "").lower()),
        "cults_defunct": sum(1 for c in cults if (c.get("status") or "").lower() == "defunct"),
        "cpc_countries": len(rd.USCIRF_2024["cpc"]),
        "swl_countries": len(rd.USCIRF_2024["swl"]),
        "epc_entities": len(rd.USCIRF_2024["epc"]),
    })


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("religion-dashboard listening on :%d", PORT)
    app.run(host=os.environ.get("BIND_HOST", "0.0.0.0"), port=PORT, threaded=True)

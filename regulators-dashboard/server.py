#!/usr/bin/env python3
"""Regulators Dashboard — FastAPI backend.

Routes:
  - GET /                       → index.html
  - GET /api/feed?…             → unified action feed + market matches (filters: days, jurisdiction, source, tag, severity, topic, has_market, q)
  - GET /api/heatmap?weeks=12   → per-week, per-regulator, per-tag aggregation
  - GET /api/topics?days=90     → per-topic counts (drives the topic-filter chip badges)
  - GET /api/markets            → raw Polymarket + Kalshi market list (debug)
  - GET /api/people             → hand-curated personnel watch with term-end days + matched markets
  - GET /api/stance             → per-regulator speech stance ladder (SEC/FCA/ESMA axes)
  - GET /healthz                → liveness

Auth: same gateway-SSO pattern as centralbank-dashboard. Set DEV_MODE=1 to
bypass when running locally.
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ingestion import kalshi_client, polymarket_client, unified_feed
from analysis import heatmap as heatmap_aggr
from analysis import market_match
from analysis import people as people_roster
from analysis import stance as stance_analysis
from analysis.topic_keywords import TOPICS, TOPIC_LABELS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="Regulators Dashboard")

HTML_PATH = Path(__file__).parent / "index.html"

_sso_secret = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"
if not _sso_secret and not _DEV_MODE:
    log.warning("GATEWAY_SSO_SECRET unset and DEV_MODE off — all requests will 503")


@app.middleware("http")
async def security_and_auth(request: Request, call_next):
    if request.url.path != "/healthz":
        if _sso_secret:
            client_secret = request.headers.get("x-gateway-secret", "")
            if not hmac.compare_digest(client_secret, _sso_secret):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
        elif not _DEV_MODE:
            return JSONResponse({"error": "Service misconfigured"}, status_code=503)

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    if _sso_secret:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))


@app.get("/api/feed")
async def api_feed(
    days: int = 90,
    jurisdiction: str = "",
    source: str = "",
    tag: str = "",
    severity: str = "",
    topic: str = "",
    has_market: bool = False,
    q: str = "",
    force: bool = False,
) -> JSONResponse:
    days = max(1, min(days, 365))
    data = unified_feed.get_cached(force=force, since_days=days)

    items = data["items"]
    if jurisdiction:
        wanted = {j.strip().upper() for j in jurisdiction.split(",") if j.strip()}
        items = [it for it in items if it.get("jurisdiction") in wanted]
    if source:
        wanted = {s.strip().upper() for s in source.split(",") if s.strip()}
        items = [it for it in items if it.get("source") in wanted]
    if tag:
        wanted = {t.strip().lower() for t in tag.split(",") if t.strip()}
        # Match if primary_tag is wanted, or any element of tags is wanted.
        # 'other' matches items with no positive tags.
        def tag_hit(it: dict) -> bool:
            if "other" in wanted and not it.get("tags"):
                return True
            if it.get("primary_tag") in wanted:
                return True
            return any(t in wanted for t in it.get("tags", []))
        items = [it for it in items if tag_hit(it)]
    if severity:
        wanted = {s.strip().lower() for s in severity.split(",") if s.strip()}
        def sev_hit(it: dict) -> bool:
            sev = it.get("severity")
            bucket = sev["bucket"] if sev else "none"
            return bucket in wanted
        items = [it for it in items if sev_hit(it)]
    if topic:
        wanted = {t.strip().lower() for t in topic.split(",") if t.strip()}
        items = [it for it in items if any(t in wanted for t in it.get("topics", []))]
    if q:
        needle = q.lower().strip()
        if needle:
            items = [
                it for it in items
                if needle in it.get("title", "").lower()
                or needle in it.get("summary", "").lower()
            ]

    # v0.5: attach market matches per item (5-min market cache, in-memory
    # join). Use a fresh shallow-copy list so the cached unified_feed items
    # aren't mutated across requests.
    poly = polymarket_client.get_cached()
    kal = kalshi_client.get_cached()
    all_markets = poly["markets"] + kal["markets"]
    items = market_match.attach_matches(items, all_markets)

    if has_market:
        items = [it for it in items if it.get("markets")]

    return JSONResponse({
        "fetched_at": data["fetched_at"],
        "since_days": data["since_days"],
        "sources": data["sources"],
        "market_sources": [
            {"name": "polymarket", "ok": poly["ok"], "count": poly["count"], "error": poly["error"]},
            {"name": "kalshi",     "ok": kal["ok"],  "count": kal["count"],  "error": kal["error"]},
        ],
        "items": items,
        "count": len(items),
    })


@app.get("/api/heatmap")
async def api_heatmap(weeks: int = 12, force: bool = False) -> JSONResponse:
    weeks = max(4, min(weeks, 52))
    data = unified_feed.get_cached(force=force, since_days=max(90, weeks * 7))
    return JSONResponse(heatmap_aggr.aggregate(data["items"], data["sources"], weeks=weeks))


@app.get("/api/markets")
async def api_markets(force: bool = False) -> JSONResponse:
    """Raw market list for debugging the join. Returns the combined
    Polymarket + Kalshi normalized markets that the matcher sees."""
    poly = polymarket_client.get_cached(force=force)
    kal = kalshi_client.get_cached(force=force)
    return JSONResponse({
        "polymarket": poly,
        "kalshi": kal,
        "combined_count": poly["count"] + kal["count"],
    })


@app.get("/api/stance")
async def api_stance(force: bool = False) -> JSONResponse:
    """Per-regulator stance ladder from the most recent speech-tagged item
    for each body. Uses the cached feed — no extra fetch cost."""
    data = unified_feed.get_cached(force=force)
    return JSONResponse({
        "fetched_at": data["fetched_at"],
        "ladder": stance_analysis.compute(data["items"]),
    })


@app.get("/api/people")
async def api_people() -> JSONResponse:
    """Hand-curated personnel watch with days-until-term-end + matched markets.
    Roster source is `data/personnel.py` — edit there to add or refresh entries.
    """
    rows = people_roster.roster()
    poly = polymarket_client.get_cached()
    kal = kalshi_client.get_cached()
    all_markets = poly["markets"] + kal["markets"]
    prepared = market_match.prepare_markets(all_markets)
    for r in rows:
        synthetic = people_roster.synthetic_item_for(r)
        r["markets"] = market_match.match_for_item(synthetic, prepared)
    return JSONResponse({
        "people": rows,
        "market_sources": [
            {"name": "polymarket", "ok": poly["ok"], "count": poly["count"], "error": poly["error"]},
            {"name": "kalshi",     "ok": kal["ok"],  "count": kal["count"],  "error": kal["error"]},
        ],
    })


@app.get("/api/topics")
async def api_topics(days: int = 90, force: bool = False) -> JSONResponse:
    """Per-topic action counts over the cached feed window. Drives the
    topic-filter chip badges; not the individual-item view (that's /api/feed).
    """
    days = max(1, min(days, 365))
    data = unified_feed.get_cached(force=force, since_days=days)
    counts: dict[str, int] = {key: 0 for key in TOPICS.keys()}
    for it in data["items"]:
        for t in it.get("topics", []):
            if t in counts:
                counts[t] += 1
    return JSONResponse({
        "fetched_at": data["fetched_at"],
        "since_days": data["since_days"],
        "total_items": len(data["items"]),
        "topics": [
            {"key": key, "label": TOPIC_LABELS.get(key, key), "count": counts[key]}
            for key in TOPICS.keys()
        ],
    })


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("BIND_HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "7080")))

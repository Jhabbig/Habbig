#!/usr/bin/env python3
"""Love Atlas — FastAPI backend.

Macro relationship-metric atlas: marriage rates, divorce rates, median
age at first marriage, cohabitation share, total fertility rate,
dating-app DAU, sexless-marriage survey signals, loneliness trends.

Sits on port 7062 behind the narve.ai gateway as love.narve.ai.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

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
_GATEWAY_ENV: Optional[Path] = None
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
print(f"[love-dashboard] env files loaded: {len(_loaded_env_files)}", flush=True)
for _f in _loaded_env_files:
    print(f"  loaded {_f}", flush=True)
if not os.getenv("GATEWAY_SSO_SECRET"):
    print("[love-dashboard] GATEWAY_SSO_SECRET missing", flush=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("love")

# ── Observability — init Sentry BEFORE FastAPI is constructed so the SDK can
# instrument it. Fail-soft: if sentry-sdk is missing or no DSN is configured,
# init_sentry() logs and continues.
import observability as _observability  # noqa: E402
_observability.init_sentry(platform="love")

PORT = int(os.environ.get("PORT", "7062"))
DATA_DIR = _DASHBOARD_DIR / "data"
STATIC_DIR = _DASHBOARD_DIR / "static"
SCHEMA_PATH = _DASHBOARD_DIR / "schema.sql"
DB_PATH = _DASHBOARD_DIR / "love.sqlite"

app = FastAPI(
    title="Love Atlas",
    description="Macro relationship metrics",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(.*\.)?(narve\.ai|habbig\.com|localhost(:\d+)?)$",
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=True,
)
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_SSO_SECRET = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"
_AUTH_BYPASS_EXACT = {"/health", "/healthz", "/api/health"}


@app.middleware("http")
async def gateway_auth(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS":
        return await call_next(request)
    if path in _AUTH_BYPASS_EXACT or path.startswith("/static/"):
        return await call_next(request)
    if _DEV_MODE and not _SSO_SECRET:
        return await call_next(request)
    if not _SSO_SECRET:
        return JSONResponse({"error": "service misconfigured"}, status_code=503)
    client_secret = request.headers.get("x-gateway-secret", "")
    if not hmac.compare_digest(client_secret, _SSO_SECRET):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


_cache: "OrderedDict[str, dict]" = OrderedDict()
_cache_lock = threading.Lock()
_TTL_DEFAULT = 10 * 60
_TTL: dict[str, int] = {
    "metrics_yaml": 60 * 60 * 24,
    "sources_yaml": 60 * 60 * 24,
    "polymarket": 60 * 5,
}


def cache_get(key: str) -> Optional[Any]:
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        ttl = _TTL.get(key.split("::", 1)[0], _TTL_DEFAULT)
        if time.time() - entry["t"] > ttl:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return entry["data"]


def cache_set(key: str, data: Any) -> None:
    with _cache_lock:
        _cache[key] = {"t": time.time(), "data": data}
        while len(_cache) > 64:
            _cache.popitem(last=False)


_USER_AGENT = "narve-love-dashboard/0.1 (+https://love.narve.ai)"
_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=8.0)


async def _http_get(url: str, *, params: Optional[dict] = None) -> Optional[httpx.Response]:
    try:
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json,*/*;q=0.5"},
            follow_redirects=True,
        ) as client:
            r = await client.get(url, params=params)
        if r.status_code == 200:
            return r
        return None
    except Exception:
        return None


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema() -> None:
    if not SCHEMA_PATH.is_file():
        return
    try:
        sql = SCHEMA_PATH.read_text()
        with _db_connect() as conn:
            conn.executescript(sql)
    except Exception as e:
        logger.warning("DB bootstrap failed: %s", e)


_ensure_schema()


def load_metrics() -> dict:
    cached = cache_get("metrics_yaml")
    if cached is not None:
        return cached
    path = DATA_DIR / "metrics.yaml"
    if not path.is_file():
        return {"metrics": [], "count": 0, "target": 30, "error": "metrics.yaml missing"}
    try:
        raw = yaml.safe_load(path.read_text())
    except Exception as e:
        return {"metrics": [], "count": 0, "target": 30, "error": str(e)}
    metrics = raw.get("metrics", []) if isinstance(raw, dict) else []
    out = {
        "source": "data/metrics.yaml (seed)",
        "target": raw.get("metric_count_target", 30) if isinstance(raw, dict) else 30,
        "generated_at": raw.get("generated_at") if isinstance(raw, dict) else None,
        "count": len(metrics),
        "metrics": metrics,
    }
    cache_set("metrics_yaml", out)
    return out


def load_sources() -> dict:
    cached = cache_get("sources_yaml")
    if cached is not None:
        return cached
    path = DATA_DIR / "sources.yaml"
    if not path.is_file():
        return {"sources": {}}
    try:
        out = yaml.safe_load(path.read_text()) or {"sources": {}}
    except Exception as e:
        return {"sources": {}, "error": str(e)}
    cache_set("sources_yaml", out)
    return out


def _metric_def(metric_id: str) -> Optional[dict]:
    data = load_metrics()
    for m in data.get("metrics", []):
        if (m.get("id") or "").lower() == metric_id.lower():
            return m
    return None


def query_metric_rows(metric_id: str, country: Optional[str] = None, period: Optional[str] = None, limit: int = 200) -> list[dict]:
    sql = "SELECT metric_id, country, period, value, source, updated_at FROM love_metrics WHERE metric_id = ?"
    args: list[Any] = [metric_id]
    if country:
        sql += " AND country = ?"
        args.append(country.lower())
    if period:
        sql += " AND period = ?"
        args.append(period)
    sql += " ORDER BY period DESC LIMIT ?"
    args.append(limit)
    try:
        with _db_connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except Exception:
        return []


def trend_rows(metric_id: str, country: str, years: int) -> list[dict]:
    sql = "SELECT period, value, source, updated_at FROM love_metrics WHERE metric_id = ? AND country = ? ORDER BY period DESC LIMIT ?"
    try:
        with _db_connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, (metric_id, country.lower(), years)).fetchall()]
    except Exception:
        return []
    rows.reverse()
    return rows


GAMMA_BASE = "https://gamma-api.polymarket.com"
LOVE_TAG_SLUGS = ["relationships", "marriage", "family", "dating", "fertility", "birth-rate", "divorce", "weddings", "society"]
REJECT_KEYWORDS = ["nfl", "nba", "nhl", "mlb", "rugby", "champion", "playoff", "election", "president", "senate", "ipo", "stock", "bitcoin", "crypto", "tesla", "spacex"]
LOVE_KEYWORDS = ["marriage", "married", "divorce", "wedding", "cohabit", "fertility", "birth rate", "births", "tfr", "dating", "tinder", "hinge", "bumble", "loneliness", "single", "household", "remarriage", "civil partnership"]


async def _fetch_events_by_tag(client: httpx.AsyncClient, tag: str) -> list[dict]:
    out: list[dict] = []
    offset = 0
    for _ in range(4):
        try:
            r = await client.get(f"{GAMMA_BASE}/events", params={"tag_slug": tag, "closed": "false", "limit": "100", "offset": str(offset)})
        except Exception:
            break
        if r.status_code != 200:
            break
        try:
            events = r.json()
        except Exception:
            break
        if not events:
            break
        for ev in events:
            title = ev.get("title", "") or ""
            tl = title.lower()
            if any(k in tl for k in REJECT_KEYWORDS):
                continue
            tags = [t.get("label", "") for t in (ev.get("tags") or []) if isinstance(t, dict)]
            for m in ev.get("markets") or []:
                mid = m.get("conditionId") or m.get("id")
                if not mid:
                    continue
                m["_event_title"] = title
                m["_event_tags"] = tags
                out.append(m)
        offset += 100
    return out


async def fetch_polymarket_love() -> dict:
    cached = cache_get("polymarket")
    if cached is not None:
        return cached
    seen_ids: set[str] = set()
    all_markets: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers={"User-Agent": _USER_AGENT}, follow_redirects=True) as client:
            results = await asyncio.gather(*[_fetch_events_by_tag(client, t) for t in LOVE_TAG_SLUGS], return_exceptions=True)
    except Exception:
        results = []
    for batch in results:
        if isinstance(batch, BaseException):
            continue
        for m in batch:
            mid = m.get("conditionId") or m.get("id")
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            all_markets.append(m)
    filtered: list[dict] = []
    for m in all_markets:
        title = (m.get("_event_title") or "") + " " + (m.get("question") or "")
        tl = title.lower()
        tag_labels = [str(t).lower() for t in m.get("_event_tags", [])]
        if any(k in tl for k in LOVE_KEYWORDS) or any(any(kw in t for kw in ("relationship", "marriage", "family")) for t in tag_labels):
            filtered.append(m)
    enriched = []
    for m in filtered:
        try:
            implied = float(m.get("lastTradePrice") or m.get("bestBid") or 0)
        except (ValueError, TypeError):
            implied = None
        enriched.append({**m, "_implied_p": implied, "_model_p": None, "_edge_pp": None, "_rationale": "Awaiting per-metric trend model."})
    out = {
        "source": "Polymarket Gamma API",
        "tag_slugs": LOVE_TAG_SLUGS,
        "markets": enriched,
        "count": len(enriched),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "stub": not bool(filtered),
        "note": None if filtered else "No live love-tagged markets returned.",
    }
    cache_set("polymarket", out)
    return out


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "love-dashboard", "ts": time.time(), "env_files_loaded": _loaded_env_files}


@app.get("/api/health")
def api_health() -> dict:
    return health()


# ──────────────────────────────────────────────────────────────────────────────
# Sentry deploy-verification endpoint.
#
# Raises a deliberate exception so an operator can confirm the subproduct's
# Sentry DSN is wired correctly after a deploy. The gateway_auth HMAC
# middleware above already gates every request, but we add a second check
# here so a non-admin user with a valid session can't burn through Sentry
# quota. Two ways to pass:
#   1. NARVE_ADMIN_EMAIL set and matches the gateway-injected user email, OR
#   2. Request comes directly from loopback (no gateway in front — useful
#      for local debugging when DEV_MODE skips the HMAC check).
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/_sentry-test")
async def _sentry_test(request: Request) -> dict:
    admin_email = os.environ.get("NARVE_ADMIN_EMAIL", "").strip().lower()
    gw_email = request.headers.get("x-gateway-user-email", "").strip().lower()
    client_host = (request.client.host if request.client else "") or ""
    is_admin = bool(admin_email) and gw_email == admin_email
    is_local = client_host in ("127.0.0.1", "::1")
    if not (is_admin or is_local):
        raise HTTPException(status_code=403, detail="admin or loopback only")
    raise RuntimeError("Sentry test event — this is intentional (love-dashboard)")


@app.get("/")
def index() -> FileResponse:
    path = STATIC_DIR / "index.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="index.html not built")
    return FileResponse(str(path))


@app.get("/api/metrics")
def api_metrics(
    metric: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    period: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    catalogue = load_metrics()
    metrics_list = catalogue.get("metrics", [])
    if not metric:
        if q:
            ql = q.lower()
            metrics_list = [m for m in metrics_list if ql in (m.get("id") or "").lower() or ql in (m.get("name") or "").lower() or ql in (m.get("description") or "").lower()]
        return {**catalogue, "metrics": metrics_list[:limit], "filtered_count": len(metrics_list[:limit]), "total_in_atlas": catalogue.get("count", 0)}
    metric_def = _metric_def(metric)
    if metric_def is None:
        raise HTTPException(status_code=404, detail=f"unknown metric: {metric}")
    rows = query_metric_rows(metric, country=country, period=period, limit=limit)
    return {
        "metric": metric_def, "country": country, "period": period,
        "rows": rows, "count": len(rows), "stub": not rows,
        "note": None if rows else f"No values for {metric} yet - scheduled scrape pending.",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/trends")
def api_trends(metric: str = Query(...), country: str = Query("us"), years: int = Query(10, ge=1, le=60)) -> dict:
    metric_def = _metric_def(metric)
    if metric_def is None:
        raise HTTPException(status_code=404, detail=f"unknown metric: {metric}")
    rows = trend_rows(metric, country, years)
    return {
        "metric": metric_def, "country": country.lower(), "years_requested": years,
        "rows": rows, "count": len(rows), "stub": not rows,
        "note": None if rows else f"No trend rows for {metric}/{country} yet.",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/compare")
def api_compare(country_a: str = Query(...), country_b: str = Query(...), metric: str = Query(...), years: int = Query(20, ge=1, le=60)) -> dict:
    metric_def = _metric_def(metric)
    if metric_def is None:
        raise HTTPException(status_code=404, detail=f"unknown metric: {metric}")
    a_rows = trend_rows(metric, country_a, years)
    b_rows = trend_rows(metric, country_b, years)
    return {
        "metric": metric_def, "country_a": country_a.lower(), "country_b": country_b.lower(),
        "years_requested": years, "a": a_rows, "b": b_rows,
        "stub": not (a_rows or b_rows),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/markets")
async def api_markets() -> dict:
    return await fetch_polymarket_love()


@app.get("/api/sources")
def api_sources() -> dict:
    return load_sources()


@app.get("/api/summary")
def api_summary() -> dict:
    cat = load_metrics()
    metrics_list = cat.get("metrics", [])
    status_counts: dict[str, int] = {}
    for m in metrics_list:
        status_counts[m.get("status") or "unknown"] = status_counts.get(m.get("status") or "unknown", 0) + 1
    return {
        "metrics": {"tracked": cat.get("count", 0), "target": cat.get("target", 30), "by_status": status_counts},
        "sources": {"count": len(load_sources().get("sources", {}) or {})},
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import uvicorn
    bind_host = os.environ.get("BIND_HOST", "127.0.0.1")
    uvicorn.run("server:app", host=bind_host, port=PORT, log_level="info")

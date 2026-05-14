#!/usr/bin/env python3
"""World Health Dashboard — FastAPI backend.

508-disease atlas, WHO Disease Outbreak News, AMR (antimicrobial
resistance) surveillance, drug supply-chain shortages (openFDA), and
Polymarket disease/health prediction markets with computed edge.

Sits on port 7053 behind the narve.ai gateway as ``health.narve.ai``.

Data sources (documented in data/sources.yaml):
  - WHO Disease Outbreak News         (RSS)
  - openFDA Drug Shortages            (JSON)
  - Polymarket Gamma API              (JSON, health/pandemic tags)
  - GLASS / CDC / ECDC AMR feeds      (stubbed — no public JSON yet)

Design:
  - Real-network calls live in fetcher functions; each is gated by an
    on-disk cache with per-key TTLs so we don't hammer WHO etc.
  - When upstreams 5xx or rate-limit, we degrade gracefully: stubs
    return the documented shape with ``"stub": true``.
  - The build itself does NOT hit any live network — caches are empty
    until something asks for them.
"""

from __future__ import annotations

# ── Observability — init Sentry FIRST, before FastAPI touches anything ───────
import observability as _observability
_observability.init_sentry(platform="world-health")

import asyncio
import hmac
import logging
import os
import re
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
print(f"[world-health-dashboard] env files loaded: {len(_loaded_env_files)}", flush=True)
for _f in _loaded_env_files:
    print(f"  ✓ {_f}", flush=True)
if not os.getenv("GATEWAY_SSO_SECRET"):
    print("⚠ [world-health-dashboard] GATEWAY_SSO_SECRET missing — gateway-fronted requests will be rejected", flush=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("world-health")

PORT = int(os.environ.get("PORT", "7053"))
DATA_DIR = _DASHBOARD_DIR / "data"
STATIC_DIR = _DASHBOARD_DIR / "static"

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="World Health",
    description="508-disease atlas · WHO outbreaks · AMR · drug supply chains · prediction markets",
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


# ── Gateway-SSO auth ─────────────────────────────────────────────────────────
#
# This service sits behind the narve gateway. Without verifying the shared
# secret, anything that can reach this port can forge identity headers and
# impersonate a subscriber. The middleware below 401s every request whose
# ``X-Gateway-Secret`` header doesn't match the server-side secret (constant
# time compare). Combined with binding 127.0.0.1, the dashboard is only
# reachable through the gateway proxy on the same host.

_SSO_SECRET = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"
_AUTH_BYPASS_EXACT = {"/health", "/healthz", "/api/health"}

if not _SSO_SECRET and not _DEV_MODE:
    logger.warning("GATEWAY_SSO_SECRET unset and DEV_MODE off — every gateway-fronted request will 401.")


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

# ── Cache ─────────────────────────────────────────────────────────────────────
_cache: "OrderedDict[str, dict]" = OrderedDict()
_cache_lock = threading.Lock()
_TTL_DEFAULT = 10 * 60
_TTL: dict[str, int] = {
    "who_don":     60 * 60,         # WHO outbreak feed — refresh hourly
    "diseases":    60 * 60 * 24,    # YAML on disk — re-read daily
    "fda":         60 * 30,         # openFDA — every 30 min
    "polymarket":  60 * 5,          # markets move — every 5 min
    "amr":         60 * 60 * 24,    # AMR — stub for now, daily would be plenty
}


def cache_get(key: str) -> Optional[Any]:
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


def cache_set(key: str, data: Any) -> None:
    with _cache_lock:
        _cache[key] = {"t": time.time(), "data": data}
        while len(_cache) > 64:
            _cache.popitem(last=False)


# ── HTTP helper ───────────────────────────────────────────────────────────────
_USER_AGENT = "narve-world-health-dashboard/0.1 (+https://health.narve.ai)"
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
        logger.warning("HTTP %d for %s", r.status_code, url)
        return None
    except Exception as e:
        logger.warning("HTTP error for %s: %s", url, e)
        return None


# ── Disease atlas (YAML on disk) ──────────────────────────────────────────────
def load_diseases() -> dict:
    cached = cache_get("diseases")
    if cached is not None:
        return cached
    path = DATA_DIR / "diseases.yaml"
    if not path.is_file():
        return {"diseases": [], "count": 0, "target": 508, "error": "diseases.yaml missing"}
    try:
        raw = yaml.safe_load(path.read_text())
    except Exception as e:
        logger.error("diseases.yaml parse error: %s", e)
        return {"diseases": [], "count": 0, "target": 508, "error": str(e)}
    diseases = raw.get("diseases", []) if isinstance(raw, dict) else []
    out = {
        "source": "data/diseases.yaml (seed)",
        "target": raw.get("disease_count_target", 508) if isinstance(raw, dict) else 508,
        "generated_at": raw.get("generated_at") if isinstance(raw, dict) else None,
        "count": len(diseases),
        "diseases": diseases,
    }
    cache_set("diseases", out)
    return out


def load_sources() -> dict:
    path = DATA_DIR / "sources.yaml"
    if not path.is_file():
        return {"sources": {}}
    try:
        return yaml.safe_load(path.read_text()) or {"sources": {}}
    except Exception as e:
        logger.error("sources.yaml parse error: %s", e)
        return {"sources": {}, "error": str(e)}


# ── WHO Disease Outbreak News (RSS) ───────────────────────────────────────────
WHO_DON_URL = "https://www.who.int/feeds/entity/csr/don/en/rss.xml"


def _strip_html(text: str) -> str:
    """Cheap HTML/CDATA stripper for RSS descriptions."""
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def fetch_who_outbreaks() -> dict:
    """Parse the WHO Disease Outbreak News RSS feed into structured items.

    No third-party XML dep — the feed is well-formed enough that regex
    parsing is fine for our needs (~50 items/feed).
    """
    cached = cache_get("who_don")
    if cached is not None:
        return cached
    r = await _http_get(WHO_DON_URL)
    if not r:
        out = {
            "source": "WHO Disease Outbreak News",
            "url": WHO_DON_URL,
            "items": [],
            "count": 0,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "stub": True,
            "note": "WHO RSS fetch failed — empty list returned.",
        }
        cache_set("who_don", out)
        return out
    text = r.text
    items = []
    for raw in re.findall(r"<item[^>]*>(.*?)</item>", text, flags=re.DOTALL):
        def grab(tag: str) -> str:
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", raw, flags=re.DOTALL)
            return _strip_html(m.group(1)) if m else ""
        title = grab("title")
        link = grab("link")
        pubdate = grab("pubDate")
        desc = grab("description")
        guid = grab("guid")
        items.append({
            "title": title,
            "link": link,
            "published": pubdate,
            "summary": desc[:500],
            "guid": guid,
        })
    out = {
        "source": "WHO Disease Outbreak News",
        "url": WHO_DON_URL,
        "items": items,
        "count": len(items),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set("who_don", out)
    return out


# ── openFDA Drug Shortages ────────────────────────────────────────────────────
OPENFDA_SHORTAGES_URL = "https://api.fda.gov/drug/shortages.json"


async def fetch_fda_shortages(search: Optional[str] = None) -> dict:
    """Pull the openFDA drug-shortage list.

    ``search`` is a generic drug name filter — passed through as an
    openFDA search expression on the ``generic_name`` field.
    """
    cache_key = f"fda::{(search or '').lower()}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    params: dict[str, Any] = {"limit": 200}
    if search:
        params["search"] = f"generic_name:{search}"
    r = await _http_get(OPENFDA_SHORTAGES_URL, params=params)
    if not r:
        out = {
            "source": "openFDA Drug Shortages",
            "url": OPENFDA_SHORTAGES_URL,
            "query": search,
            "shortages": [],
            "count": 0,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "stub": True,
            "note": "openFDA fetch failed — empty list returned.",
        }
        _cache_lock.acquire()
        try:
            _cache[cache_key] = {"t": time.time(), "data": out}
        finally:
            _cache_lock.release()
        return out
    try:
        data = r.json()
    except Exception as e:
        logger.warning("openFDA JSON parse error: %s", e)
        data = {}
    raw_results = data.get("results", []) if isinstance(data, dict) else []
    shortages: list[dict] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        shortages.append({
            "generic_name": item.get("generic_name"),
            "proprietary_name": item.get("proprietary_name"),
            "company_name": item.get("company_name"),
            "status": item.get("status"),
            "shortage_reason": item.get("shortage_reason"),
            "initial_posting_date": item.get("initial_posting_date"),
            "update_date": item.get("update_date"),
            "expected_resupply_date": item.get("expected_resupply_date"),
            "therapeutic_category": item.get("therapeutic_category"),
            "presentation": item.get("presentation"),
        })
    out = {
        "source": "openFDA Drug Shortages",
        "url": OPENFDA_SHORTAGES_URL,
        "query": search,
        "shortages": shortages,
        "count": len(shortages),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    with _cache_lock:
        _cache[cache_key] = {"t": time.time(), "data": out}
        while len(_cache) > 64:
            _cache.popitem(last=False)
    return out


# ── AMR (antimicrobial resistance) — STUB ─────────────────────────────────────
# Production will pull from WHO GLASS, CDC AR Threats, ECDC EARS-Net. None
# have a clean JSON API today; the build is intentionally stubbed.
_AMR_STUB: dict[str, dict] = {
    "methicillin": {
        "antibiotic": "methicillin",
        "target_pathogens": ["Staphylococcus aureus"],
        "global_resistance_pct": {"low_income": 30.0, "high_income": 35.0, "global": 33.0},
        "trend": "rising in low-income, plateaued in high-income",
        "source": "stub (WHO GLASS 2023 ranges)",
    },
    "carbapenem": {
        "antibiotic": "carbapenem",
        "target_pathogens": ["Klebsiella pneumoniae", "Acinetobacter baumannii", "Pseudomonas aeruginosa"],
        "global_resistance_pct": {"low_income": 27.0, "high_income": 9.0, "global": 18.0},
        "trend": "rising fast in ICUs globally",
        "source": "stub (WHO GLASS 2023 ranges)",
    },
    "fluoroquinolone": {
        "antibiotic": "fluoroquinolone",
        "target_pathogens": ["Escherichia coli", "Salmonella spp.", "Neisseria gonorrhoeae"],
        "global_resistance_pct": {"low_income": 42.0, "high_income": 22.0, "global": 35.0},
        "trend": "rising",
        "source": "stub (WHO GLASS 2023 ranges)",
    },
    "fluconazole": {
        "antibiotic": "fluconazole",
        "target_pathogens": ["Candida auris", "Candida glabrata"],
        "global_resistance_pct": {"low_income": 90.0, "high_income": 85.0, "global": 88.0},
        "trend": "endemic resistance in C. auris (treat as resistant by default)",
        "source": "stub (CDC AR Threats 2023)",
    },
}


def fetch_amr_stats(antibiotic: Optional[str]) -> dict:
    key = (antibiotic or "").strip().lower()
    if not key:
        return {
            "source": "stub",
            "antibiotic": None,
            "available_antibiotics": sorted(_AMR_STUB.keys()),
            "stub": True,
            "note": "Pass ?antibiotic=<name>. AMR is stubbed pending WHO GLASS / CDC AR / ECDC integration.",
        }
    # Allow partial matches like "methicillin-resistant" → "methicillin"
    for k, v in _AMR_STUB.items():
        if k in key or key in k:
            return {**v, "stub": True, "matched_key": k, "query": antibiotic}
    return {
        "source": "stub",
        "antibiotic": antibiotic,
        "available_antibiotics": sorted(_AMR_STUB.keys()),
        "stub": True,
        "note": f"No data for '{antibiotic}'. Try one of the available_antibiotics.",
    }


# ── Polymarket health markets ─────────────────────────────────────────────────
GAMMA_BASE = "https://gamma-api.polymarket.com"
HEALTH_TAG_SLUGS = [
    "health", "pandemic", "covid", "disease", "outbreak", "public-health",
    "h5n1", "bird-flu", "mpox", "monkeypox",
]
REJECT_KEYWORDS = [
    "nfl", "nba", "nhl", "mlb", "mls", "rugby", "premier league",
    "champion", "playoff", "election", "president", "senate",
    "ipo", "stock", "bitcoin", "crypto", "tesla", "spacex",
]
HEALTH_KEYWORDS = [
    "disease", "outbreak", "epidemic", "pandemic", "h5n1", "bird flu",
    "avian flu", "mpox", "monkeypox", "covid", "sars", "ebola",
    "marburg", "nipah", "polio", "measles", "dengue", "cholera",
    "tuberculosis", "tb ", "malaria", "hiv", "vaccine", "rsv",
    "lassa", "yellow fever", "phe", "who declares", "case count",
    "amr", "antibiotic resistance", "drug shortage",
]


async def _fetch_events_by_tag(client: httpx.AsyncClient, tag: str) -> list[dict]:
    out: list[dict] = []
    offset = 0
    for _ in range(4):
        try:
            r = await client.get(
                f"{GAMMA_BASE}/events",
                params={"tag_slug": tag, "closed": "false", "limit": "100", "offset": str(offset)},
            )
        except Exception as e:
            logger.warning("polymarket tag=%s error: %s", tag, e)
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


async def fetch_polymarket_health() -> dict:
    cached = cache_get("polymarket")
    if cached is not None:
        return cached
    seen_ids: set[str] = set()
    all_markets: list[dict] = []
    try:
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        ) as client:
            results = await asyncio.gather(
                *[_fetch_events_by_tag(client, t) for t in HEALTH_TAG_SLUGS],
                return_exceptions=True,
            )
    except Exception as e:
        logger.warning("polymarket gather error: %s", e)
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
    # Final keyword filter — Polymarket tagging is noisy
    filtered: list[dict] = []
    for m in all_markets:
        title = (m.get("_event_title") or "") + " " + (m.get("question") or "")
        tl = title.lower()
        if any(k in tl for k in HEALTH_KEYWORDS) or any("health" in t.lower() for t in m.get("_event_tags", [])):
            filtered.append(m)
    enriched = _attach_edges(filtered)
    out = {
        "source": "Polymarket Gamma API",
        "tag_slugs": HEALTH_TAG_SLUGS,
        "markets": enriched,
        "count": len(enriched),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "stub": False if filtered else True,
        "note": None if filtered else "No live health markets returned; gamma may be empty or unreachable.",
    }
    cache_set("polymarket", out)
    return out


def _attach_edges(markets: list[dict]) -> list[dict]:
    """Compute a coarse edge per market.

    We don't have a per-disease epidemiological model yet (that's the
    "tracked" rows in diseases.yaml — TODO for a follow-up). For now we
    report:

      _implied_p : last-trade price (Polymarket's market-clearing prob)
      _model_p   : None (placeholder — fill in once disease models ship)
      _edge_pp   : None
    """
    out = []
    for m in markets:
        try:
            implied = float(m.get("lastTradePrice") or m.get("bestBid") or 0)
        except (ValueError, TypeError):
            implied = None
        out.append({
            **m,
            "_implied_p": implied,
            "_model_p": None,
            "_edge_pp": None,
            "_rationale": "Awaiting per-disease model (next iteration).",
        })
    return out


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "world-health-dashboard", "ts": time.time(),
            "env_files_loaded": _loaded_env_files}


@app.get("/api/health")
def api_health() -> dict:
    return health()


@app.get("/")
def index() -> FileResponse:
    path = STATIC_DIR / "index.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="index.html not built")
    return FileResponse(str(path))


@app.get("/api/outbreaks")
async def api_outbreaks(limit: int = Query(50, ge=1, le=200)) -> dict:
    data = await fetch_who_outbreaks()
    items = data.get("items", [])[:limit]
    return {**data, "items": items, "count": len(items), "limit": limit}


@app.get("/api/diseases")
def api_diseases(
    q: Optional[str] = Query(None, description="Substring match on name / ICD-10 / category"),
    category: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=1000),
) -> dict:
    data = load_diseases()
    diseases = data.get("diseases", [])
    if q:
        ql = q.lower()
        diseases = [d for d in diseases if ql in (d.get("name", "") or "").lower()
                    or ql in (d.get("icd10", "") or "").lower()
                    or ql in (d.get("category", "") or "").lower()]
    if category:
        cl = category.lower()
        diseases = [d for d in diseases if (d.get("category", "") or "").lower() == cl]
    if status:
        sl = status.lower()
        diseases = [d for d in diseases if (d.get("status", "") or "").lower() == sl]
    return {
        **data,
        "diseases": diseases[:limit],
        "filtered_count": len(diseases[:limit]),
        "total_in_atlas": data.get("count", 0),
    }


@app.get("/api/amr-stats")
def api_amr_stats(antibiotic: Optional[str] = Query(None)) -> dict:
    return fetch_amr_stats(antibiotic)


@app.get("/api/supply-chains")
async def api_supply_chains(drug: Optional[str] = Query(None)) -> dict:
    return await fetch_fda_shortages(drug)


@app.get("/api/markets")
async def api_markets() -> dict:
    return await fetch_polymarket_health()


@app.get("/api/sources")
def api_sources() -> dict:
    return load_sources()


@app.get("/api/summary")
async def api_summary() -> dict:
    """One-shot endpoint for the landing-page hero cards."""
    diseases = load_diseases()
    outbreaks = await fetch_who_outbreaks()
    shortages = await fetch_fda_shortages(None)
    return {
        "diseases": {
            "tracked": diseases.get("count", 0),
            "target": diseases.get("target", 508),
        },
        "outbreaks": {
            "count": outbreaks.get("count", 0),
            "latest": (outbreaks.get("items") or [None])[0],
        },
        "shortages": {
            "count": shortages.get("count", 0),
            "stub": shortages.get("stub", False),
        },
        "amr": {
            "antibiotics_tracked": sorted(_AMR_STUB.keys()),
            "stub": True,
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    # Loopback-only — the gateway is the sole ingress. Override with
    # ``BIND_HOST`` if you need to expose this directly for debugging.
    bind_host = os.environ.get("BIND_HOST", "127.0.0.1")
    logger.info("Starting world-health-dashboard on %s:%d", bind_host, PORT)
    uvicorn.run("server:app", host=bind_host, port=PORT, log_level="info")

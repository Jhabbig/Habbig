#!/usr/bin/env python3
"""World Health Dashboard — FastAPI backend.

508-disease atlas, WHO Disease Outbreak News, AMR (antimicrobial
resistance) surveillance, drug supply-chain shortages (openFDA), and
Polymarket disease/health prediction markets with computed edge.

Sits on port 7053 behind the narve.ai gateway as ``health.narve.ai``.

Data sources (documented in data/sources.yaml):
  - WHO Disease Outbreak News         (RSS, parsed via feedparser)
  - openFDA Drug Shortages            (JSON)
  - Polymarket Gamma API              (JSON, health/pandemic tags)
  - GLASS / CDC / ECDC AMR feeds      (stubbed — no public JSON yet)

Design:
  - Real-network calls live in fetcher functions; each is gated by an
    in-memory cache with per-key TTLs so we don't hammer WHO etc.
  - When upstreams 5xx, rate-limit, or time out we degrade gracefully:
    serve the last cached snapshot (stale-while-error) and, if no entry
    has ever been cached, fall back to a documented stub with
    ``"stub": true``. Endpoints always stay 200.
  - A background refresher pre-warms WHO + FDA caches every hour so the
    first user request after boot is instant.
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

# Security: import defusedxml BEFORE feedparser so feedparser auto-detects
# it and routes all XML parsing through the XXE-hardened parser (blocks
# external entities, DTDs, and entity expansion bombs). This protects the
# WHO Disease Outbreak News RSS path — WHO is trusted today but the feed
# is fetched over the public internet and could be tampered with mid-path.
# defusedxml is also exposed as ``ET`` for any future direct XML parsing
# (e.g. ECB SDW, other XML upstreams) so we never reach for stdlib
# ``xml.etree`` by accident.
import defusedxml  # noqa: F401  # presence forces feedparser to use it
import defusedxml.ElementTree as ET  # noqa: F401

# feedparser is the canonical WHO RSS parser. Treat it as optional — if the
# import fails for any reason, the fetcher falls back to a minimal regex
# parser so the service still boots. With defusedxml already imported above,
# feedparser's internal XML parser is XXE-safe.
try:
    import feedparser  # type: ignore
    _HAS_FEEDPARSER = True
except Exception:  # pragma: no cover — best-effort fallback path
    feedparser = None  # type: ignore[assignment]
    _HAS_FEEDPARSER = False

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


# ── BetterStack / Logtail ─────────────────────────────────────────────────────
# Ships structured logs to the central BetterStack source for the "world-health"
# subproduct. Falls back to the apex LOGTAIL_TOKEN if the per-service variable
# is unset. If neither is set we silently skip — stdout/stderr handlers stay
# attached so logs are never lost.
class _ServiceTagFilter(logging.Filter):
    """Stamps every record with service=<name> so BetterStack can route/group."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service = service_name

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        if not hasattr(record, "service"):
            record.service = self._service
        return True


_logtail_token = os.getenv("LOGTAIL_TOKEN_HEALTH", os.getenv("LOGTAIL_TOKEN", "")).strip()
# Always tag local records with the service name so downstream aggregators
# (docker logs -> vector -> wherever) can group correctly even without Logtail.
logging.getLogger().addFilter(_ServiceTagFilter("world-health"))
if _logtail_token:
    try:
        from logtail import LogtailHandler  # type: ignore

        _handler = LogtailHandler(source_token=_logtail_token)
        _handler.setLevel(logging.INFO)
        _handler.addFilter(_ServiceTagFilter("world-health"))
        logging.getLogger().addHandler(_handler)
        logger.info("Logtail handler attached", extra={"service": "world-health"})
    except ImportError:
        logger.warning("logtail-python not installed; skipping BetterStack handler",
                       extra={"service": "world-health"})
    except Exception as _exc:  # pragma: no cover — defensive: never crash on log init
        logger.warning("Logtail init failed: %s", _exc, extra={"service": "world-health"})


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
    "who_don":     60 * 60,         # WHO DON RSS — refresh hourly
    "diseases":    60 * 60 * 24,    # YAML on disk — re-read daily
    "fda":         60 * 60 * 4,     # openFDA drug shortages — every 4h
    "polymarket":  60 * 5,          # markets move — every 5 min
    "amr":         60 * 60 * 24,    # AMR — stub for now, daily would be plenty
}


def _ttl_for(key: str) -> int:
    """TTL lookup that honours prefixes — ``fda::aspirin`` shares the
    ``fda`` TTL so we can cache per-drug variants without listing each.
    """
    if key in _TTL:
        return _TTL[key]
    head = key.split("::", 1)[0]
    return _TTL.get(head, _TTL_DEFAULT)


def cache_get(key: str) -> Optional[Any]:
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        if time.time() - entry["t"] > _ttl_for(key):
            # Expired — but keep the entry so ``cache_get_stale`` can serve
            # it as a fallback if the next live fetch fails.
            return None
        _cache.move_to_end(key)
        return entry["data"]


def cache_get_stale(key: str) -> Optional[Any]:
    """Return the cached value ignoring TTL — used as the failover when an
    upstream fetch errors. Returns ``None`` if nothing was ever cached.
    """
    with _cache_lock:
        entry = _cache.get(key)
        return entry["data"] if entry else None


def cache_set(key: str, data: Any) -> None:
    with _cache_lock:
        _cache[key] = {"t": time.time(), "data": data}
        while len(_cache) > 64:
            _cache.popitem(last=False)


# ── HTTP helper ───────────────────────────────────────────────────────────────
_USER_AGENT = "narve.ai world-health-dashboard"
# 30s per-call budget per the upstream contract; connect cap stays tight so a
# dead resolver fails fast rather than burning the whole budget on DNS.
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


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
WHO_DON_CACHE_KEY = "who_don"

# Disease-keyword vocabulary used to tag DON items. Order matters — longer /
# more specific matches go first so e.g. "yellow fever" wins over "fever".
_DISEASE_KEYWORDS: list[str] = [
    "avian influenza", "bird flu", "h5n1", "h7n9",
    "monkeypox", "mpox",
    "yellow fever",
    "marburg virus disease", "marburg",
    "ebola virus disease", "ebola",
    "lassa fever", "lassa",
    "crimean-congo", "rift valley",
    "middle east respiratory syndrome", "mers-cov", "mers",
    "severe acute respiratory syndrome", "sars-cov-2", "sars",
    "covid-19", "covid",
    "nipah virus", "nipah",
    "hendra", "chikungunya", "dengue", "zika",
    "chapare", "machupo", "junin",
    "plague",
    "diphtheria", "tetanus", "pertussis",
    "polio", "poliomyelitis",
    "measles", "rubella",
    "cholera",
    "typhoid", "paratyphoid",
    "meningococcal disease", "meningitis",
    "anthrax", "botulism", "tularemia", "brucellosis",
    "rabies",
    "tuberculosis",
    "leishmaniasis", "trypanosomiasis",
    "schistosomiasis",
    "leptospirosis", "legionellosis",
    "hepatitis a", "hepatitis b", "hepatitis c", "hepatitis e", "hepatitis",
    "influenza",
    "respiratory syncytial virus", "rsv",
    "hand foot and mouth",
    "scarlet fever", "streptococcal",
    "salmonellosis", "shigellosis", "listeriosis", "campylobacteriosis",
    "norovirus",
    "haemorrhagic fever",
    "acute flaccid", "encephalitis",
    "oropouche",
    "unknown etiology",
]


def _extract_disease(title: str) -> Optional[str]:
    """Pick a disease keyword out of a DON headline. Returns the matched
    canonical token (e.g. ``"ebola"``) or ``None`` if nothing matches.
    """
    if not title:
        return None
    tl = title.lower()
    for kw in _DISEASE_KEYWORDS:
        if kw in tl:
            return kw
    return None


def _strip_html(text: str) -> str:
    """Cheap HTML/CDATA stripper for RSS descriptions (fallback path)."""
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_who_feed_with_feedparser(text: str) -> list[dict]:
    parsed = feedparser.parse(text)  # type: ignore[union-attr]
    items: list[dict] = []
    for entry in parsed.entries:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        published = (entry.get("published") or entry.get("updated") or "").strip()
        summary_raw = entry.get("summary") or entry.get("description") or ""
        summary = _strip_html(summary_raw)[:500]
        items.append({
            "title": title,
            "link": link,
            "published": published,
            "summary": summary,
            "disease": _extract_disease(title),
        })
    return items


def _parse_who_feed_with_regex(text: str) -> list[dict]:
    items: list[dict] = []
    for raw in re.findall(r"<item[^>]*>(.*?)</item>", text, flags=re.DOTALL):
        def grab(tag: str) -> str:
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", raw, flags=re.DOTALL)
            return _strip_html(m.group(1)) if m else ""
        title = grab("title")
        items.append({
            "title": title,
            "link": grab("link"),
            "published": grab("pubDate"),
            "summary": grab("description")[:500],
            "disease": _extract_disease(title),
        })
    return items


async def fetch_who_outbreaks() -> dict:
    """Parse the WHO Disease Outbreak News RSS feed into structured items.

    Uses ``feedparser`` when available; falls back to a regex parser if not.
    On HTTP failure, returns the last cached snapshot (stale) — and only if
    no entry has ever been cached does it emit a ``stub`` shape.
    """
    cached = cache_get(WHO_DON_CACHE_KEY)
    if cached is not None:
        return cached
    r = await _http_get(WHO_DON_URL)
    if not r:
        stale = cache_get_stale(WHO_DON_CACHE_KEY)
        if stale is not None:
            return {**stale, "served_stale": True}
        return {
            "source": "WHO Disease Outbreak News",
            "url": WHO_DON_URL,
            "items": [],
            "count": 0,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "stub": True,
            "error": "WHO RSS fetch failed and no cached snapshot available.",
        }
    text = r.text
    try:
        if _HAS_FEEDPARSER:
            items = _parse_who_feed_with_feedparser(text)
        else:
            items = _parse_who_feed_with_regex(text)
    except Exception as e:
        logger.warning("WHO RSS parse error: %s", e)
        stale = cache_get_stale(WHO_DON_CACHE_KEY)
        if stale is not None:
            return {**stale, "served_stale": True}
        return {
            "source": "WHO Disease Outbreak News",
            "url": WHO_DON_URL,
            "items": [],
            "count": 0,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "stub": True,
            "error": f"parse error: {e}",
        }
    out = {
        "source": "WHO Disease Outbreak News",
        "url": WHO_DON_URL,
        "parser": "feedparser" if _HAS_FEEDPARSER else "regex-fallback",
        "items": items,
        "count": len(items),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set(WHO_DON_CACHE_KEY, out)
    return out


# ── openFDA Drug Shortages ────────────────────────────────────────────────────
OPENFDA_SHORTAGES_URL = "https://api.fda.gov/drug/shortages.json"
# Default page size when no drug filter is set. 50 keeps payloads small for
# the landing-page summary card; per-drug queries reuse the same cap because
# openFDA returns at most a few records per generic name.
FDA_DEFAULT_LIMIT = 50


def _fda_cache_key(search: Optional[str]) -> str:
    return f"fda::{(search or '').lower()}"


def _normalize_fda_results(raw_results: list[Any]) -> list[dict]:
    shortages: list[dict] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        shortages.append({
            "generic_name": item.get("generic_name"),
            "proprietary_name": item.get("proprietary_name"),
            "company_name": item.get("company_name"),
            "status": item.get("status"),
            "shortage_reason": item.get("shortage_reason") or item.get("reason"),
            "related_info": item.get("related_info"),
            "initial_posting_date": item.get("initial_posting_date"),
            "update_date": item.get("update_date"),
            "expected_resupply_date": item.get("expected_resupply_date"),
            "therapeutic_category": item.get("therapeutic_category"),
            "presentation": item.get("presentation"),
        })
    return shortages


async def fetch_fda_shortages(search: Optional[str] = None) -> dict:
    """Pull the openFDA drug-shortage list.

    ``search`` is a generic drug name. When set, the openFDA search
    expression filters on ``generic_name`` with a quoted exact-phrase match
    so multi-word drug names ("sodium chloride") aren't tokenized.
    """
    cache_key = _fda_cache_key(search)
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    params: dict[str, Any] = {
        "limit": FDA_DEFAULT_LIMIT,
        "sort": "update_date:desc",
    }
    if search:
        # Quote the query so multi-word generics survive openFDA's tokenizer.
        params["search"] = f'generic_name:"{search}"'
    r = await _http_get(OPENFDA_SHORTAGES_URL, params=params)
    if not r:
        stale = cache_get_stale(cache_key)
        if stale is not None:
            return {**stale, "served_stale": True}
        return {
            "source": "openFDA Drug Shortages",
            "url": OPENFDA_SHORTAGES_URL,
            "query": search,
            "shortages": [],
            "count": 0,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "stub": True,
            "error": "openFDA fetch failed and no cached snapshot available.",
        }
    try:
        data = r.json()
    except Exception as e:
        logger.warning("openFDA JSON parse error: %s", e)
        stale = cache_get_stale(cache_key)
        if stale is not None:
            return {**stale, "served_stale": True}
        data = {}
    raw_results = data.get("results", []) if isinstance(data, dict) else []
    shortages = _normalize_fda_results(raw_results)
    out = {
        "source": "openFDA Drug Shortages",
        "url": OPENFDA_SHORTAGES_URL,
        "query": search,
        "shortages": shortages,
        "count": len(shortages),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_set(cache_key, out)
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


# ── Background refresher ─────────────────────────────────────────────────────
#
# Pre-warm WHO + FDA caches on a fixed interval so the first user request
# after boot is instant and so a transient upstream blip can be ridden out
# by the stale-fallback. Cadence is hourly: it's the gating TTL (WHO) and
# safely under FDA's 4h TTL.
_REFRESH_INTERVAL_SECONDS = 60 * 60
_refresher_task: Optional[asyncio.Task] = None


async def _refresh_caches_once() -> None:
    logger.info("background refresh: WHO DON + FDA shortages")
    try:
        await fetch_who_outbreaks()
    except Exception as e:  # never raise out of the refresher
        logger.warning("background refresh WHO failed: %s", e)
    try:
        await fetch_fda_shortages(None)
    except Exception as e:
        logger.warning("background refresh FDA failed: %s", e)


async def _refresher_loop() -> None:
    # Short initial delay so startup isn't blocked behind two slow upstreams.
    await asyncio.sleep(2.0)
    while True:
        await _refresh_caches_once()
        try:
            await asyncio.sleep(_REFRESH_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


@app.on_event("startup")
async def _start_refresher() -> None:
    global _refresher_task
    if os.environ.get("DISABLE_BG_REFRESH", "").strip() == "1":
        logger.info("background refresher disabled via DISABLE_BG_REFRESH=1")
        return
    if _refresher_task is None or _refresher_task.done():
        _refresher_task = asyncio.create_task(_refresher_loop(), name="world-health-refresher")
        logger.info("background refresher started (interval=%ds)", _REFRESH_INTERVAL_SECONDS)


@app.on_event("shutdown")
async def _stop_refresher() -> None:
    global _refresher_task
    if _refresher_task is not None and not _refresher_task.done():
        _refresher_task.cancel()
        try:
            await _refresher_task
        except (asyncio.CancelledError, Exception):
            pass
        _refresher_task = None


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
            "stub": outbreaks.get("stub", False),
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

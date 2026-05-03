"""Manifold Markets health-question ingestion.

Manifold has an open, key-less API at api.manifold.markets/v0. We use
/search-markets with health-related search terms; each call returns up to 100
matching open markets sorted by relevance. We dedupe by id across queries
and keep open binary markets.

Manifold uses play money for most markets but has a substantial volume of
health/pandemic forecasting questions, often more granular than Polymarket
or Kalshi.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path
from threading import Lock

log = logging.getLogger(__name__)

API = "https://api.manifold.markets/v0"
CACHE_DIR = Path(__file__).parent.parent / "cache" / "markets"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_SECONDS = 5 * 60

_lock = Lock()

# Search terms — each yields up to 100 markets, we dedupe across.
# Ordered roughly by specificity (most-specific first to avoid generic noise).
SEARCH_TERMS: tuple[str, ...] = (
    "H5N1", "bird flu", "pandemic", "PHEIC", "WHO", "FDA approval",
    "mpox", "Ebola", "Marburg", "measles", "polio",
    "vaccine", "outbreak", "epidemic",
    "Ozempic", "weight loss drug", "GLP-1",
    "life expectancy", "mortality", "disease",
    "RFK", "Surgeon General", "abortion",
    "drug approval", "clinical trial",
)


def _cache_path() -> Path:
    return CACHE_DIR / "manifold_health.json"


def _read_cache() -> dict | None:
    p = _cache_path()
    if not p.exists():
        return None
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Manifold cache unreadable: %s", exc)
        return None
    if (time.time() - body.get("fetched_at", 0)) > CACHE_TTL_SECONDS:
        return None
    return body


def _write_cache(payload: dict) -> None:
    try:
        _cache_path().write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:
        log.warning("Manifold cache write failed: %s", exc)


def _http_get(path: str, params: dict, timeout: float = 15.0) -> object:
    qs = urllib.parse.urlencode(params)
    url = f"{API}{path}?{qs}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "world-health-dashboard/0.3",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted)
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _safe_float(x) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (ValueError, TypeError):
        return None


def _normalize(m: dict) -> dict:
    return {
        "id": m.get("id") or "",
        "source": "manifold",
        "question": m.get("question") or "",
        "url": m.get("url") or "",
        "probability": _safe_float(m.get("probability")),
        "volume": _safe_float(m.get("volume")),
        "volume_24h": _safe_float(m.get("volume24Hours")),
        "liquidity": _safe_float(m.get("totalLiquidity")),
        "close_time": _epoch_ms_to_iso(m.get("closeTime")),
        "category": m.get("outcomeType"),
        "outcomes": None,
        "tags": [],
        "description": "",  # Manifold returns rich text; skip in v1.
    }


def _epoch_ms_to_iso(ms) -> str | None:
    if not ms:
        return None
    try:
        ts = int(ms) / 1000.0
    except (ValueError, TypeError):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def fetch(force: bool = False) -> dict:
    with _lock:
        if not force:
            cached = _read_cache()
            if cached:
                return cached

    seen: set[str] = set()
    out: list[dict] = []
    for term in SEARCH_TERMS:
        try:
            data = _http_get("/search-markets", {
                "term": term,
                "limit": 100,
                "filter": "open",
                "contractType": "BINARY",
                "sort": "score",
            })
        except Exception as exc:
            log.warning("Manifold search '%s' failed: %s", term, exc)
            continue
        if not isinstance(data, list):
            continue
        for m in data:
            mid = m.get("id")
            if not mid or mid in seen:
                continue
            if m.get("isResolved") or m.get("outcomeType") not in (None, "BINARY"):
                continue
            seen.add(mid)
            out.append(_normalize(m))

    out.sort(key=lambda x: -(x.get("volume") or 0.0))

    payload = {
        "source": "manifold",
        "markets": out,
        "fetched_at": time.time(),
        "search_terms": len(SEARCH_TERMS),
        "stale": False,
    }
    with _lock:
        _write_cache(payload)
    log.info("Manifold: %d health markets via %d searches", len(out), len(SEARCH_TERMS))
    return payload


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    p = fetch(force=True)
    print(f"Health markets: {len(p['markets'])}")
    for m in p["markets"][:10]:
        prob = f"{m['probability']:.2f}" if m['probability'] is not None else "—"
        vol = f"M${m['volume']:,.0f}" if m['volume'] else "—"
        print(f"  yes={prob}  vol={vol:>14s}  {m['question'][:80]}")

"""Polymarket health-market ingestion.

Pulls active markets from the Polymarket Gamma API and filters down to
health-related questions. Two filter paths:

  1. Tag-based — Polymarket tags markets like 'health', 'pandemics',
     'vaccines'. Mostly reliable for markets created via Polymarket's curated
     events. The full tag taxonomy is at /tags.
  2. Keyword fallback — questions / descriptions containing words like
     'WHO', 'FDA', 'pandemic', 'H5N1', 'mpox', etc. Catches markets that
     weren't tagged.

We pull active + non-archived + non-closed markets, normalize prices to
0.0–1.0, and emit a flat list with a stable shape across sources.
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

GAMMA = "https://gamma-api.polymarket.com"
CACHE_DIR = Path(__file__).parent.parent / "cache" / "markets"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_SECONDS = 5 * 60  # 5min — markets move

_lock = Lock()


# ── Health filter signals ─────────────────────────────────────────────────────
HEALTH_TAG_SLUGS: set[str] = {
    "health", "healthcare", "pandemics", "pandemic", "diseases", "disease",
    "vaccines", "vaccine", "mpox", "monkeypox", "avian-flu", "bird-flu",
    "h5n1", "ebola", "covid", "covid-19", "fda", "who", "cdc",
    "rfk-jr", "obesity", "drugs", "ozempic", "marburg", "dengue", "malaria",
    "polio", "measles", "wegovy", "public-health",
}

# Keywords matched CASE-SENSITIVELY against the question text only — using
# lowercase for these would catch "who" inside "who will win", "fda" inside
# "fdadasdf", etc. Acronyms and disease names that have a unique case form go
# here.
HEALTH_KEYWORDS_CASE_SENSITIVE: tuple[str, ...] = (
    " WHO ", " FDA ", " CDC ", " HHS ", " NIH ", " IHR ", " EMA ",
    " PHEIC ", " H5N1", " H5N", " H7N", " H9N", " H1N1",
    "Ebola", "Marburg", "Mpox", "MPOX", "Nipah", "Zika", "Lassa",
    "Ozempic", "Wegovy", "Mounjaro", "GLP-1",
    "RFK Jr",
    "Medicaid", "Medicare", "Obamacare", " ACA ",
)
# Lowercase keywords matched case-insensitively (always lowercased before compare).
HEALTH_KEYWORDS_LC: tuple[str, ...] = (
    "pandemic", "epidemic", "outbreak",
    "bird flu", "avian flu", "swine flu",
    "monkeypox", "dengue", "cholera", "malaria", "polio", "measles",
    "rabies", "tuberculosis", "hepatitis",
    "vaccine", "vaccination", "immunization", "booster shot",
    "drug approval", "fda approval", "clinical trial",
    "weight loss drug", "weight-loss drug",
    "surgeon general", "robert f kennedy",
    "abortion", "fertility", "ivf", " obesity ",
    "human cases", "human-to-human transmission", "zoonotic",
    "life expectancy", "infant mortality", "maternal mortality",
    "psilocybin", "marijuana", "cannabis legaliz",
    "covid", "coronavirus", "long covid",
)


def _cache_path() -> Path:
    return CACHE_DIR / "polymarket_health.json"


def _read_cache() -> dict | None:
    p = _cache_path()
    if not p.exists():
        return None
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Polymarket cache unreadable: %s", exc)
        return None
    if (time.time() - body.get("fetched_at", 0)) > CACHE_TTL_SECONDS:
        return None
    return body


def _write_cache(payload: dict) -> None:
    try:
        _cache_path().write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:
        log.warning("Polymarket cache write failed: %s", exc)


def _http_get(path: str, params: dict, timeout: float = 20.0) -> object:
    qs = urllib.parse.urlencode(params)
    url = f"{GAMMA}{path}?{qs}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "world-health-dashboard/0.3",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted)
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _fetch_active(limit: int = 500, offset: int = 0) -> list[dict]:
    """Fetch active, non-closed Polymarket markets sorted by volume."""
    params = {
        "limit": limit,
        "offset": offset,
        "closed": "false",
        "active": "true",
        "archived": "false",
        "order": "volumeNum",
        "ascending": "false",
    }
    data = _http_get("/markets", params)
    return data if isinstance(data, list) else []


def _is_health_market(m: dict) -> bool:
    """Decide if a Polymarket market is health-relevant.

    Only the *question* is matched — descriptions are too noisy (legal /
    resolution-source boilerplate frequently mentions WHO, CDC, etc).
    Acronym keywords are case-sensitive to avoid matching natural words like
    'who will win' or 'fda' as a substring.
    """
    # Tag-based — exact slug or label match.
    tags = m.get("tags") or []
    for t in tags:
        if isinstance(t, dict):
            slug = (t.get("slug") or "").lower()
            label = (t.get("label") or "").lower()
            if slug in HEALTH_TAG_SLUGS or label in HEALTH_TAG_SLUGS:
                return True

    q = " " + (m.get("question") or "") + " "
    for kw in HEALTH_KEYWORDS_CASE_SENSITIVE:
        if kw in q:
            return True
    q_lc = q.lower()
    for kw in HEALTH_KEYWORDS_LC:
        if kw in q_lc:
            return True
    return False


def _safe_float(x) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (ValueError, TypeError):
        return None


def _normalize(m: dict) -> dict:
    # Polymarket binary markets: outcomes is JSON array string of names,
    # outcomePrices is JSON array string of price strings.
    try:
        outcomes = json.loads(m.get("outcomes") or "[]") if isinstance(m.get("outcomes"), str) else m.get("outcomes") or []
        prices = json.loads(m.get("outcomePrices") or "[]") if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices") or []
    except Exception:
        outcomes, prices = [], []

    outs = []
    yes_price = None
    for i, name in enumerate(outcomes):
        p = _safe_float(prices[i]) if i < len(prices) else None
        outs.append({"label": str(name), "price": p})
        if isinstance(name, str) and name.lower() == "yes":
            yes_price = p

    if yes_price is None:
        # Fall back to bestBid as a yes-side proxy, then to outcomes[0].
        yes_price = _safe_float(m.get("bestBid")) or _safe_float(m.get("lastTradePrice"))
        if yes_price is None and outs:
            yes_price = outs[0]["price"]

    tags = []
    for t in (m.get("tags") or []):
        if isinstance(t, dict):
            label = t.get("label")
            if label:
                tags.append(label)

    slug = m.get("slug") or m.get("conditionId") or ""
    url = f"https://polymarket.com/event/{slug}" if slug else ""

    return {
        "id": str(m.get("id") or m.get("conditionId") or m.get("slug") or ""),
        "source": "polymarket",
        "question": m.get("question") or "",
        "url": url,
        "probability": yes_price,
        "volume": _safe_float(m.get("volume") or m.get("volumeNum")),
        "volume_24h": _safe_float(m.get("volume24hr")),
        "liquidity": _safe_float(m.get("liquidity") or m.get("liquidityNum")),
        "close_time": m.get("endDate") or m.get("endDateIso"),
        "category": (m.get("category") or "").strip() or None,
        "outcomes": outs if len(outs) > 2 else None,
        "tags": tags,
        "description": (m.get("description") or "")[:500],
    }


def fetch(force: bool = False, max_markets: int = 1500) -> dict:
    with _lock:
        if not force:
            cached = _read_cache()
            if cached:
                return cached

    raw_markets: list[dict] = []
    try:
        # Polymarket Gamma caps at 500/page; fetch up to 3 pages.
        for page in range(0, 3):
            batch = _fetch_active(limit=500, offset=page * 500)
            if not batch:
                break
            raw_markets.extend(batch)
            if len(batch) < 500:
                break
            if len(raw_markets) >= max_markets:
                break
    except Exception as exc:
        log.warning("Polymarket fetch failed: %s", exc)
        # Stale fallback.
        p = _cache_path()
        if p.exists():
            try:
                stale = json.loads(p.read_text(encoding="utf-8"))
                stale["stale"] = True
                stale["error"] = str(exc)
                return stale
            except Exception:
                pass
        return {"markets": [], "fetched_at": time.time(), "stale": False, "error": str(exc)}

    health = [m for m in raw_markets if _is_health_market(m)]
    norm = [_normalize(m) for m in health]
    norm.sort(key=lambda x: -(x.get("volume") or 0.0))

    payload = {
        "source": "polymarket",
        "markets": norm,
        "fetched_at": time.time(),
        "total_scanned": len(raw_markets),
        "stale": False,
    }
    with _lock:
        _write_cache(payload)
    log.info("Polymarket: %d health markets / %d scanned", len(norm), len(raw_markets))
    return payload


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    p = fetch(force=True)
    print(f"Health markets: {len(p['markets'])} of {p.get('total_scanned')}")
    for m in p["markets"][:8]:
        prob = f"{m['probability']:.2f}" if m['probability'] is not None else "—"
        vol = f"${m['volume']:,.0f}" if m['volume'] else "—"
        print(f"  yes={prob}  vol={vol:>15s}  {m['question'][:80]}")

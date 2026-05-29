"""Polymarket events ingestor — curated AI events with full multi-outcome trees.

We use Polymarket's Gamma API (`gamma-api.polymarket.com`), public, no key.

Two pathways:
- `fetch_featured(slugs)` — pulls the *event* by slug, including all nested
  binary markets. Useful for events like "which lab releases the next
  frontier model" that bundle many Yes/No options.
- `fetch_movers(limit, min_change)` — scans the global active markets list
  for biggest 24h price movers among AI-tagged questions.

Each market entry is normalized to:
    {
      "id":            str,
      "question":      str,
      "yes_outcome":   str,        # display label for the top-priced outcome
      "yes_price":     float,      # 0–1
      "one_day_change": float,     # signed, in *probability points* (-1..+1)
      "volume_24h":    float,      # USD
      "end_date":      ISO str,
      "url":           str,        # deep link to the event page
    }

An event additionally has `markets: [normalized_market, ...]`, plus `title`,
`slug`, `venue: "polymarket"`, `url`.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from threading import Lock

log = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
_UA = "Mozilla/5.0 (AIRaceDashboard/1.0; +markets)"
VENUE = "polymarket"

_TTL = 90  # seconds — markets move continuously; 90s is the centralbank prior.
_lock = Lock()
_event_cache: dict[str, tuple[float, dict | None]] = {}
_movers_cache: dict = {"data": None, "fetched_at": 0.0}


def _http_get_json(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _coerce_list(raw):
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []
    return raw if isinstance(raw, list) else []


def _coerce_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _normalize_market(m: dict, event_slug: str | None = None) -> dict | None:
    outcomes = _coerce_list(m.get("outcomes"))
    prices = [_coerce_float(p) for p in _coerce_list(m.get("outcomePrices"))]
    if not outcomes or not prices:
        return None
    # Prefer the literal "Yes" outcome on binary markets so movers + spreads
    # always compare Yes-vs-Yes. Fall back to top-priced for multi-outcome
    # events where there is no Yes/No.
    yes_idx = next(
        (i for i, o in enumerate(outcomes) if str(o).strip().lower() == "yes"),
        prices.index(max(prices)),
    )
    slug = event_slug or m.get("slug") or ""
    return {
        "id": m.get("id"),
        "question": m.get("question") or "",
        "yes_outcome": outcomes[yes_idx] if yes_idx < len(outcomes) else "Yes",
        "yes_price": prices[yes_idx],
        "outcomes": outcomes,
        "prices": prices,
        "one_day_change": _coerce_float(m.get("oneDayPriceChange")),
        "volume_24h": _coerce_float(m.get("volume24hr") or m.get("volume24hrClob")),
        "volume_total": _coerce_float(m.get("volumeNum") or m.get("volume")),
        "end_date": m.get("endDate") or "",
        "url": f"https://polymarket.com/event/{slug}" if slug else "",
    }


# ── Featured events ──────────────────────────────────────────────────────────
def fetch_event(slug: str) -> dict | None:
    """Fetch one event by slug, return normalized or None."""
    now = time.time()
    with _lock:
        cached = _event_cache.get(slug)
        if cached and (now - cached[0]) < _TTL:
            return cached[1]

    try:
        url = f"{GAMMA}/events?slug={urllib.parse.quote(slug)}"
        arr = _http_get_json(url)
        if not isinstance(arr, list) or not arr:
            payload = None
        else:
            ev = arr[0]
            markets = []
            for raw in (ev.get("markets") or []):
                norm = _normalize_market(raw, event_slug=slug)
                if norm:
                    markets.append(norm)
            if not markets:
                payload = None
            else:
                markets.sort(key=lambda m: m["yes_price"], reverse=True)
                payload = {
                    "venue": VENUE,
                    "slug": slug,
                    "title": ev.get("title") or "",
                    "description": (ev.get("description") or "")[:280],
                    "url": f"https://polymarket.com/event/{slug}",
                    "markets": markets,
                    "volume_24h_total": sum(m["volume_24h"] for m in markets),
                    "end_date": markets[0].get("end_date", ""),
                }
    except Exception as e:  # noqa: BLE001
        log.warning("Polymarket event fetch failed (%s): %s", slug, e)
        payload = None

    with _lock:
        _event_cache[slug] = (now, payload)
    return payload


def fetch_featured(slugs: list[str]) -> list[dict]:
    """Fetch every slug in order; drop any that 404."""
    out: list[dict] = []
    for slug in slugs:
        ev = fetch_event(slug)
        if ev:
            out.append(ev)
    return out


# ── Movers ───────────────────────────────────────────────────────────────────
def fetch_movers(ai_keywords: list[str], limit: int = 200, min_change: float = 0.05) -> list[dict]:
    """Scan active markets, return AI-tagged ones with |1d change| ≥ min_change."""
    now = time.time()
    with _lock:
        if _movers_cache["data"] is not None and (now - _movers_cache["fetched_at"]) < _TTL:
            return [m for m in _movers_cache["data"] if abs(m["one_day_change"]) >= min_change]

    out: list[dict] = []
    try:
        url = (
            f"{GAMMA}/markets?closed=false&active=true&limit={int(limit)}"
            "&order=volume24hr&ascending=false"
        )
        markets = _http_get_json(url)
        if not isinstance(markets, list):
            markets = []
        for m in markets:
            haystack = " ".join([
                (m.get("question") or "").lower(),
                (m.get("category") or "").lower(),
                (m.get("slug") or "").lower(),
            ])
            if not any(kw in haystack for kw in ai_keywords):
                continue
            norm = _normalize_market(m, event_slug=m.get("slug") or "")
            if norm:
                out.append(norm)
    except Exception as e:  # noqa: BLE001
        log.warning("Polymarket movers fetch failed: %s", e)

    out.sort(key=lambda m: abs(m["one_day_change"]), reverse=True)
    with _lock:
        _movers_cache["data"] = out
        _movers_cache["fetched_at"] = now
    return [m for m in out if abs(m["one_day_change"]) >= min_change]

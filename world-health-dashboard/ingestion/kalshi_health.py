"""Kalshi health-market ingestion.

Kalshi categorises markets at the *series* level (a series like CASE7DFL =
'Florida COVID daily case avg' has a category, then individual markets are
its weekly/monthly contracts). To get all health markets:

  1. GET /series?category=Health        → list of health series tickers
  2. For each series, GET /markets?series_ticker=X → its open markets
     (parallelised — Kalshi returns one or two active markets per series).

Prices arrive as decimal-dollar strings ("0.4300", "0.0012"). We parse them
to floats. Note the field names changed mid-2025 from `last_price` (cents) to
`last_price_dollars` (decimal) — we handle both for safety.
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

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
CACHE_DIR = Path(__file__).parent.parent / "cache" / "markets"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_SECONDS = 5 * 60

_lock = Lock()


def _cache_path() -> Path:
    return CACHE_DIR / "kalshi_health.json"


def _read_cache() -> dict | None:
    p = _cache_path()
    if not p.exists():
        return None
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Kalshi cache unreadable: %s", exc)
        return None
    if (time.time() - body.get("fetched_at", 0)) > CACHE_TTL_SECONDS:
        return None
    return body


def _write_cache(payload: dict) -> None:
    try:
        _cache_path().write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:
        log.warning("Kalshi cache write failed: %s", exc)


def _http_get(path: str, params: dict | None = None, timeout: float = 15.0) -> dict | None:
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"{KALSHI}{path}{qs}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "world-health-dashboard/0.3",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted)
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        log.warning("Kalshi GET %s failed: %s", path, exc)
        return None


def _series_ticker_from_event(m: dict) -> str:
    """The market's series_ticker field is sometimes empty; derive from
    event_ticker which has the form 'SERIES-EVENTSUFFIX' or 'SERIES'."""
    et = m.get("event_ticker") or ""
    if not et:
        return ""
    # Strip trailing -DDDDDDDD or similar event suffix
    parts = et.split("-")
    return parts[0] if parts else et


def _safe_float(x) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (ValueError, TypeError):
        return None
    # Cents-style values (>1) get normalized.
    if v > 1.5:
        v = v / 100.0
    return v


def _fetch_health_events(max_pages: int = 30) -> list[dict]:
    """Paginate /events with nested markets, keeping only category=Health.

    Each page returns 200 events of mixed categories. Health is a small slice
    (~3-5 events per page). We bail when the cursor stops advancing.
    """
    out: list[dict] = []
    cursor = ""
    for _ in range(max_pages):
        params = {"status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _http_get("/events", params)
        if not data:
            break
        es = data.get("events", []) or []
        out.extend([e for e in es if e.get("category") == "Health"])
        new_cursor = data.get("cursor") or ""
        if not new_cursor or new_cursor == cursor:
            break
        cursor = new_cursor
    return out


def _normalize(m: dict, series: dict | None) -> dict:
    yes = (
        _safe_float(m.get("yes_ask_dollars"))
        or _safe_float(m.get("last_price_dollars"))
        or _safe_float(m.get("yes_ask"))
        or _safe_float(m.get("last_price"))
    )
    vol = _safe_float(m.get("volume_fp")) or _safe_float(m.get("volume"))
    vol_24h = _safe_float(m.get("volume_24h_fp")) or _safe_float(m.get("volume_24h"))
    liq = _safe_float(m.get("liquidity_dollars")) or _safe_float(m.get("liquidity"))

    title = m.get("title") or ""
    sub = m.get("yes_sub_title") or m.get("subtitle") or ""
    question = title if not sub or sub.lower() in title.lower() else f"{title} — {sub}"

    return {
        "id": m.get("ticker") or "",
        "source": "kalshi",
        "question": question,
        "url": f"https://kalshi.com/markets/{(m.get('event_ticker') or '').lower()}",
        "probability": yes,
        "volume": vol,
        "volume_24h": vol_24h,
        "liquidity": liq,
        "close_time": m.get("close_time"),
        "category": "Health",
        "outcomes": None,  # Kalshi markets are binary by default
        "tags": [series.get("title") if series else None].__class__([
            t for t in [series.get("title") if series else None] if t
        ]),
        "description": (m.get("rules_primary") or m.get("subtitle") or "")[:500],
    }


def fetch(force: bool = False) -> dict:
    with _lock:
        if not force:
            cached = _read_cache()
            if cached:
                return cached

    try:
        events = _fetch_health_events()
    except Exception as exc:
        log.warning("Kalshi events fetch failed: %s", exc)
        events = []

    if not events:
        p = _cache_path()
        if p.exists():
            try:
                stale = json.loads(p.read_text(encoding="utf-8"))
                stale["stale"] = True
                return stale
            except Exception:
                pass
        return {"markets": [], "fetched_at": time.time(), "stale": False, "event_count": 0}

    raw_markets: list[tuple[dict, dict]] = []
    for ev in events:
        for mkt in ev.get("markets") or []:
            if mkt.get("status") in ("active", "open"):
                # Synthesize a 'series' record from the event for normalize().
                pseudo_series = {"ticker": ev.get("event_ticker"), "title": ev.get("title")}
                raw_markets.append((mkt, pseudo_series))

    normalized = [_normalize(m, s) for m, s in raw_markets]
    # Keep markets that have a price; many Kalshi health markets are
    # genuinely thin so we don't require non-zero volume.
    normalized = [m for m in normalized if m["probability"] is not None]
    normalized.sort(key=lambda x: -(x.get("volume") or 0.0))

    payload = {
        "source": "kalshi",
        "markets": normalized,
        "fetched_at": time.time(),
        "event_count": len(events),
        "stale": False,
    }
    with _lock:
        _write_cache(payload)
    log.info("Kalshi health: %d markets across %d events", len(normalized), len(events))
    return payload


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    p = fetch(force=True)
    print(f"Markets: {len(p['markets'])} across {p.get('event_count')} events")
    for m in p["markets"][:10]:
        prob = f"{m['probability']:.2f}" if m['probability'] is not None else "—"
        vol = f"${m['volume']:,.0f}" if m['volume'] else "—"
        print(f"  yes={prob}  vol={vol:>14s}  {m['question'][:80]}")

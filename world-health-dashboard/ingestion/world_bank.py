"""World Bank Open Data client.

Endpoint shape:
    https://api.worldbank.org/v2/country/all/indicator/<CODE>?format=json
        &date=<from>:<to>&per_page=20000

Response:
    [<meta dict>, [<row>, <row>, ...]]
    Each row: {
        "indicator": {"id": ..., "value": ...},
        "country":   {"id": ..., "value": ...},   # 2-letter
        "countryiso3code": "USA",
        "date": "2022",
        "value": 78.5 | None,
        ...
    }

We pull each indicator once, parse into per-country time series, and cache on
disk under cache/world_bank/<code>.json so a server restart doesn't re-hit the
API. TTL is 24 h (these indicators update quarterly to annually).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path
from threading import Lock

from .country_codes import normalize as normalize_iso3

log = logging.getLogger(__name__)

API = "https://api.worldbank.org/v2/country/all/indicator/{code}"
CACHE_DIR = Path(__file__).parent.parent / "cache" / "world_bank"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 24 h disk-cache TTL — World Bank refreshes annually, so this is conservative.
CACHE_TTL_SECONDS = 24 * 3600

# Year window. We pull a wide history so the time-slider has data.
DATE_FROM = 1960
DATE_TO = 2026

_lock = Lock()


def _cache_path(code: str) -> Path:
    safe = code.replace("/", "_").replace("..", "_")
    return CACHE_DIR / f"{safe}.json"


def _read_cache(code: str) -> dict | None:
    path = _cache_path(code)
    if not path.exists():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("World Bank cache unreadable for %s: %s", code, exc)
        return None
    if (time.time() - body.get("fetched_at", 0)) > CACHE_TTL_SECONDS:
        return None
    return body


def _write_cache(code: str, payload: dict) -> None:
    path = _cache_path(code)
    try:
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:
        log.warning("World Bank cache write failed for %s: %s", code, exc)


def _fetch(code: str, timeout: float = 30.0) -> list[dict]:
    """Hit World Bank, follow pagination if needed (rare for health indicators)."""
    qs = urllib.parse.urlencode(
        {
            "format": "json",
            "date": f"{DATE_FROM}:{DATE_TO}",
            "per_page": 20000,
        }
    )
    url = f"{API.format(code=code)}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "world-health-dashboard/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted host)
        body = resp.read().decode("utf-8", errors="replace")
    parsed = json.loads(body)
    if not isinstance(parsed, list) or len(parsed) < 2:
        log.warning("World Bank: unexpected response shape for %s", code)
        return []
    rows = parsed[1] or []
    if not isinstance(rows, list):
        return []
    return rows


def _shape_rows(rows: list[dict]) -> dict:
    """Convert raw rows into:
    {
      "by_country": {iso3: [(year:int, value:float), ...]},
      "latest":     {iso3: {"year": ..., "value": ...}},
    }
    """
    by_country: dict[str, list[tuple[int, float]]] = {}
    for r in rows:
        iso = r.get("countryiso3code") or ""
        iso = normalize_iso3(iso)
        if not iso:
            continue
        val = r.get("value")
        date = r.get("date")
        if val is None or date is None:
            continue
        try:
            year = int(date)
            v = float(val)
        except (ValueError, TypeError):
            continue
        by_country.setdefault(iso, []).append((year, v))

    latest: dict[str, dict] = {}
    for iso, points in by_country.items():
        points.sort(key=lambda p: p[0])
        if points:
            year, value = points[-1]
            latest[iso] = {"year": year, "value": value}

    return {
        "by_country": {iso: [{"year": y, "value": v} for y, v in pts] for iso, pts in by_country.items()},
        "latest": latest,
    }


def fetch_indicator(code: str, force: bool = False) -> dict:
    """Return shaped indicator data for `code`, with disk caching."""
    with _lock:
        if not force:
            cached = _read_cache(code)
            if cached:
                return cached
    try:
        rows = _fetch(code)
    except Exception as exc:
        log.warning("World Bank fetch failed for %s: %s", code, exc)
        # Fall back to stale cache if any.
        path = _cache_path(code)
        if path.exists():
            try:
                stale = json.loads(path.read_text(encoding="utf-8"))
                stale["stale"] = True
                return stale
            except Exception as cache_exc:
                log.warning("world_bank stale cache read failed for %s (%s); returning empty payload", code, cache_exc)
        return {"by_country": {}, "latest": {}, "fetched_at": time.time(), "error": str(exc)}

    shaped = _shape_rows(rows)
    payload = {
        "source": "world_bank",
        "code": code,
        "fetched_at": time.time(),
        **shaped,
    }
    with _lock:
        _write_cache(code, payload)
    log.info("World Bank %s: %d countries, %d rows", code, len(shaped["by_country"]), len(rows))
    return payload


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = fetch_indicator("SP.DYN.LE00.IN", force=True)
    print(f"Countries: {len(out['by_country'])}")
    print(f"USA latest: {out['latest'].get('USA')}")
    print(f"JPN latest: {out['latest'].get('JPN')}")

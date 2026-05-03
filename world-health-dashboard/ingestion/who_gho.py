"""WHO Global Health Observatory (GHO) OData client.

Endpoint:
    https://ghoapi.azureedge.net/api/<INDICATOR_CODE>

OData JSON shape:
    {"value": [{
        "Id": ..., "IndicatorCode": "...",
        "SpatialDimType": "COUNTRY",   "SpatialDim": "USA",
        "TimeDimType":    "YEAR",      "TimeDim":    2022,
        "Dim1Type":       "SEX",       "Dim1":       "SEX_BTSX",
        "NumericValue":   78.5,        "Value":      "78.5 [78.0-79.0]",
        ...
    }, ...]}

For most indicators we want country × year × both-sexes (Dim1='SEX_BTSX' when
the indicator is sex-disaggregated; otherwise empty / null). We collapse to:
    by_country: {iso3: [{year, value}, ...]}
    latest:     {iso3: {year, value}}

Filtering: WHO supports OData $filter, but we keep it simple and fetch the
whole indicator (typically a few thousand rows) then filter client-side. This
is what `centralbank-dashboard/ingestion/fred_client.py` does too.
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

API = "https://ghoapi.azureedge.net/api/{code}"
CACHE_DIR = Path(__file__).parent.parent / "cache" / "who_gho"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 24h TTL — WHO indicators refresh on a slow cycle (annually for most).
CACHE_TTL_SECONDS = 24 * 3600

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
        log.warning("WHO GHO cache unreadable for %s: %s", code, exc)
        return None
    if (time.time() - body.get("fetched_at", 0)) > CACHE_TTL_SECONDS:
        return None
    return body


def _write_cache(code: str, payload: dict) -> None:
    path = _cache_path(code)
    try:
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:
        log.warning("WHO GHO cache write failed for %s: %s", code, exc)


def _fetch(code: str, timeout: float = 45.0) -> list[dict]:
    """OData server-side filter for COUNTRY rows + both-sex (when applicable)."""
    flt = "SpatialDimType eq 'COUNTRY'"
    qs = urllib.parse.urlencode({"$filter": flt})
    url = f"{API.format(code=code)}?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "world-health-dashboard/0.1",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted host)
        body = resp.read().decode("utf-8", errors="replace")
    parsed = json.loads(body)
    rows = parsed.get("value", [])
    return rows if isinstance(rows, list) else []


def _shape_rows(rows: list[dict]) -> dict:
    """For each (country, year) keep one value, preferring both-sexes when the
    indicator is sex-disaggregated. Some indicators (HPV, maternal mortality)
    are female-only — we keep those single-sex rows rather than discarding."""
    # SEX preference (lower number = higher priority).
    SEX_PRIORITY = {"SEX_BTSX": 0, "": 1, "SEX_FMLE": 2, "SEX_MLE": 3}

    # raw[(iso, year)] = list of (sex_priority, value)
    raw: dict[tuple[str, int], list[tuple[int, float]]] = {}
    for r in rows:
        iso = normalize_iso3(r.get("SpatialDim") or "")
        if not iso:
            continue
        val = r.get("NumericValue")
        year = r.get("TimeDim")
        if val is None or year is None:
            continue
        try:
            y = int(year)
            v = float(val)
        except (ValueError, TypeError):
            continue
        dim1_type = (r.get("Dim1Type") or "").upper()
        dim1 = (r.get("Dim1") or "").upper()
        sex_pri = SEX_PRIORITY.get(dim1, 1) if dim1_type == "SEX" else 1
        raw.setdefault((iso, y), []).append((sex_pri, v))

    deduped: dict[str, dict[int, float]] = {}
    for (iso, y), entries in raw.items():
        entries.sort(key=lambda e: e[0])
        deduped.setdefault(iso, {})[y] = entries[0][1]

    latest: dict[str, dict] = {}
    series: dict[str, list[dict]] = {}
    for iso, per_year in deduped.items():
        sorted_years = sorted(per_year.keys())
        series[iso] = [{"year": y, "value": per_year[y]} for y in sorted_years]
        if sorted_years:
            ymax = sorted_years[-1]
            latest[iso] = {"year": ymax, "value": per_year[ymax]}

    return {"by_country": series, "latest": latest}


def fetch_indicator(code: str, force: bool = False) -> dict:
    with _lock:
        if not force:
            cached = _read_cache(code)
            if cached:
                return cached
    try:
        rows = _fetch(code)
    except Exception as exc:
        log.warning("WHO GHO fetch failed for %s: %s", code, exc)
        path = _cache_path(code)
        if path.exists():
            try:
                stale = json.loads(path.read_text(encoding="utf-8"))
                stale["stale"] = True
                return stale
            except Exception as cache_exc:
                log.warning("who_gho stale cache read failed for %s (%s); returning empty payload", code, cache_exc)
        return {"by_country": {}, "latest": {}, "fetched_at": time.time(), "error": str(exc)}

    shaped = _shape_rows(rows)
    payload = {
        "source": "who_gho",
        "code": code,
        "fetched_at": time.time(),
        **shaped,
    }
    with _lock:
        _write_cache(code, payload)
    log.info("WHO GHO %s: %d countries, %d rows", code, len(shaped["by_country"]), len(rows))
    return payload


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = fetch_indicator("WHOSIS_000002", force=True)
    print(f"Countries: {len(out['by_country'])}")
    print(f"USA latest: {out['latest'].get('USA')}")
    print(f"JPN latest: {out['latest'].get('JPN')}")

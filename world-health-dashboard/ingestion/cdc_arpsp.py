"""CDC Antibiotic Resistance Patient Safety Atlas (ARPSP) — C. auris.

Candida auris is an emerging multi-drug-resistant fungus driving healthcare
outbreaks in dozens of US states. The CDC publishes per-state, per-year, per-
antifungal resistance rates via the Socrata dataset `mdwz-ar4b`.

Endpoint:
    https://data.cdc.gov/resource/mdwz-ar4b.json

Returns rows like:
    {
        "antifungal_class": "Polyene",
        "drug": "Amphotericin B",
        "year": "2016",
        "state_abbreviation": "NY",                 # absent for US national rows
        "number_of_isolates": "25",
        "number_of_resistant_isolates": "19",
        "percent_resistant": "0.76",
        "region": "Northeast",
    }
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

API = "https://data.cdc.gov/resource/mdwz-ar4b.json"
CACHE_DIR = Path(__file__).parent.parent / "cache" / "cdc_arpsp"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_SECONDS = 24 * 3600

_lock = Lock()


def _cache_path() -> Path:
    return CACHE_DIR / "c_auris.json"


def _read_cache() -> dict | None:
    p = _cache_path()
    if not p.exists():
        return None
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
        if (time.time() - body.get("fetched_at", 0)) < CACHE_TTL_SECONDS:
            return body
    except Exception:
        return None
    return None


def _http_get(url: str, timeout: float = 30.0) -> object:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "world-health-dashboard/0.4",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted)
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _safe_float(x) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (ValueError, TypeError):
        return None


def fetch(force: bool = False) -> dict:
    """Return shaped C. auris resistance data:
    {
        "national": [{year, drug, antifungal_class, isolates, percent_resistant}, ...],
        "by_state": {"NY": [{...}, ...]},
        "drugs": [{class, drug}, ...],
        "fetched_at": <epoch>,
    }
    """
    with _lock:
        if not force:
            cached = _read_cache()
            if cached:
                return cached

    qs = urllib.parse.urlencode({"$limit": 5000})
    try:
        rows = _http_get(f"{API}?{qs}")
    except Exception as exc:
        log.warning("CDC ARPSP fetch failed: %s", exc)
        p = _cache_path()
        if p.exists():
            try:
                stale = json.loads(p.read_text(encoding="utf-8"))
                stale["stale"] = True
                stale["error"] = str(exc)
                return stale
            except Exception as cache_exc:
                log.warning("cdc_arpsp stale cache read failed (%s); returning empty payload", cache_exc)
        return {"national": [], "by_state": {}, "fetched_at": time.time(), "error": str(exc)}

    if not isinstance(rows, list):
        rows = []

    national: list[dict] = []
    by_state: dict[str, list[dict]] = {}
    drugs_seen: set[tuple[str, str]] = set()

    for r in rows:
        drug = r.get("drug")
        clazz = r.get("antifungal_class")
        if drug and clazz:
            drugs_seen.add((clazz, drug))
        try:
            year = int(r.get("year")) if r.get("year") else None
        except (ValueError, TypeError):
            year = None
        rec = {
            "year": year,
            "drug": drug,
            "antifungal_class": clazz,
            "isolates": int(r.get("number_of_isolates") or 0),
            "resistant_isolates": int(r.get("number_of_resistant_isolates") or 0),
            "percent_resistant": _safe_float(r.get("percent_resistant")),
            "region": r.get("region"),
        }
        state = r.get("state_abbreviation")
        if state:
            by_state.setdefault(state, []).append(rec)
        else:
            national.append(rec)

    # Sort everything by (year, drug)
    national.sort(key=lambda x: (x.get("year") or 0, x.get("drug") or ""))
    for st in by_state.values():
        st.sort(key=lambda x: (x.get("year") or 0, x.get("drug") or ""))

    payload = {
        "source": "CDC ARPSP",
        "pathogen": "Candida auris",
        "national": national,
        "by_state": by_state,
        "drugs": [{"class": c, "drug": d} for c, d in sorted(drugs_seen)],
        "states_count": len(by_state),
        "fetched_at": time.time(),
        "stale": False,
    }
    try:
        _cache_path().write_text(json.dumps(payload), encoding="utf-8")
    except Exception as cache_exc:
        log.warning("cdc_arpsp cache write failed: %s", cache_exc)
    log.info("CDC ARPSP C. auris: %d national rows, %d states", len(national), len(by_state))
    return payload


def latest_summary() -> dict:
    """Latest-year national + state percent-resistant per drug."""
    payload = fetch()
    nat = payload.get("national", [])
    if not nat:
        return {"national": {}, "by_state": {}, "fetched_at": payload.get("fetched_at")}

    latest_year = max((r["year"] for r in nat if r.get("year") is not None), default=None)
    nat_latest = [r for r in nat if r.get("year") == latest_year]

    by_state_latest: dict[str, list[dict]] = {}
    for state, rows in payload.get("by_state", {}).items():
        latest_state = [r for r in rows if r.get("year") == latest_year]
        if latest_state:
            by_state_latest[state] = latest_state

    return {
        "year": latest_year,
        "national": {r["drug"]: r for r in nat_latest},
        "by_state": by_state_latest,
        "fetched_at": payload.get("fetched_at"),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    s = latest_summary()
    print(f"latest year: {s['year']}, states: {len(s['by_state'])}")
    print("national resistance (latest year):")
    for drug, r in s["national"].items():
        pct = r.get("percent_resistant")
        print(f"  {drug:30s} {(pct * 100 if pct else 0):5.1f}%   isolates={r.get('isolates')}")

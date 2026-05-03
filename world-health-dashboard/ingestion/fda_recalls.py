"""FDA drug enforcement (recalls) — openFDA endpoint.

`https://api.fda.gov/drug/enforcement.json` — every drug recall the FDA has
posted, with classification (Class I = most serious, III = least),
recall reason, recalling firm, dates, distribution. We scope to recent
(last 5 years) drug recalls to assess each drug's manufacturing-quality
track record.

Per-drug query: `openfda.generic_name:"<name>"`. Cached 24h per generic.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

log = logging.getLogger(__name__)

API = "https://api.fda.gov/drug/enforcement.json"
CACHE_DIR = Path(__file__).parent.parent / "cache" / "fda_recalls"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_SECONDS = 24 * 3600

_lock = Lock()


def _cache_path(generic: str) -> Path:
    safe = "".join(c if c.isalnum() else "_" for c in generic.lower()).strip("_")[:80]
    return CACHE_DIR / f"{safe}.json"


def _read_cache(generic: str) -> dict | None:
    p = _cache_path(generic)
    if not p.exists():
        return None
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
        if (time.time() - body.get("fetched_at", 0)) < CACHE_TTL_SECONDS:
            return body.get("data")
    except Exception:
        return None
    return None


def _write_cache(generic: str, data: dict) -> None:
    try:
        _cache_path(generic).write_text(
            json.dumps({"fetched_at": time.time(), "data": data}),
            encoding="utf-8",
        )
    except Exception:
        pass


def _http_get(params: dict, timeout: float = 20.0) -> dict | None:
    qs = urllib.parse.urlencode(params)
    url = f"{API}?{qs}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "world-health-dashboard/0.4",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted)
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"results": [], "meta": {"results": {"total": 0}}}
        log.warning("openFDA recall GET failed (%s)", e.code)
        return None
    except Exception as exc:
        log.warning("openFDA recall GET failed: %s", exc)
        return None


def _years_ago(date_str: str) -> float | None:
    """openFDA dates are YYYYMMDD strings; return age in years."""
    if not date_str or len(date_str) != 8:
        return None
    try:
        dt = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days / 365.25
    except ValueError:
        return None


def lookup(generic: str, max_age_years: float = 5.0, force: bool = False) -> dict:
    """Recalls for `generic` within `max_age_years` of now."""
    if not force:
        cached = _read_cache(generic)
        if cached:
            return cached

    queries = [
        f'openfda.generic_name:"{generic}"',
        f'openfda.substance_name:"{generic}"',
    ]
    results: list[dict] = []
    total = 0
    for q in queries:
        data = _http_get({"search": q, "limit": 100})
        if data and data.get("results"):
            results = data["results"]
            total = (data.get("meta") or {}).get("results", {}).get("total", len(results))
            break

    recent: list[dict] = []
    by_class: dict[str, int] = {"Class I": 0, "Class II": 0, "Class III": 0}
    by_firm: dict[str, int] = {}

    for r in results:
        date_str = r.get("recall_initiation_date") or ""
        age = _years_ago(date_str)
        if age is None or age > max_age_years:
            continue
        clsf = (r.get("classification") or "").strip()
        by_class[clsf] = by_class.get(clsf, 0) + 1
        firm = (r.get("recalling_firm") or "").strip()
        if firm:
            by_firm[firm] = by_firm.get(firm, 0) + 1
        recent.append({
            "date":       date_str,
            "firm":       firm,
            "product":    (r.get("product_description") or "")[:200],
            "reason":     (r.get("reason_for_recall") or "")[:300],
            "classification": clsf,
            "status":     r.get("status"),
            "country":    r.get("country"),
        })
    recent.sort(key=lambda x: x["date"], reverse=True)

    out = {
        "generic": generic,
        "total_recalls_alltime": total,
        "recent_recalls": recent[:20],
        "recent_count": len(recent),
        "by_classification": by_class,
        "by_firm": dict(sorted(by_firm.items(), key=lambda kv: -kv[1])[:10]),
        "lookback_years": max_age_years,
    }
    _write_cache(generic, out)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for d in ("metformin", "ibuprofen", "vincristine", "albuterol", "artemether"):
        r = lookup(d)
        print(f"  {d:15s} alltime={r['total_recalls_alltime']:5d}  "
              f"recent5y={r['recent_count']:3d}  "
              f"classI={r['by_classification']['Class I']}  "
              f"top_firm={list(r['by_firm'].keys())[:1]}")

"""openFDA drug labels — for manufacturer enumeration.

`https://api.fda.gov/drug/label.json` returns labels submitted to the FDA in
SPL (Structured Product Labeling) format. Each label has an `openfda` block
with normalized fields including `manufacturer_name`, `brand_name`, RxCUI,
and the substances. We use this to count *distinct manufacturers per drug*,
which is the primary supply-chain-resilience signal:

  • 1 manufacturer  →  single-source, fragile
  • 5+ manufacturers →  resilient (generic competition)

Our queries search by `openfda.generic_name` or `openfda.substance_name`. We
cap each drug at 100 labels (the API max page) — when a drug has 100+ labels
that itself signals a deeply commoditized supply.
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

API = "https://api.fda.gov/drug/label.json"
CACHE_DIR = Path(__file__).parent.parent / "cache" / "openfda_drugs"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_SECONDS = 7 * 24 * 3600  # labels rarely change; weekly is plenty

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
    except Exception as exc:
        log.warning("openfda_drugs cache write failed for %s: %s", generic, exc)


def _http_get(params: dict, timeout: float = 20.0) -> dict | None:
    qs = urllib.parse.urlencode(params)
    url = f"{API}?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "world-health-dashboard/0.4",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted)
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        # 404 = no matches (legitimate when a generic isn't in FDA labels).
        if e.code == 404:
            return {"results": [], "meta": {"results": {"total": 0}}}
        log.warning("openFDA label GET failed (%s): %s", e.code, e)
        return None
    except Exception as exc:
        log.warning("openFDA label GET failed: %s", exc)
        return None


def _normalize_strings(values) -> list[str]:
    """openFDA returns most fields as lists of strings — normalize to a flat
    sorted set of unique strings."""
    out: set[str] = set()
    if isinstance(values, list):
        for v in values:
            if isinstance(v, str) and v.strip():
                out.add(v.strip())
    elif isinstance(values, str) and values.strip():
        out.add(values.strip())
    return sorted(out)


def lookup(generic: str, force: bool = False) -> dict:
    """Return manufacturer / brand / RxCUI / NDC summary for a generic."""
    if not force:
        cached = _read_cache(generic)
        if cached:
            return cached

    # Try generic_name first, fall back to substance_name (handles INN→USAN
    # synonyms like paracetamol→acetaminophen).
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

    manufacturers: set[str] = set()
    brands: set[str] = set()
    rxcuis: set[str] = set()
    substances: set[str] = set()
    routes: set[str] = set()
    pharm_class: set[str] = set()
    application_numbers: set[str] = set()

    for r in results:
        of = r.get("openfda") or {}
        for m in _normalize_strings(of.get("manufacturer_name")):
            manufacturers.add(m)
        for b in _normalize_strings(of.get("brand_name")):
            brands.add(b)
        for x in _normalize_strings(of.get("rxcui")):
            rxcuis.add(x)
        for s in _normalize_strings(of.get("substance_name")):
            substances.add(s)
        for rt in _normalize_strings(of.get("route")):
            routes.add(rt)
        for pc in _normalize_strings(of.get("pharm_class_epc") or of.get("pharm_class")):
            pharm_class.add(pc)
        for an in _normalize_strings(of.get("application_number")):
            application_numbers.add(an)

    out = {
        "generic": generic,
        "total_labels": total,
        "label_results_returned": len(results),
        "manufacturers": sorted(manufacturers),
        "manufacturer_count": len(manufacturers),
        "brands": sorted(brands)[:30],
        "rxcuis": sorted(rxcuis),
        "substances": sorted(substances),
        "routes": sorted(routes),
        "pharm_class": sorted(pharm_class),
        "application_numbers": sorted(application_numbers),
    }
    _write_cache(generic, out)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for d in ("metformin", "ibuprofen", "vincristine", "semaglutide", "albuterol", "artemether", "isoniazid", "dolutegravir"):
        r = lookup(d)
        print(
            f"  {d:18s} labels={r['total_labels']:5d}  manufacturers={r['manufacturer_count']:3d}  brands={len(r['brands']):3d}  top_mfr={(r['manufacturers'][0] if r['manufacturers'] else '—')[:35]}"
        )

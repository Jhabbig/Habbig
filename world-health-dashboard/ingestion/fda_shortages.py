"""FDA Drug Shortages — openFDA endpoint.

`https://api.fda.gov/drug/shortages.json` lists every drug ever flagged as
in shortage, with status (Current / Resolved), reason, dates, and the
companies involved. Total dataset is ~1,700 entries; we pull the full set
once and group by generic name for fast lookups.

Key signals for the supply-chain weak-point heuristic:
  • currently in shortage (Current status)
  • historical shortage count (how often this drug has bottlenecked)
  • shortage_reason (manufacturing / demand / discontinuation)
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from threading import Lock

log = logging.getLogger(__name__)

API = "https://api.fda.gov/drug/shortages.json"
CACHE_DIR = Path(__file__).parent.parent / "cache" / "fda_shortages"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_SECONDS = 6 * 3600

_lock = Lock()


def _cache_path() -> Path:
    return CACHE_DIR / "shortages.json"


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


def _http_get(params: dict, timeout: float = 30.0) -> dict | None:
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
    except Exception as exc:
        log.warning("FDA shortages GET failed: %s", exc)
        return None


def _normalize_name(s: str | None) -> str:
    """Lowercase, strip dosage-form suffixes / packaging notes."""
    if not s:
        return ""
    s = s.lower()
    # Drop trailing dosage forms / strengths / packaging
    s = re.sub(r"\b(tablet|capsule|injection|solution|suspension|powder|cream|ointment|gel|drops|inhaler|spray|syrup|elixir|patch|implant)s?\b.*$", "", s)
    s = re.sub(r"\b(oral|iv|im|sc|sublingual|topical|nasal|otic|ophthalmic|rectal|vaginal)\b", "", s)
    s = re.sub(r"\d+\s*(mg|mcg|g|ml|%|iu|units?)\b", "", s)
    s = re.sub(r"[(),\[\]/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.;:-")
    return s


def fetch(force: bool = False) -> dict:
    """Pull all shortage entries (paginated) and group by generic name."""
    with _lock:
        if not force:
            cached = _read_cache()
            if cached:
                return cached

    all_rows: list[dict] = []
    skip = 0
    page_size = 1000
    for _ in range(10):
        data = _http_get({"limit": page_size, "skip": skip})
        if not data:
            break
        rs = data.get("results", []) or []
        if not rs:
            break
        all_rows.extend(rs)
        total = (data.get("meta") or {}).get("results", {}).get("total", 0)
        skip += len(rs)
        if skip >= total or len(rs) < page_size:
            break

    # Group by normalized generic name. Same generic may have many
    # shortage entries (different presentations, different companies).
    by_generic: dict[str, list[dict]] = {}
    by_brand: dict[str, list[dict]] = {}
    for r in all_rows:
        gn_raw = r.get("generic_name") or ""
        bn_raw = r.get("proprietary_name") or ""
        # Index by every word-token of the generic name (handles combos)
        gn_norm = _normalize_name(gn_raw)
        if gn_norm:
            for tok in gn_norm.split():
                if len(tok) >= 4:
                    by_generic.setdefault(tok, []).append(r)
        bn_norm = _normalize_name(bn_raw)
        if bn_norm:
            for tok in bn_norm.split():
                if len(tok) >= 4:
                    by_brand.setdefault(tok, []).append(r)

    payload = {
        "source": "openFDA Drug Shortages",
        "all_entries": all_rows,
        "by_generic_token": by_generic,
        "by_brand_token": by_brand,
        "total_entries": len(all_rows),
        "current_count": sum(1 for r in all_rows if (r.get("status") or "").lower() == "current"),
        "fetched_at": time.time(),
    }
    try:
        _cache_path().write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:
        log.warning("fda_shortages cache write failed: %s", exc)
    log.info("FDA shortages: %d total, %d current", payload["total_entries"], payload["current_count"])
    return payload


def for_drug(generic_name: str) -> list[dict]:
    """All shortage entries matching a given generic name (token overlap)."""
    payload = fetch()
    by_token = payload.get("by_generic_token", {})
    by_brand = payload.get("by_brand_token", {})
    norm = _normalize_name(generic_name)
    seen_ids: set[int] = set()  # use python id() of dict for dedup
    out: list[dict] = []
    for tok in norm.split():
        if len(tok) < 4:
            continue
        for r in by_token.get(tok, []) + by_brand.get(tok, []):
            rid = id(r)
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            out.append(r)
    # Sort by status (Current first) then by update date desc.
    out.sort(
        key=lambda r: (
            0 if (r.get("status") or "").lower() == "current" else 1,
            -_date_key(r.get("update_date") or r.get("initial_posting_date") or ""),
        )
    )
    return out


def _date_key(s: str) -> int:
    """openFDA dates are "MM/DD/YYYY"; convert to YYYYMMDD int for sort."""
    if not s:
        return 0
    parts = s.split("/")
    if len(parts) == 3:
        try:
            mm, dd, yy = parts
            return int(yy) * 10000 + int(mm) * 100 + int(dd)
        except ValueError:
            return 0
    return 0


def current_shortages() -> list[dict]:
    """Drugs currently in shortage."""
    payload = fetch()
    return [r for r in payload.get("all_entries", []) if (r.get("status") or "").lower() == "current"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    p = fetch(force=True)
    print(f"total entries: {p['total_entries']}")
    print(f"currently in shortage: {p['current_count']}")
    for drug in ("metformin", "amoxicillin", "vincristine", "amphotericin", "albuterol"):
        ents = for_drug(drug)
        cur = [e for e in ents if (e.get("status") or "").lower() == "current"]
        print(f"  {drug:18s} entries={len(ents):3d}  current={len(cur)}")
        for e in cur[:2]:
            print(f"    [CUR] {e.get('generic_name', '')[:40]:40s} {e.get('company_name', '')[:30]}")

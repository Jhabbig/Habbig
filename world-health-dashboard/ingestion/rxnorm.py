"""RxNorm brand ↔ generic resolver.

The US National Library of Medicine's RxNav API (no key required) maps
generic drug names to all brand-name equivalents. We resolve each generic in
our EML to the set of brand names patients might recognize ('semaglutide' →
'Ozempic, Wegovy, Rybelsus', etc.) so the disease atlas can show both.

Endpoint:
  https://rxnav.nlm.nih.gov/REST/drugs.json?name=<generic>

Response shape:
  {"drugGroup": {"name": ..., "conceptGroup": [{"tty": "SBD",
     "conceptProperties": [{"name": "ibuprofen 200 MG Oral Tablet [Proprinal]"},
     ...]}]}}

Brand names are bracketed in the SBD/BPCK names — we extract '[BrandName]'.
The data is slow-changing (generics don't get new brands daily); cache for
30 days.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

log = logging.getLogger(__name__)

API = "https://rxnav.nlm.nih.gov/REST"
CACHE_DIR = Path(__file__).parent.parent / "cache" / "rxnorm"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

CACHE_TTL_SECONDS = 30 * 24 * 3600
_lock = Lock()

# Drugs that are vaccines / not RxNorm-mappable cleanly. Skip these to avoid
# wasting API calls; the EML notes column is descriptive enough.
SKIP_PREFIXES = (
    "rts,", "r21/", "bcg ", "mmr ", "hpv ", "men", "qdenga", "yf-vax",
    "covid-19 vaccines", "seasonal influenza vaccine", "rotateq",
    "rotarix", "shanchol", "dukoral", "stamaril", "ervebo", "jynneos",
    "rvsv", "mva-bn", "typbar",
)


def _cache_path(name: str) -> Path:
    safe = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return CACHE_DIR / f"{safe[:80]}.json"


def _read_cache(name: str) -> dict | None:
    p = _cache_path(name)
    if not p.exists():
        return None
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
        if (time.time() - body.get("fetched_at", 0)) < CACHE_TTL_SECONDS:
            return body.get("data")
    except Exception:
        return None
    return None


def _write_cache(name: str, data: dict) -> None:
    try:
        _cache_path(name).write_text(
            json.dumps({"fetched_at": time.time(), "data": data}),
            encoding="utf-8",
        )
    except Exception:
        pass


def _http_get(path: str, params: dict, timeout: float = 15.0) -> dict | None:
    qs = urllib.parse.urlencode(params)
    url = f"{API}{path}?{qs}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "world-health-dashboard/0.4",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted)
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        log.warning("RxNorm GET %s failed: %s", path, exc)
        return None


_BRAND_RE = re.compile(r"\[([^\]]+)\]")


def _normalize_query(generic: str) -> str:
    """RxNorm's name search is case-insensitive but expects single tokens.
    Strip parentheticals and trailing notes for the lookup."""
    g = generic.strip()
    # 'paracetamol' may not return matches in RxNorm (US uses 'acetaminophen')
    aliases = {
        "paracetamol": "acetaminophen",
        "salbutamol": "albuterol",
        "rifampicin": "rifampin",
    }
    g_low = g.lower()
    if g_low in aliases:
        return aliases[g_low]
    # Strip qualifiers in parens.
    g = re.sub(r"\s*\([^)]+\)", "", g).strip()
    # Strip trailing dosage forms.
    g = re.sub(
        r"\s+(LA|XR|SR|ER|IR|XL)\b", "", g, flags=re.IGNORECASE
    ).strip()
    # Drop combos: 'a-b' -> just 'a' for the query (we'll find combo SBDs anyway)
    if "-" in g and " " not in g:
        g = g.split("-")[0]
    return g


def resolve(generic: str, force: bool = False) -> dict:
    """Return {generic, query, brands: [...], rxcuis: [...], scd_count, sbd_count}."""
    if any(generic.lower().startswith(p) for p in SKIP_PREFIXES):
        return {"generic": generic, "query": generic, "brands": [], "rxcuis": [],
                "scd_count": 0, "sbd_count": 0, "skipped": True}

    if not force:
        cached = _read_cache(generic)
        if cached:
            return cached

    query = _normalize_query(generic)
    data = _http_get("/drugs.json", {"name": query})
    if not data:
        out = {"generic": generic, "query": query, "brands": [], "rxcuis": [],
               "scd_count": 0, "sbd_count": 0, "error": "fetch_failed"}
        _write_cache(generic, out)
        return out

    drug_group = data.get("drugGroup") or {}
    concept_groups = drug_group.get("conceptGroup") or []
    brands: set[str] = set()
    rxcuis: set[str] = set()
    scd_count = 0
    sbd_count = 0

    for cg in concept_groups:
        tty = cg.get("tty") or ""
        for cp in (cg.get("conceptProperties") or []):
            name = cp.get("name") or ""
            rxcui = cp.get("rxcui") or ""
            if rxcui:
                rxcuis.add(rxcui)
            if tty in ("SBD", "BPCK"):
                sbd_count += 1
                m = _BRAND_RE.search(name)
                if m:
                    brand = m.group(1).strip()
                    # Filter obvious junk like single-letter brands
                    if len(brand) >= 2 and brand.lower() != generic.lower():
                        brands.add(brand)
            elif tty in ("SCD", "GPCK"):
                scd_count += 1

    out = {
        "generic": generic,
        "query": query,
        "brands": sorted(brands)[:20],
        "rxcuis": sorted(rxcuis)[:20],
        "scd_count": scd_count,
        "sbd_count": sbd_count,
    }
    _write_cache(generic, out)
    return out


def resolve_many(generics: list[str], max_workers: int = 8) -> dict[str, dict]:
    """Concurrent batch resolution; respects RxNorm rate limit."""
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for g, r in zip(generics, pool.map(resolve, generics)):
            out[g] = r
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for g in ("ibuprofen", "semaglutide", "metformin", "amoxicillin",
              "artemether-lumefantrine", "paracetamol", "salbutamol"):
        r = resolve(g)
        brands = ", ".join(r["brands"][:6])
        print(f"  {g:35s} → {len(r['brands'])} brands: {brands}")

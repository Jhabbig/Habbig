"""Polymarket sentiment-market matcher.

Pulls active Polymarket markets that proxy how voters feel about the country
and the people running it:

  - Direction of country / right track / wrong track
  - Presidential approval rating (above/below thresholds)
  - Congress / Speaker approval
  - Recession / economy outlook (consumer-facing)

Source: Polymarket Gamma API (`gamma-api.polymarket.com/markets`). Public,
no key. We cache 5 min — prices move continuously but more often than that
just hammers their API.

Rule-based matching (per project policy): keep only markets whose question
mentions one of a known sentiment-relevant phrase set. We don't try to do
LLM classification here; the upside isn't worth the dependency.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from threading import Lock

log = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
_UA = "voter-pulse-dashboard/0.1"

# Tag slugs to scan. Polymarket uses slugs to bucket markets; these are the
# ones most likely to contain voter-mood markets without sweeping in noise.
TAG_SLUGS = [
    "us-current-affairs",
    "politics",
    "us-politics",
    "elections",
    "approval",
    "economy",
]

# Categories we sort each matched market into, in priority order. The first
# regex that matches wins.
CATEGORIES: list[tuple[str, re.Pattern]] = [
    ("approval",   re.compile(r"\bapproval\b|\bapprove\b", re.I)),
    ("right_track", re.compile(r"\b(right track|wrong track|direction of (the )?country)\b", re.I)),
    ("recession", re.compile(r"\brecession\b", re.I)),
    ("inflation", re.compile(r"\binflation\b|\bcpi\b", re.I)),
    ("unemployment", re.compile(r"\b(unemployment|jobless|jobs report|payrolls)\b", re.I)),
    ("election",  re.compile(r"\b(win|wins|elected|election|nominee|primary)\b", re.I)),
]

# Markets we explicitly do NOT want even if they hit a tag (sports, crypto,
# weather names that can leak through politics tags).
_REJECT_RX = re.compile(r"\b(super bowl|nba|nfl|world cup|bitcoin|ethereum|hurricane)\b", re.I)


def _categorise(question: str) -> str | None:
    if not question or _REJECT_RX.search(question):
        return None
    for cat, rx in CATEGORIES:
        if rx.search(question):
            return cat
    return None


def _parse_yes_price(market: dict) -> float | None:
    raw = market.get("outcomePrices")
    if not raw:
        return None
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        if not prices:
            return None
        return float(prices[0])
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def _parse_volume(market: dict) -> float:
    for k in ("volume24hr", "volumeNum", "volume24Hr", "volume"):
        v = market.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (ValueError, TypeError):
            continue
    return 0.0


def _fetch_tag(slug: str, limit: int = 100) -> list[dict]:
    params = {
        "closed": "false",
        "active": "true",
        "limit": str(limit),
        "tag_slug": slug,
    }
    url = f"{GAMMA}/markets?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.warning("Polymarket fetch failed for tag=%s: %s", slug, exc)
        return []


def fetch_sentiment_markets() -> list[dict]:
    """Return matched, deduped, sorted-by-volume sentiment markets."""
    seen: set[str] = set()
    out: list[dict] = []
    for slug in TAG_SLUGS:
        for m in _fetch_tag(slug):
            mid = str(m.get("id") or m.get("slug") or "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            q = m.get("question") or ""
            cat = _categorise(q)
            if not cat:
                continue
            yes = _parse_yes_price(m)
            if yes is None:
                continue
            volume = _parse_volume(m)
            out.append({
                "id": mid,
                "category": cat,
                "question": q,
                "yes_price": yes,
                "volume_24h": volume,
                "end_date": m.get("endDate") or m.get("end_date_iso"),
                "url": f"https://polymarket.com/event/{m.get('slug', '')}" if m.get("slug") else None,
            })
    out.sort(key=lambda r: r["volume_24h"], reverse=True)
    return out


# ── Cache ────────────────────────────────────────────────────────────────────
_CACHE: dict = {"data": [], "fetched_at": 0.0}
_CACHE_TTL = 5 * 60
_lock = Lock()


def get_cached(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        fresh = (now - _CACHE["fetched_at"]) < _CACHE_TTL and _CACHE["data"] is not None
        if fresh and not force and _CACHE["fetched_at"]:
            return {"markets": _CACHE["data"], "fetched_at": _CACHE["fetched_at"]}
    markets = fetch_sentiment_markets()
    with _lock:
        _CACHE["data"] = markets
        _CACHE["fetched_at"] = now
    return {"markets": markets, "fetched_at": now}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    res = get_cached(force=True)
    print(f"{len(res['markets'])} markets")
    for m in res["markets"][:20]:
        print(f"[{m['category']:13s}] yes={m['yes_price']:.2f} vol={m['volume_24h']:>10,.0f}  {m['question'][:80]}")

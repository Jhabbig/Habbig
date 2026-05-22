"""Kalshi FOMC-market matcher (read-only, public API, no auth).

Pulls open FOMC-related events + their nested markets from Kalshi's
``/trade-api/v2/events`` endpoint, classifies each market into the same
bucket vocabulary as :mod:`polymarket_client` (``cut25`` / ``hold`` /
``hike25`` / …), and returns the YES price + volume + a deep-link URL.

Why a separate client (vs reusing the gateway's existing top-traders
``kalshi_client.py``):
  - That module uses ``httpx``; this dashboard is stdlib-only for ingestion.
  - We only need a narrow FOMC slice, not the whole top-of-volume scan.
  - Trade-out URLs need to point at Kalshi's *event* page when there are
    multiple sub-markets, not the individual market — keeps users on a
    single page where they see all rate buckets and pick one.

Authentication — the public ``/events`` and ``/markets`` endpoints don't
need auth. Order placement (Phase 2) needs RSA-PSS signing with each user's
own API key + private key; out of scope for this v0.5 read-only build.

Cache: 5 min — Kalshi prices move continuously but more often just hammers
their rate limit.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from threading import Lock

from . import outcome_classifier

log = logging.getLogger(__name__)

KALSHI_HOST = "https://api.elections.kalshi.com"
KALSHI_API = "/trade-api/v2"
_UA = "centralbank-dashboard/0.5"

# Title-level filter — defensive layer; the series filter below should already
# narrow to FOMC events, but Kalshi's event taxonomy moves around occasionally.
_FED_RX = __import__("re").compile(
    r"\b(fed|fomc|federal reserve|federal funds|fed funds|interest rate)\b", __import__("re").IGNORECASE,
)

# Kalshi series tickers we'll query (union, not first-hit). Coverage notes:
#   - KXFEDDECISION  — cleanest per-bucket markets (cut25 / hike25 / hike0 / etc.)
#                      As of 2026-04 these contracts are listed but largely
#                      illiquid until ~1-2 weeks before each meeting.
#   - KXFEDCOMBO     — combined "rate + dissent count" combo markets
#   - KXBOE          — Bank of England bank rate (future expansion)
#   - KXEZDEPRATE    — ECB deposit facility rate (future expansion)
# Only KXFEDDECISION + KXFEDCOMBO matter for the FOMC view in v0.5; the others
# are listed for when we extend implied/edge to ECB and BoE.
_FED_SERIES_TICKERS = ("KXFEDDECISION", "KXFEDCOMBO")

_CACHE: dict = {"data": None, "fetched_at": 0.0, "key": None}
_CACHE_TTL = 5 * 60
_lock = Lock()


def _http_get(path: str, params: dict | None = None, timeout: float = 15.0) -> dict | None:
    if params:
        url = f"{KALSHI_HOST}{path}?" + urllib.parse.urlencode(params)
    else:
        url = f"{KALSHI_HOST}{path}"
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        log.warning("Kalshi GET %s failed: %s", url, exc)
        return None


def _normalize_price(p) -> float | None:
    """Kalshi v2 prices arrive either as 0–1 dollar floats or as 1–99 cents.
    Normalize to 0–1 probability. Returns None on any failure."""
    if p is None:
        return None
    try:
        v = float(p)
    except (TypeError, ValueError):
        return None
    if v > 1.0:
        v = v / 100.0
    if v < 0 or v > 1:
        return None
    return round(v, 4)


def _yes_price(market: dict) -> float | None:
    """Pick the best YES price reading. Prefer last_price (most recent trade);
    fall back to mid of yes_bid/yes_ask; finally yes_bid alone."""
    last = _normalize_price(market.get("last_price"))
    if last is not None and last > 0:
        return last
    bid = _normalize_price(market.get("yes_bid"))
    ask = _normalize_price(market.get("yes_ask"))
    if bid is not None and ask is not None and ask > 0:
        return round((bid + ask) / 2, 4)
    return bid


def _market_url(ticker: str | None, event_ticker: str | None) -> str | None:
    """Deep link to Kalshi's UI. Prefer the event page (groups all rate
    buckets) so the user lands on the full FOMC outcome ladder, not a single
    sub-market. Falls back to the market-page URL if no event ticker."""
    if event_ticker:
        return f"https://kalshi.com/events/{event_ticker.lower()}"
    if ticker:
        return f"https://kalshi.com/markets/{ticker.lower()}"
    return None


def _fetch_events_for_series(series_ticker: str, status: str = "open") -> list[dict]:
    """Try to fetch events under a series ticker. Returns [] on failure."""
    payload = _http_get(
        f"{KALSHI_API}/events",
        params={
            "series_ticker": series_ticker,
            "status": status,
            "with_nested_markets": "true",
            "limit": 50,
        },
    )
    if not payload or "events" not in payload:
        return []
    return payload["events"]


def _filter_events_by_meeting(events: list[dict], meeting_date: date, window_days: int = 7) -> list[dict]:
    """Keep events whose any nested market closes inside the FOMC window.
    Kalshi events often span several rate buckets that all settle the same day."""
    end_min = datetime.combine(meeting_date, datetime.min.time(), tzinfo=timezone.utc)
    end_max = datetime.combine(
        meeting_date + timedelta(days=window_days), datetime.max.time(), tzinfo=timezone.utc,
    )
    out: list[dict] = []
    for ev in events:
        title = ev.get("title", "") + " " + ev.get("sub_title", "")
        if not _FED_RX.search(title):
            continue
        # Probe the close-time of the first nested market — a single event's
        # markets all settle together for FOMC questions.
        markets = ev.get("markets") or []
        if not markets:
            continue
        try:
            close = datetime.fromisoformat(markets[0]["close_time"].replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError):
            continue
        if end_min <= close <= end_max:
            out.append(ev)
    return out


def match_fomc_markets(meeting_date: date, current_rate: float | None) -> list[dict]:
    """Return matched Kalshi markets with bucket, price, volume, URL.

    ``current_rate`` (in percent) is needed to classify level-style Kalshi
    questions like "Fed funds rate at 4.25%-4.50%". Pass ``None`` when
    unavailable — only delta-style questions will match.
    """
    # Union across all known FOMC-related series tickers — cover both the
    # per-step KXFEDDECISION markets and the KXFEDCOMBO combos.
    events: list[dict] = []
    for series in _FED_SERIES_TICKERS:
        chunk = _fetch_events_for_series(series)
        if chunk:
            log.info("Kalshi: %d events under series_ticker=%s", len(chunk), series)
            events.extend(chunk)

    if not events:
        log.warning("Kalshi: no events found under any known FOMC series ticker")
        return []

    fomc_events = _filter_events_by_meeting(events, meeting_date)
    if not fomc_events:
        return []

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for ev in fomc_events:
        ev_ticker = ev.get("event_ticker") or ev.get("ticker") or ""
        for m in ev.get("markets") or []:
            if m.get("status") not in (None, "open", "active", "initialized"):
                # Defensive — sometimes events list closed sub-markets too.
                continue
            title = m.get("title") or m.get("yes_sub_title") or ""
            subtitle = m.get("subtitle") or m.get("yes_sub_title") or ""
            text = f"{title} {subtitle}".strip()
            bucket = outcome_classifier.classify(text, current_rate)
            if not bucket:
                continue
            price = _yes_price(m)
            if price is None:
                continue
            ticker = m.get("ticker") or ""
            key = (bucket, ticker)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "outcome_bucket": bucket,
                "question": text,
                "kalshi_price": price,
                "kalshi_yes_bid": _normalize_price(m.get("yes_bid")),
                "kalshi_yes_ask": _normalize_price(m.get("yes_ask")),
                "volume_24h": float(m.get("volume_24h") or 0),
                "open_interest": int(m.get("open_interest") or 0),
                "url": _market_url(ticker, ev_ticker),
                "ticker": ticker,
                "event_ticker": ev_ticker,
                "close_time": m.get("close_time"),
            })
    out.sort(key=lambda x: x["volume_24h"], reverse=True)
    return out


def get_cached_for_meeting(meeting_date: date, current_rate: float | None, force: bool = False) -> list[dict]:
    now = time.time()
    key = f"{meeting_date.isoformat()}|{current_rate}"
    with _lock:
        fresh = (
            _CACHE["data"] is not None
            and _CACHE["key"] == key
            and (now - _CACHE["fetched_at"]) < _CACHE_TTL
        )
        if fresh and not force:
            return _CACHE["data"]
    data = match_fomc_markets(meeting_date, current_rate)
    with _lock:
        _CACHE["data"] = data
        _CACHE["fetched_at"] = now
        _CACHE["key"] = key
    return data


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from . import decision_calendar, fred_client
    cal = decision_calendar.upcoming(horizon_days=120)
    fomc = next((m for m in cal if m["cb"] == "US"), None)
    if not fomc:
        print("no upcoming FOMC")
        raise SystemExit(0)
    rates = fred_client.get_cached_rates()
    dff = next((s for s in rates["series"] if s["series_id"] == "DFF"), None)
    rate = dff["latest"][1] if dff and dff["latest"] else None
    md = date.fromisoformat(fomc["decision_date"])
    print(f"FOMC {md}, current rate={rate}")
    rows = get_cached_for_meeting(md, rate, force=True)
    print(f"matched {len(rows)} Kalshi markets")
    for r in rows[:10]:
        print(f"  {r['outcome_bucket']:8s}  ${r['kalshi_price']:.2f}  vol=${r['volume_24h']:>10,.0f}  "
              f"{r['question'][:60]}")

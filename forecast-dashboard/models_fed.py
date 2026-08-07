"""Fed rate-decision model — maps FOMC markets to outcome buckets (cut25 /
hold / hike25 ...) and prices them from the ZQ fed-funds-futures implied path.

Vendored (never imported) from centralbank-dashboard: the FOMC decision
calendar (ingestion/decision_calendar.py), the ZQ implied path (ingestion/
implied_path.py — Yahoo close fetch + _derive_probs), and the outcome bucket
classifier (ingestion/outcome_classifier.py).

Current DFF rate resolution order: FRED_API_KEY env (keyed FRED JSON API),
else FORECAST_DFF env override, else skip gracefully with a log line.

Row contract:
    {
        "market_uid": "<venue>:<venue_id>",
        "source": "fed_implied",
        "model_prob": float,        # 0..1
        "prob_method": "zq_interp",
        "detail": str,              # JSON: contract + implied path
    }

Only markets tied to the NEXT FOMC meeting are scored — the next-month ZQ
contract prices exactly that decision. Buckets from one decision sum to ~1.0
across that event's markets when all buckets are listed.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

# 2026 FOMC decision announcement dates. Verify against
# https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm — update annually.
FOMC_MEETINGS_2026: list[str] = [
    "2026-01-28",
    "2026-03-18",
    "2026-04-29",
    "2026-06-17",
    "2026-07-29",
    "2026-09-16",
    "2026-10-28",
    "2026-12-09",
]

MONTH_CODES = "FGHJKMNQUVXZ"  # CME/Globex: Jan=F ... Dec=Z

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
FRED_OBS = "https://api.stlouisfed.org/fred/series/observations"
# Yahoo blocks default client UAs; use a realistic browser string.
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)

_FED_RX = re.compile(r"\b(fomc|fed|federal\s+reserve|federal\s+funds|fed\s+funds)\b", re.I)


# ── outcome bucket classifier (vendored) ─────────────────────────────────────

_CUT_VERB = (
    r"(?:cut|cuts|cutting|decrease|decreases|decreased|"
    r"reduce|reduces|reduced|lower|lowers|lowered)"
)
_HIKE_VERB = (
    r"(?:hike|hikes|hiked|hiking|increase|increases|increased|"
    r"raise|raises|raised|raising)"
)
_VERB = f"(?:{_CUT_VERB}|{_HIKE_VERB})"
_BPS = r"(?:bps|bp|basis\s*points?)"

# `>N` / `more than N` marks range markets that aggregate several buckets —
# they need a cumulative comparison, so they're deliberately left unclassified.
_INEQ = r"(?:>|>=|≥|more\s+than|at\s+least)"
_VERB_BPS_RX = re.compile(rf"({_VERB})\b[^.\n]{{0,40}}?({_INEQ}\s*)?(\d{{1,3}})\s*{_BPS}", re.I)
_BPS_VERB_RX = re.compile(rf"({_INEQ}\s*)?(\d{{1,3}})\s*{_BPS}[^.\n]{{0,30}}?({_VERB})", re.I)
_HOLD_RX = re.compile(
    r"\b(hold|no change|unchanged|leave (?:rates? )?(?:the same|alone)|fed maintains rate)\b",
    re.I,
)
_CUT_RX = re.compile(_CUT_VERB, re.I)

_RANGE_RX = re.compile(r"(\d+\.\d{1,2})\s*%?\s*[-–to]+\s*(\d+\.\d{1,2})\s*%", re.I)
_SINGLE_RX = re.compile(r"(\d+\.\d{1,2})\s*%")


def classify_delta(text: str) -> str | None:
    q = text or ""
    if _HOLD_RX.search(q):
        return "hold"
    m = _VERB_BPS_RX.search(q)
    if m:
        verb, ineq, bps = m.group(1), m.group(2), int(m.group(3))
        if ineq:
            return None
        if bps == 0:
            return "hold"
        return f"{'cut' if _CUT_RX.fullmatch(verb) else 'hike'}{bps}"
    m = _BPS_VERB_RX.search(q)
    if m:
        ineq, bps, verb = m.group(1), int(m.group(2)), m.group(3)
        if ineq:
            return None
        if bps == 0:
            return "hold"
        return f"{'cut' if _CUT_RX.fullmatch(verb) else 'hike'}{bps}"
    return None


def classify_level(text: str, current_rate: float | None) -> str | None:
    """Level-style questions ("rate at 4.25%-4.50%"). The band's lower bound
    vs current_rate, rounded to the nearest 25 bp, gives the single-step
    bucket aligned with the delta vocabulary."""
    if current_rate is None or not text:
        return None
    text_l = text.lower()
    m = _RANGE_RX.search(text_l)
    if m:
        target = float(m.group(1))
    else:
        m = _SINGLE_RX.search(text_l)
        if not m:
            return None
        target = float(m.group(1))
    delta_bps = round((target - current_rate) * 100 / 25) * 25
    if delta_bps == 0:
        return "hold"
    return f"{'cut' if delta_bps < 0 else 'hike'}{abs(delta_bps)}"


def classify(text: str, current_rate: float | None = None) -> str | None:
    bucket = classify_delta(text)
    if bucket:
        return bucket
    return classify_level(text, current_rate)


# ── ZQ implied path (vendored) ───────────────────────────────────────────────


def ff_contract_symbol(year: int, month: int) -> str:
    """ZQ contract symbol on Yahoo, e.g. (2026, 5) -> 'ZQK26.CBT'."""
    return f"ZQ{MONTH_CODES[month - 1]}{year % 100:02d}.CBT"


def _next_month(d: date) -> tuple[int, int]:
    return (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)


def derive_probs(delta_pct: float, step_pct: float = 0.25) -> dict:
    """CME FedWatch-style linear interpolation across 25-bp steps.

    delta = -0.10 → {hold: 0.6, cut25: 0.4}; mass split between the two
    adjacent step buckets, so listed buckets always sum to 1."""
    steps_below = int(delta_pct // step_pct) if delta_pct >= 0 else -int((-delta_pct) // step_pct)
    lower_n = steps_below
    upper_n = steps_below + (1 if delta_pct >= 0 else -1)
    frac = abs((delta_pct - lower_n * step_pct) / step_pct)
    frac = max(0.0, min(1.0, frac))

    def label(n: int) -> str:
        if n == 0:
            return "hold"
        return f"{'hike' if n > 0 else 'cut'}{abs(n) * int(step_pct * 100)}"

    if abs(delta_pct - lower_n * step_pct) < 1e-9:
        return {label(lower_n): 1.0}
    return {label(lower_n): round(1 - frac, 3), label(upper_n): round(frac, 3)}


def _next_fomc(today: date | None = None) -> dict | None:
    today = today or datetime.now(timezone.utc).date()
    for ds in FOMC_MEETINGS_2026:
        if date.fromisoformat(ds) >= today:
            return {"decision_date": ds, "label": "FOMC"}
    return None


async def _fetch_yahoo_close(client: httpx.AsyncClient, symbol: str) -> float | None:
    try:
        resp = await client.get(
            YAHOO_CHART.format(symbol=symbol),
            headers={"User-Agent": _UA, "Accept": "application/json"},
        )
        if resp.status_code != 200:
            log.warning("Yahoo returned %d for %s", resp.status_code, symbol)
            return None
        result = resp.json()["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        for v in reversed(closes):
            if v is not None:
                return float(v)
    except Exception as e:
        log.warning("Yahoo fetch failed for %s: %s", symbol, e)
    return None


# ── current DFF rate ─────────────────────────────────────────────────────────


async def _fetch_fred_dff(client: httpx.AsyncClient, api_key: str) -> float | None:
    params = {
        "series_id": "DFF",
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": "5",
    }
    try:
        resp = await client.get(FRED_OBS, params=params)
        if resp.status_code != 200:
            log.warning("FRED returned %d for DFF", resp.status_code)
            return None
        for obs in resp.json().get("observations", []):
            v = obs.get("value")
            if v not in (None, ".", ""):
                return float(v)
    except Exception as e:
        log.warning("FRED DFF fetch failed: %s", e)
    return None


async def _current_dff(client: httpx.AsyncClient) -> float | None:
    api_key = os.getenv("FRED_API_KEY")
    if api_key:
        rate = await _fetch_fred_dff(client, api_key)
        if rate is not None:
            return rate
    override = os.getenv("FORECAST_DFF")
    if override:
        try:
            return float(override)
        except ValueError:
            log.warning("FORECAST_DFF is not a number: %r", override)
    return None


# ── entry point ──────────────────────────────────────────────────────────────


def _matches_meeting(question: str, end_date_iso: str | None, meeting: date) -> bool:
    if _MONTH_NAMES[meeting.month - 1] in (question or "").lower():
        return True
    if end_date_iso:
        try:
            end = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00")).date()
        except (ValueError, AttributeError):
            return False
        return meeting - timedelta(days=3) <= end <= meeting + timedelta(days=10)
    return False


async def compute(markets: list[dict]) -> list[dict]:
    candidates = [m for m in markets if _FED_RX.search(m.get("question") or "")]
    if not candidates:
        return []

    meeting = _next_fomc()
    if not meeting:
        log.info("fed model: no upcoming FOMC meeting in calendar; skipping")
        return []
    meeting_date = date.fromisoformat(meeting["decision_date"])

    async with httpx.AsyncClient(timeout=15.0) as client:
        current_rate = await _current_dff(client)
        if current_rate is None:
            log.info("fed model: no DFF source (set FRED_API_KEY or FORECAST_DFF); skipping")
            return []

        contract_yr, contract_mo = _next_month(meeting_date)
        symbol = ff_contract_symbol(contract_yr, contract_mo)
        price = await _fetch_yahoo_close(client, symbol)
        if price is None:
            log.info("fed model: no ZQ close for %s; skipping", symbol)
            return []

    implied_rate = 100.0 - price
    delta = implied_rate - current_rate
    probs = derive_probs(delta)

    rows: list[dict] = []
    for m in candidates:
        if not _matches_meeting(m.get("question") or "", m.get("end_date"), meeting_date):
            continue
        bucket = classify(m.get("question") or "", current_rate)
        if not bucket:
            continue
        uid = m.get("uid") or f"{m.get('venue')}:{m.get('venue_id')}"
        rows.append(
            {
                "market_uid": uid,
                "source": "fed_implied",
                "model_prob": probs.get(bucket, 0.0),
                "prob_method": "zq_interp",
                "detail": json.dumps(
                    {
                        "contract": symbol,
                        "contract_price": round(price, 4),
                        "current_rate": current_rate,
                        "implied_post_rate": round(implied_rate, 4),
                        "delta_bps": round(delta * 100, 1),
                        "bucket": bucket,
                        "probabilities": probs,
                        "meeting_date": meeting["decision_date"],
                    }
                ),
            }
        )
    return rows

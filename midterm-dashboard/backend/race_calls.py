from __future__ import annotations
"""Race-call providers for election night.

Three adapters, each behind an env flag — the live dashboard works with
zero providers configured (markets-only mode), and gracefully adds calls
as providers come online:

  AP_API_KEY    → Associated Press Election API ($$ enterprise tier)
  DDHQ_API_KEY  → Decision Desk HQ public API
  WIKIPEDIA     → Always available, but only after polls close — scrapes
                  the "2026 elections" articles for "Winner" infoboxes.

A fourth "manual" provider lets admins call a race from the UI for
demos, tests, or election-night human-in-the-loop.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from aggregators._retry import fetch_json_with_retry

logger = logging.getLogger(__name__)


def providers_configured() -> dict:
    """Which call providers are wired up. Surfaced to the frontend so the
    live page can tell users whether they're seeing real data or just
    markets-only."""
    return {
        "ap": bool(os.getenv("AP_API_KEY", "").strip()),
        "ddhq": bool(os.getenv("DDHQ_API_KEY", "").strip()),
        "wikipedia": True,        # always available, lower-quality
        "manual": True,           # always available — admin entry
    }


# ---------------------------------------------------------------------------
# Associated Press (scaffolded — real endpoint is enterprise-tier)
# ---------------------------------------------------------------------------

AP_BASE = "https://api.ap.org/v3/elections"


async def _fetch_ap_calls(session: aiohttp.ClientSession, election_date: str) -> list[dict]:
    """Fetch race calls from the AP Election API.

    AP's schema places one race under ``races[].reportingUnits[0].candidates``
    with the called candidate flagged via ``winner: "X"``. We translate to
    our canonical ``{race_key, provider, called_party, ...}`` shape.
    """
    api_key = os.getenv("AP_API_KEY", "").strip()
    if not api_key:
        return []
    params = {"apikey": api_key, "format": "json"}
    url = f"{AP_BASE}/{election_date}"
    data = await fetch_json_with_retry(
        session, url, params=params, timeout=30, source_label="ap-elections", max_attempts=2,
    )
    if not isinstance(data, dict):
        return []
    out = []
    for race in (data.get("races") or []):
        race_type = (race.get("officeName") or "").lower().split()[0]   # "U.S. Senate" → "u.s."
        if race_type.startswith("u.s."):
            race_type = race.get("officeName", "").lower().replace("u.s.", "").strip().split()[0]
        if race_type not in ("senate", "house", "governor"):
            continue
        state = (race.get("statePostal") or "").upper()
        if not state:
            continue
        race_key = f"{race_type}_{state}"
        # Find the winning candidate
        units = race.get("reportingUnits") or []
        called = None
        for u in units[:1]:  # state-level reporting unit only
            for c in (u.get("candidates") or []):
                if c.get("winner") == "X":
                    called = c
                    break
        if not called:
            continue
        out.append({
            "race_key": race_key,
            "provider": "ap",
            "called_party": (called.get("party") or "")[:1].upper() or None,
            "called_candidate": f"{called.get('first', '')} {called.get('last', '')}".strip(),
            "leader_pct": float(called.get("pct") or 0) or None,
            "reporting_pct": float(units[0].get("precinctsReportingPct") or 0) if units else None,
            "notes": "AP Decision Desk",
        })
    return out


# ---------------------------------------------------------------------------
# Decision Desk HQ
# ---------------------------------------------------------------------------

DDHQ_BASE = "https://api.decisiondeskhq.com/v3/elections"


async def _fetch_ddhq_calls(session: aiohttp.ClientSession) -> list[dict]:
    """Decision Desk HQ uses a similar shape to AP. The endpoint requires a
    bearer token in the Authorization header (not a query param like AP).

    We don't have an aiohttp helper for bearer auth in fetch_json_with_retry,
    so we do the request inline with the retry-aware aiohttp session. If
    DDHQ rate-limits we'll see a 429 and the retry helper above could
    eventually be extended to take headers; for now, treat 429 like 5xx.
    """
    api_key = os.getenv("DDHQ_API_KEY", "").strip()
    if not api_key:
        return []
    headers = {"Authorization": f"Bearer {api_key}", "User-Agent": "MidtermEdge/1.0"}
    url = f"{DDHQ_BASE}/2026"
    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"DDHQ {resp.status}: {await resp.text()}")
                return []
            data = await resp.json()
    except Exception as e:
        logger.warning(f"DDHQ fetch error: {e}")
        return []

    out = []
    for race in (data.get("races") if isinstance(data, dict) else []) or []:
        rt = (race.get("type") or "").lower()
        if rt not in ("senate", "house", "governor"):
            continue
        state = (race.get("state") or "").upper()
        if not state:
            continue
        race_key = f"{rt}_{state}"
        winner = race.get("winner") or {}
        if not winner:
            continue
        out.append({
            "race_key": race_key,
            "provider": "ddhq",
            "called_party": (winner.get("party") or "")[:1].upper() or None,
            "called_candidate": winner.get("name") or None,
            "leader_pct": float(winner.get("pct") or 0) or None,
            "reporting_pct": float(race.get("reporting_pct") or 0) or None,
            "notes": "Decision Desk HQ",
        })
    return out


# ---------------------------------------------------------------------------
# Disagreement detector
# ---------------------------------------------------------------------------

def market_implied_winner_party(outcomes: list[dict]) -> Optional[str]:
    """Infer D/R from the highest-probability outcome's name."""
    if not outcomes:
        return None
    top = max(outcomes, key=lambda o: o.get("probability") or 0)
    name = (top.get("name") or "").lower()
    # Direct party signals
    if "democrat" in name or name in ("dem", "dems", "d") or name == "yes":
        return "D"
    if "republican" in name or name in ("rep", "reps", "gop", "r") or name == "no":
        return "R"
    if "independent" in name or name in ("ind", "i"):
        return "I"
    return None


def detect_disagreement(call: dict, market_top: dict | None) -> dict | None:
    """Compare one race call against the market's current top outcome.

    Returns ``None`` when there's nothing to flag, or a dict with the
    severity + magnitude when the market still implies a different
    winner than the called party.
    """
    if not call or not market_top:
        return None
    call_party = call.get("called_party")
    market_party = market_top.get("inferred_party")
    if not call_party or not market_party:
        return None
    if call_party == market_party:
        return None  # they agree
    # Market still backing the LOSING side
    prob = market_top.get("probability") or 0.0
    if prob < 0.5:
        return None  # market already conceded
    # Severity scales with how confident the market still is in the wrong side
    severity = "low"
    if prob >= 0.70:
        severity = "high"
    elif prob >= 0.55:
        severity = "medium"
    return {
        "call_party": call_party,
        "market_party": market_party,
        "market_prob": round(prob, 4),
        "severity": severity,
    }

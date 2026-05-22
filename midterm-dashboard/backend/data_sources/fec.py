"""FEC (Federal Election Commission) API client.

Fetches campaign finance data for US congressional candidates.

Endpoint: https://api.open.fec.gov/v1/
Free API key available at https://api.data.gov/signup/
Rate limit: 1000 requests/hour (DEMO_KEY), 120 requests/minute (keyed).

Docs: https://api.open.fec.gov/developers/
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

FEC_BASE = "https://api.open.fec.gov/v1"
FEC_KEY = os.environ.get("FEC_API_KEY", "DEMO_KEY")

# FEC office codes
_OFFICE_MAP = {
    "house": "H",
    "senate": "S",
    "governor": None,  # FEC doesn't track governors (state races)
    "president": "P",
}


def _normalize_name(fec_name: str) -> str:
    """Convert 'LAST, FIRST MIDDLE' → 'First Last'."""
    if not fec_name:
        return ""
    parts = fec_name.split(",", 1)
    if len(parts) == 2:
        last = parts[0].strip().title()
        first = parts[1].strip().split()[0].title() if parts[1].strip() else ""
        return f"{first} {last}".strip()
    return fec_name.strip().title()


def _match_score(fec_name: str, target_name: str) -> int:
    """Score how well an FEC name matches a target candidate name."""
    fec_norm = _normalize_name(fec_name).lower()
    target_lower = target_name.lower()
    if fec_norm == target_lower:
        return 100  # exact
    fec_tokens = set(re.findall(r"[a-z']+", fec_norm))
    target_tokens = set(re.findall(r"[a-z']+", target_lower))
    if not target_tokens:
        return 0
    # Last name match is critical
    fec_last = fec_name.split(",")[0].strip().lower() if "," in fec_name else ""
    if fec_last and fec_last in target_lower:
        # First name or initial match
        if fec_tokens & target_tokens == target_tokens:
            return 90  # all target tokens in FEC name
        return 70  # last name match
    overlap = len(fec_tokens & target_tokens)
    return overlap * 20


async def fetch_race_financials(
    session: aiohttp.ClientSession,
    state: str,
    race_type: str,
    district: Optional[str] = None,
    cycle: int = 2026,
    max_results: int = 15,
) -> list[dict]:
    """Fetch FEC financial totals for all candidates in a race.

    Returns a list of {fec_name, name, party, receipts, disbursements,
    cash_on_hand, candidate_id} sorted by receipts descending.
    """
    office = _OFFICE_MAP.get(race_type)
    if not office:
        return []  # FEC doesn't track governor races

    params = {
        "api_key": FEC_KEY,
        "state": state.upper(),
        "office": office,
        "cycle": str(cycle),
        "per_page": str(max_results),
        "sort": "-receipts",
    }
    if district and office == "H":
        d = str(district).lstrip("0") or "0"
        try:
            params["district"] = f"{int(d):02d}"
        except ValueError:
            params["district"] = d

    url = f"{FEC_BASE}/candidates/totals/"
    try:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status != 200:
                logger.warning("FEC totals %s %s-%s: HTTP %d", race_type, state, district, resp.status)
                return []
            data = await resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("FEC totals %s %s-%s failed: %s", race_type, state, district, e)
        return []

    results = []
    for c in data.get("results", []):
        receipts = c.get("receipts") or 0
        if isinstance(receipts, str):
            try:
                receipts = float(receipts)
            except ValueError:
                receipts = 0
        disbursements = c.get("disbursements") or 0
        if isinstance(disbursements, str):
            try:
                disbursements = float(disbursements)
            except ValueError:
                disbursements = 0
        cash = c.get("cash_on_hand_end_period") or 0
        if isinstance(cash, str):
            try:
                cash = float(cash)
            except ValueError:
                cash = 0

        party_raw = c.get("party_full") or c.get("party") or ""
        party = "Republican" if "republican" in party_raw.lower() else \
                "Democratic" if "democrat" in party_raw.lower() else party_raw

        results.append({
            "fec_name": c.get("name", ""),
            "name": _normalize_name(c.get("name", "")),
            "party": party,
            "receipts": round(receipts),
            "disbursements": round(disbursements),
            "cash_on_hand": round(cash),
            "candidate_id": c.get("candidate_id"),
            "district": c.get("district") or c.get("district_number"),
            "incumbent_challenge": c.get("incumbent_challenge_full"),
        })
    return results


def match_fec_to_candidate(
    fec_candidates: list[dict], candidate_name: str
) -> Optional[dict]:
    """Find the best FEC match for a Polymarket candidate name.

    Returns the matched FEC record or None if no good match.
    """
    if not fec_candidates or not candidate_name:
        return None
    best = None
    best_score = 0
    for fc in fec_candidates:
        score = _match_score(fc.get("fec_name", ""), candidate_name)
        if score > best_score:
            best = fc
            best_score = score
    # Require at least a last-name match (score >= 70)
    if best_score >= 70:
        return best
    return None

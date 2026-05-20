"""Congressional Periodic Transaction Report (PTR) fetcher.

US House and Senate members are required to disclose stock trades within
45 days under the STOCK Act. The official sources are:
  - House:  https://disclosures-clerk.house.gov/  (annual ZIPs, mostly PDFs)
  - Senate: https://efdsearch.senate.gov/         (search UI, needs cookies)

Neither is fun to scrape. The community has built clean JSON aggregates
at house-stock-watcher / senate-stock-watcher S3 buckets — these are
the same datasets every consumer Congress tracker (CapitolTrades, Quiver,
Stockcrack, etc.) leans on. We use them as the primary source, document
the dependency in the README, and bake in graceful failure so the rest
of the dashboard never breaks if the buckets are down.

Both feeds return a JSON array. Field names differ slightly per source
so we normalise on read.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

import httpx

log = logging.getLogger("congress")

HOUSE_URL  = "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json"
SENATE_URL = "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json"

_TIMEOUT = 30.0
_AMOUNT_RX = re.compile(r"\$?\s*([\d,]+)\s*(?:-|to|–)\s*\$?\s*([\d,]+)", re.I)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": "narve.ai whale tracker contact@narve.ai"},
        timeout=_TIMEOUT,
        follow_redirects=True,
    )


async def fetch_house() -> list[dict]:
    async with _client() as cx:
        r = await cx.get(HOUSE_URL)
        r.raise_for_status()
        data = r.json()
    return [_normalise_house(item) for item in data if isinstance(item, dict)]


async def fetch_senate() -> list[dict]:
    async with _client() as cx:
        r = await cx.get(SENATE_URL)
        r.raise_for_status()
        data = r.json()
    return [_normalise_senate(item) for item in data if isinstance(item, dict)]


def _normalise_house(item: dict) -> dict:
    # House schema typically:
    #   disclosure_year, disclosure_date, transaction_date, owner,
    #   ticker, asset_description, type, amount, representative,
    #   district, ptr_link, cap_gains_over_200_usd
    amount = item.get("amount") or ""
    amin, amax = _parse_amount_range(amount)
    rep = (item.get("representative") or "").strip()
    txn_date = item.get("transaction_date") or ""
    disclosure = item.get("disclosure_date") or ""
    ticker = (item.get("ticker") or "").strip().upper() or None
    txn_id = item.get("transaction_id") or (
        f"H:{rep}:{txn_date}:{ticker or item.get('asset_description','?')}:{amount}:{item.get('type','')}"
    )
    return {
        "transaction_id":   str(txn_id)[:200],
        "chamber":          "House",
        "representative":   rep,
        "party":            (item.get("party") or "").strip() or None,
        "state":            (item.get("state_district") or item.get("district") or "").strip() or None,
        "transaction_date": txn_date,
        "disclosure_date":  disclosure,
        "ticker":           ticker,
        "asset_description": (item.get("asset_description") or "").strip() or None,
        "asset_type":       (item.get("asset_type") or "Stock").strip() or None,
        "transaction_type": (item.get("type") or item.get("transaction_type") or "").strip() or None,
        "amount_range":     amount or None,
        "amount_min":       amin,
        "amount_max":       amax,
        "comment":          (item.get("comment") or "").strip()[:300] or None,
        "source_url":       (item.get("ptr_link") or item.get("source") or "").strip() or None,
    }


def _normalise_senate(item: dict) -> dict:
    # Senate schema typically:
    #   senator, ptr_link, transaction_date, asset_type, asset_description,
    #   type, amount, comment, ticker
    amount = item.get("amount") or ""
    amin, amax = _parse_amount_range(amount)
    sen = (item.get("senator") or item.get("first_name", "") + " " + item.get("last_name", "")).strip()
    txn_date = item.get("transaction_date") or ""
    disclosure = item.get("disclosure_date") or ""
    ticker = (item.get("ticker") or "").strip().upper() or None
    txn_id = item.get("transaction_id") or (
        f"S:{sen}:{txn_date}:{ticker or item.get('asset_description','?')}:{amount}:{item.get('type','')}"
    )
    return {
        "transaction_id":   str(txn_id)[:200],
        "chamber":          "Senate",
        "representative":   sen,
        "party":            (item.get("party") or "").strip() or None,
        "state":            (item.get("state") or "").strip() or None,
        "transaction_date": txn_date,
        "disclosure_date":  disclosure,
        "ticker":           ticker,
        "asset_description": (item.get("asset_description") or "").strip() or None,
        "asset_type":       (item.get("asset_type") or "Stock").strip() or None,
        "transaction_type": (item.get("type") or item.get("transaction_type") or "").strip() or None,
        "amount_range":     amount or None,
        "amount_min":       amin,
        "amount_max":       amax,
        "comment":          (item.get("comment") or "").strip()[:300] or None,
        "source_url":       (item.get("ptr_link") or item.get("source") or "").strip() or None,
    }


def _parse_amount_range(s: str) -> tuple[float | None, float | None]:
    """Disclosures use bands like '$1,001 - $15,000'. Return (min, max)."""
    if not s:
        return (None, None)
    m = _AMOUNT_RX.search(s)
    if not m:
        # Some entries say "Over $50,000,000" — keep min only.
        m2 = re.search(r"over\s*\$?\s*([\d,]+)", s, re.I)
        if m2:
            try:
                return (float(m2.group(1).replace(",", "")), None)
            except ValueError:
                pass
        return (None, None)
    try:
        return (float(m.group(1).replace(",", "")), float(m.group(2).replace(",", "")))
    except ValueError:
        return (None, None)


def dedupe(rows: Iterable[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        tid = r.get("transaction_id") or ""
        if not tid or tid in seen:
            continue
        seen.add(tid)
        out.append(r)
    return out

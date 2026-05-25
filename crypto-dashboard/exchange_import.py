#!/usr/bin/env python3
"""
Auto-import historical fills from connected exchanges.

Closes the manual-holdings-entry UX gap. Every fill on Coinbase or
Kraken becomes either:
  - a `crypto_holdings` row (buys) — with cost basis adjusted for fees
  - a `crypto_dispositions` row (sells) — allocated against existing
    lots via the user's configured lot method (HIFO / FIFO / etc.)

The `crypto_exchange_imports` ledger is the source of truth for which
fills we've already imported. Its UNIQUE(exchange, external_id) is the
idempotency boundary — re-running import is always safe.

Order of operations matters for correctness:
  1. Pull both exchanges' fills (or the requested one).
  2. Sort by filled_at ascending.
  3. For each fill, skip if already in the import ledger.
  4. **Buys first, then sells, in time order** — so when we hit a sell
     the lots it consumes are already created.
  5. Persist atomically: insert holding → insert import row pointing
     at the new holding. Or insert disposition → insert import row.
     If either fails, the unique constraint blocks the duplicate on the
     next retry.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

import database as db
import exchanges as xch
import tax as tax_mod

log = logging.getLogger("crypto.exchange_import")


# ─── Helpers ────────────────────────────────────────────────────────────────

def _to_date(iso_ts: str) -> Optional[str]:
    """Coerce an ISO datetime to a date string (YYYY-MM-DD) — the format
    `crypto_holdings.acquired_at` and `crypto_dispositions.sell_date`
    expect. We drop the time component because lot accounting works at
    day granularity."""
    if not iso_ts:
        return None
    try:
        # Handle 'Z' suffix from Coinbase.
        clean = iso_ts.replace("Z", "+00:00")
        return datetime.fromisoformat(clean).date().isoformat()
    except (ValueError, TypeError):
        return None


def _adjusted_cost_basis(fill: xch.Fill) -> float:
    """Per-unit cost basis adjusted for fees. For a buy: total spent =
    qty × price + fee, basis = total/qty. The +fee/qty adjustment is the
    standard tax treatment."""
    if fill.qty <= 0:
        return 0.0
    return float(fill.price) + (float(fill.fee_usd) / float(fill.qty))


def _adjusted_sell_price(fill: xch.Fill) -> float:
    """Per-unit net sell price after fees. Net proceeds = qty × price −
    fee; effective price per unit = proceeds / qty. Mirror of cost-basis
    adjustment for buys."""
    if fill.qty <= 0:
        return 0.0
    return float(fill.price) - (float(fill.fee_usd) / float(fill.qty))


# ─── Per-fill persistence ───────────────────────────────────────────────────

def _persist_buy(user_id: str, exchange: str, fill: xch.Fill) -> dict:
    """Turn a buy fill into a crypto_holdings row + import-ledger entry.
    Returns {imported: bool, holding_id?, reason?}."""
    acquired = _to_date(fill.filled_at)
    if not acquired:
        return {"imported": False, "reason": "bad timestamp"}
    cost_basis = _adjusted_cost_basis(fill)
    if cost_basis <= 0:
        return {"imported": False, "reason": "zero cost basis"}
    holding_id = db.add_holding(
        user_id=user_id, ticker=fill.ticker,
        qty=float(fill.qty), cost_basis=cost_basis,
        acquired_at=acquired,
        note=f"auto-import: {exchange} #{fill.external_id[:16]}",
    )
    # Try the import-ledger insert. ANY exception here (not just
    # IntegrityError on the UNIQUE constraint) must roll back the holding
    # we just created — otherwise a transient sqlite3.OperationalError
    # like "database is locked" leaves the holding orphaned with no
    # ledger row, and the next retry creates a duplicate.
    try:
        inserted = db.insert_exchange_import(
            user_id=user_id, exchange=exchange,
            external_id=fill.external_id, ticker=fill.ticker,
            side="buy", qty=float(fill.qty), price=float(fill.price),
            fee_usd=float(fill.fee_usd), filled_at=fill.filled_at,
            holding_id=holding_id, disposition_id=None,
            raw_json=json.dumps(fill.raw, default=str)[:8000],
        )
    except Exception as e:
        try:
            db.remove_holding(user_id, holding_id)
        except Exception:
            pass
        raise
    if not inserted:
        db.remove_holding(user_id, holding_id)
        return {"imported": False, "reason": "already imported (race)"}
    return {"imported": True, "holding_id": holding_id}


def _persist_sell(user_id: str, exchange: str, fill: xch.Fill) -> dict:
    """Turn a sell fill into a crypto_dispositions row + import-ledger
    entry. Uses the user's configured tax lot method to allocate against
    existing lots. If no lots are available (user sold before importing
    buys, or it's a short), we still record the import row but mark
    disposition_id = NULL so the user sees it in the audit log."""
    sell_date = _to_date(fill.filled_at)
    if not sell_date:
        return {"imported": False, "reason": "bad timestamp"}
    settings = tax_mod.get_tax_settings(user_id)
    method = settings["default_lot_method"]
    sell_price = _adjusted_sell_price(fill)
    if sell_price <= 0:
        return {"imported": False, "reason": "zero sell price"}
    disposition_id: Optional[int] = None
    try:
        result = tax_mod.record_disposition(
            user_id=user_id, ticker=fill.ticker,
            qty=float(fill.qty), sell_price=sell_price,
            method=method, sell_date=sell_date,
            exchange=exchange, execution_id=None,
            notes=f"auto-import: {exchange} #{fill.external_id[:16]}",
        )
        disposition_id = result.get("id")
    except ValueError as e:
        # Most common cause: no open lots available. We still want to
        # record the fill in the import ledger so the user has the full
        # audit trail; they can manually reconcile later.
        log.info("disposition skipped for %s/%s: %s", user_id, fill.external_id, e)
    inserted = db.insert_exchange_import(
        user_id=user_id, exchange=exchange,
        external_id=fill.external_id, ticker=fill.ticker,
        side="sell", qty=float(fill.qty), price=float(fill.price),
        fee_usd=float(fill.fee_usd), filled_at=fill.filled_at,
        holding_id=None, disposition_id=disposition_id,
        raw_json=json.dumps(fill.raw, default=str)[:8000],
    )
    if not inserted:
        # Lost the UNIQUE(exchange, external_id) race against a concurrent
        # importer. Roll back the disposition + lot-consumption rows so we
        # don't double-decrement the user's open lots. Mirrors the
        # rollback in _persist_buy.
        if disposition_id is not None:
            try:
                db.remove_disposition(user_id, disposition_id)
            except Exception as e:
                log.warning("rollback failed for disposition %s: %s",
                            disposition_id, e)
        return {"imported": False, "reason": "already imported (race)"}
    return {"imported": True, "disposition_id": disposition_id,
            "no_lots": disposition_id is None}


# ─── Orchestrator ───────────────────────────────────────────────────────────

def import_fills(user_id: str, exchange: str) -> dict:
    """Pull every new fill from the exchange and turn it into holdings /
    dispositions. Idempotent — re-running is safe.

    Returns {fetched, new_buys, new_sells, sells_no_lots, skipped, error?}."""
    if exchange not in ("coinbase", "kraken"):
        return {"error": f"unsupported exchange: {exchange}"}
    adapter = xch.get_adapter(user_id, exchange)
    if adapter is None:
        return {"error": f"{exchange} not configured"}
    # Incremental: only fetch fills after the last one we imported.
    since_iso = db.get_latest_exchange_import_at(user_id, exchange)
    try:
        fills = adapter.get_filled_orders(since_iso=since_iso)
    except Exception as e:
        log.warning("fetch failed for %s/%s: %s", user_id, exchange, e)
        return {"error": f"fetch failed: {type(e).__name__}: {e}"}

    # Sort ascending so when we hit a sell its buys are already persisted.
    fills.sort(key=lambda f: f.filled_at)

    summary = {
        "fetched": len(fills), "new_buys": 0, "new_sells": 0,
        "sells_no_lots": 0, "skipped": 0, "errors": 0,
    }
    for fill in fills:
        # Dedup at the source: if the external_id already exists in the
        # import ledger, skip.
        if db.exchange_import_exists(user_id, exchange, fill.external_id):
            summary["skipped"] += 1
            continue
        try:
            if fill.side == "buy":
                r = _persist_buy(user_id, exchange, fill)
                if r["imported"]:
                    summary["new_buys"] += 1
                else:
                    summary["skipped"] += 1
            elif fill.side == "sell":
                r = _persist_sell(user_id, exchange, fill)
                if r["imported"]:
                    summary["new_sells"] += 1
                    if r.get("no_lots"):
                        summary["sells_no_lots"] += 1
                else:
                    summary["skipped"] += 1
        except Exception as e:
            log.warning("persist failed for fill %s: %s", fill.external_id, e)
            summary["errors"] += 1
    return summary


def import_all(user_id: str) -> dict:
    """Run imports for every connected exchange. Returns per-exchange
    summary keyed on exchange name."""
    out: dict = {"exchanges": {}}
    for exchange in xch.configured_exchanges(user_id):
        out["exchanges"][exchange] = import_fills(user_id, exchange)
    return out


def import_status(user_id: str) -> dict:
    """Return the latest import timestamp + per-exchange total counts.
    Used by the UI to render the "last imported" indicator."""
    out: dict = {"exchanges": {}}
    for exchange in ("coinbase", "kraken"):
        info = db.get_exchange_import_status(user_id, exchange)
        out["exchanges"][exchange] = info or {"configured": False}
    return out

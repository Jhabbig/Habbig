#!/usr/bin/env python3
"""
Tax-optimal selling — lot selection, realized-P&L, harvest scanner, Form 8949 export.

The model:
  - `crypto_holdings` is the **immutable** lot ledger. When a user buys, a row
    is inserted. Rows are never modified — tax authorities want the original
    acquisition record intact.
  - `crypto_dispositions` is the **immutable** sale ledger. One row per sell event.
  - `crypto_tax_lot_consumption` is the *join* between them: how much of which
    acquisition lot was consumed by which sale, with per-lot gain split into
    long-term vs short-term.
  - "Remaining qty" of a lot = lot.qty − Σ consumption.consumed_qty for that lot.

Why an immutable model:
  - Single source of truth — never lose data via mutating updates.
  - Auditable — re-derivable from primary events.
  - Lets us swap lot methods retroactively for a hypothetical "what if I'd
    used HIFO last year?" analysis (the `preview_sell()` helper).

Lot-selection methods supported:
  FIFO  — oldest lot first (US default if you don't elect otherwise)
  LIFO  — newest lot first
  HIFO  — highest cost basis first (minimizes realized gain; default here)
  LOFO  — lowest cost basis first (maximizes gain; useful in low-bracket years)
  TAX_OPTIMAL — prefer long-term lots with positive gain (lower tax rate) and
                short-term lots with losses (offset ordinary income).

Jurisdiction:
  US — long-term threshold = 365 days. Wash-sale doesn't apply to crypto yet
       under current law, but we flag harvest candidates that would have
       triggered it for forward-looking caution.

Form 8949 export columns: description, date_acquired, date_sold, proceeds,
cost_basis, code, adjustment, gain_loss. Separate Part I (ST) and Part II (LT).
"""

from __future__ import annotations

import csv
import io
import logging
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import database as db
import long_term as lt

log = logging.getLogger("crypto.tax")


# ─── Lot-selection methods ──────────────────────────────────────────────────

LOT_METHODS = ("FIFO", "LIFO", "HIFO", "LOFO", "TAX_OPTIMAL")
DEFAULT_LOT_METHOD = "HIFO"
LT_THRESHOLD_DAYS = 365

# Default safety thresholds for the harvest scanner.
DEFAULT_HARVEST_MIN_LOSS_USD = 100.0
DEFAULT_HARVEST_MIN_AGE_DAYS = 30


# ─── Data classes ───────────────────────────────────────────────────────────

@dataclass
class LotState:
    """A snapshot of one acquisition lot at a moment in time, with
    consumption already factored in."""
    holding_id: int
    ticker: str
    qty_original: float
    qty_consumed: float
    qty_remaining: float
    cost_basis: float
    acquired_at: str  # ISO date

    @property
    def is_long_term_at(self) -> str:
        """Date on which this lot becomes long-term-eligible (acquired + 366d)."""
        return (datetime.fromisoformat(self.acquired_at).date() +
                timedelta(days=LT_THRESHOLD_DAYS + 1)).isoformat()

    def to_dict(self) -> dict:
        return {**asdict(self), "long_term_at": self.is_long_term_at}


@dataclass
class LotPick:
    """One leg of an allocation across lots — how much of a particular lot is
    being consumed by a sale."""
    holding_id: int
    consumed_qty: float
    cost_basis: float
    acquired_at: str
    sell_price: float
    proceeds: float
    realized_gain: float
    classification: str  # LT | ST
    days_held: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SellPreview:
    ticker: str
    requested_qty: float
    filled_qty: float            # may be < requested if not enough lots
    method: str
    sell_price: float
    total_proceeds: float
    total_cost_basis: float
    total_realized: float
    lt_realized: float
    st_realized: float
    picks: list[LotPick]
    shortfall: float             # requested_qty − filled_qty
    note: str

    def to_dict(self) -> dict:
        return {**asdict(self), "picks": [p.to_dict() for p in self.picks]}


# ─── Lot state computation ──────────────────────────────────────────────────

def open_lots(user_id: str, ticker: str | None = None) -> list[LotState]:
    """Return open lots (remaining > 0) for one or all tickers, oldest first.
    `consumed_qty` is summed from prior dispositions."""
    holdings = db.get_holdings(user_id)
    consumed_by_holding = db.get_consumption_by_holding(user_id)
    out: list[LotState] = []
    for h in holdings:
        if ticker and h["ticker"] != ticker.upper():
            continue
        consumed = float(consumed_by_holding.get(h["id"], 0.0))
        remaining = float(h["qty"]) - consumed
        if remaining <= 1e-12:
            continue
        out.append(LotState(
            holding_id=h["id"], ticker=h["ticker"],
            qty_original=float(h["qty"]), qty_consumed=consumed,
            qty_remaining=remaining, cost_basis=float(h["cost_basis"]),
            acquired_at=h["acquired_at"],
        ))
    return out


# ─── Lot selection ──────────────────────────────────────────────────────────

def _sort_for_method(lots: list[LotState], method: str, sell_price: float) -> list[LotState]:
    """Return lots sorted by the order in which they should be consumed."""
    today = datetime.now(timezone.utc).date()

    def days_held(lot: LotState) -> int:
        try:
            return (today - datetime.fromisoformat(lot.acquired_at).date()).days
        except ValueError:
            return 0

    if method == "FIFO":
        return sorted(lots, key=lambda l: l.acquired_at)
    if method == "LIFO":
        return sorted(lots, key=lambda l: l.acquired_at, reverse=True)
    if method == "HIFO":
        return sorted(lots, key=lambda l: l.cost_basis, reverse=True)
    if method == "LOFO":
        return sorted(lots, key=lambda l: l.cost_basis)
    if method == "TAX_OPTIMAL":
        # Heuristic: prefer (in order):
        #  1. ST losses (offset ordinary income at full rate)
        #  2. LT losses
        #  3. LT gains at the highest cost basis (smallest LT gain → low-rate tax)
        #  4. ST gains at the highest cost basis (smallest ST gain → high-rate but small)
        def key(lot: LotState):
            held = days_held(lot)
            is_long_term = held >= LT_THRESHOLD_DAYS
            unrealised_per_unit = sell_price - lot.cost_basis
            is_loss = unrealised_per_unit < 0
            tier = 0 if (is_loss and not is_long_term) else \
                   1 if is_loss else \
                   2 if is_long_term else 3
            # Within tier: largest loss first if loss, highest basis first if gain.
            secondary = -unrealised_per_unit if is_loss else lot.cost_basis
            return (tier, -secondary)
        return sorted(lots, key=key)
    raise ValueError(f"Unknown lot method: {method}")


def allocate_lots(lots: list[LotState], sell_qty: float, method: str,
                  sell_price: float, sell_date: str) -> tuple[list[LotPick], float]:
    """Pick lots to fulfill `sell_qty`. Returns (picks, shortfall).
    Allocation is greedy: take whole lots first, then partial on the last lot."""
    ordered = _sort_for_method(lots, method, sell_price)
    remaining = float(sell_qty)
    picks: list[LotPick] = []
    sell_date_d = datetime.fromisoformat(sell_date).date()

    for lot in ordered:
        if remaining <= 1e-12:
            break
        take = min(lot.qty_remaining, remaining)
        if take <= 1e-12:
            continue
        cost = take * lot.cost_basis
        proceeds = take * sell_price
        gain = proceeds - cost
        try:
            acquired_d = datetime.fromisoformat(lot.acquired_at).date()
            days_held = (sell_date_d - acquired_d).days
        except ValueError:
            days_held = 0
        classification = "LT" if days_held >= LT_THRESHOLD_DAYS else "ST"
        picks.append(LotPick(
            holding_id=lot.holding_id, consumed_qty=take,
            cost_basis=lot.cost_basis, acquired_at=lot.acquired_at,
            sell_price=sell_price, proceeds=proceeds, realized_gain=gain,
            classification=classification, days_held=days_held,
        ))
        remaining -= take

    return picks, max(0.0, remaining)


def preview_sell(user_id: str, ticker: str, qty: float, method: str,
                 sell_price: float | None = None,
                 sell_date: str | None = None) -> SellPreview:
    """Hypothetical sale — no records written. Useful for the UI to show the
    user what their tax bill would be before they pull the trigger.
    `sell_price` defaults to the last close from `crypto_daily_bars`."""
    ticker = ticker.upper()
    if method not in LOT_METHODS:
        raise ValueError(f"unknown method: {method}")
    if sell_price is None:
        _, closes = lt.get_daily_closes(ticker, days=3)
        if len(closes) == 0:
            return SellPreview(ticker=ticker, requested_qty=qty, filled_qty=0, method=method,
                               sell_price=0, total_proceeds=0, total_cost_basis=0,
                               total_realized=0, lt_realized=0, st_realized=0,
                               picks=[], shortfall=qty, note="no price data")
        sell_price = float(closes[-1])
    sell_date = sell_date or datetime.now(timezone.utc).date().isoformat()

    lots = open_lots(user_id, ticker)
    if not lots:
        return SellPreview(ticker=ticker, requested_qty=qty, filled_qty=0, method=method,
                           sell_price=sell_price, total_proceeds=0, total_cost_basis=0,
                           total_realized=0, lt_realized=0, st_realized=0,
                           picks=[], shortfall=qty, note="no open lots for this ticker")

    picks, shortfall = allocate_lots(lots, qty, method, sell_price, sell_date)
    filled = qty - shortfall
    proceeds = sum(p.proceeds for p in picks)
    cost = sum(p.consumed_qty * p.cost_basis for p in picks)
    realised = proceeds - cost
    lt_r = sum(p.realized_gain for p in picks if p.classification == "LT")
    st_r = sum(p.realized_gain for p in picks if p.classification == "ST")
    note = ""
    if shortfall > 1e-9:
        note = f"only {filled:.8f} of {qty} fillable — partial sell"
    return SellPreview(
        ticker=ticker, requested_qty=qty, filled_qty=filled, method=method,
        sell_price=sell_price, total_proceeds=round(proceeds, 2),
        total_cost_basis=round(cost, 2), total_realized=round(realised, 2),
        lt_realized=round(lt_r, 2), st_realized=round(st_r, 2),
        picks=picks, shortfall=shortfall, note=note,
    )


# ─── Recording a real disposition ───────────────────────────────────────────

def record_disposition(user_id: str, ticker: str, qty: float, sell_price: float,
                       method: str | None = None, sell_date: str | None = None,
                       exchange: str = "manual", execution_id: int | None = None,
                       notes: str = "") -> dict:
    """Persist a sale: create a disposition row + the lot-consumption rows.
    Returns the persisted disposition with full picks. Idempotent? No — call
    once per real sale. The execution_id link prevents double-recording when
    the executor places + fills a sell.
    """
    settings = get_tax_settings(user_id)
    method = method or settings["default_lot_method"]
    sell_date = sell_date or datetime.now(timezone.utc).date().isoformat()

    if execution_id is not None and db.disposition_exists_for_execution(execution_id):
        existing = db.get_disposition_by_execution(execution_id)
        return dict(existing) if existing else {}

    preview = preview_sell(user_id, ticker, qty, method, sell_price, sell_date)
    if preview.filled_qty <= 0:
        raise ValueError(preview.note or "no lots available")

    disposition_id = db.insert_disposition(
        user_id=user_id, ticker=ticker, qty=preview.filled_qty,
        sell_price=sell_price, sell_date=sell_date, method=method,
        exchange=exchange, execution_id=execution_id,
        realized_gain=preview.total_realized,
        lt_gain=preview.lt_realized, st_gain=preview.st_realized,
        notes=notes,
    )
    rows = []
    for p in preview.picks:
        rows.append((
            disposition_id, p.holding_id, p.consumed_qty, p.cost_basis,
            p.realized_gain, p.classification, p.days_held,
        ))
    db.insert_lot_consumption(rows)
    return {
        "id": disposition_id, "ticker": ticker, "qty": preview.filled_qty,
        "sell_price": sell_price, "sell_date": sell_date, "method": method,
        "realized_gain": preview.total_realized,
        "lt_gain": preview.lt_realized, "st_gain": preview.st_realized,
        "picks": [p.to_dict() for p in preview.picks],
    }


# ─── Tax-loss harvest scanner ───────────────────────────────────────────────

@dataclass
class HarvestOpportunity:
    holding_id: int
    ticker: str
    qty_remaining: float
    cost_basis: float
    current_price: float
    unrealized_loss_usd: float
    unrealized_loss_pct: float
    days_held: int
    classification: str       # LT | ST
    wash_sale_risk: bool      # bought same asset in last 30d
    estimated_tax_save_usd: float  # at default bracket assumption

    def to_dict(self) -> dict:
        return asdict(self)


# Crude tax-bracket assumption used for "estimated savings" display. Users
# can override this in settings if they care about accuracy.
DEFAULT_ST_RATE = 0.30   # ordinary income
DEFAULT_LT_RATE = 0.15   # long-term capital gains


def find_harvest_opportunities(user_id: str,
                                min_loss_usd: float | None = None,
                                min_age_days: int | None = None) -> list[HarvestOpportunity]:
    """Scan all open lots for tax-loss-harvest candidates.
    Returns lots whose unrealized loss exceeds `min_loss_usd` AND are older
    than `min_age_days` (the safety against short-term churn losses)."""
    settings = get_tax_settings(user_id)
    min_loss_usd = min_loss_usd if min_loss_usd is not None else settings["harvest_min_loss_usd"]
    min_age_days = min_age_days if min_age_days is not None else settings["harvest_min_age_days"]
    today = datetime.now(timezone.utc).date()

    lots = open_lots(user_id)
    # Group lots by ticker so we can compute wash-sale risk per ticker (any
    # purchase of that ticker in the trailing 30 days).
    recent_buys: dict[str, list[str]] = {}
    for h in db.get_holdings(user_id):
        try:
            acquired = datetime.fromisoformat(h["acquired_at"]).date()
        except ValueError:
            continue
        if (today - acquired).days <= 30:
            recent_buys.setdefault(h["ticker"], []).append(h["acquired_at"])

    out: list[HarvestOpportunity] = []
    for lot in lots:
        _, closes = lt.get_daily_closes(lot.ticker, days=3)
        if len(closes) == 0:
            continue
        price = float(closes[-1])
        loss_per_unit = price - lot.cost_basis
        if loss_per_unit >= 0:
            continue  # only losses
        total_loss = loss_per_unit * lot.qty_remaining
        if abs(total_loss) < min_loss_usd:
            continue
        try:
            days_held = (today - datetime.fromisoformat(lot.acquired_at).date()).days
        except ValueError:
            days_held = 0
        if days_held < min_age_days:
            continue
        classification = "LT" if days_held >= LT_THRESHOLD_DAYS else "ST"
        rate = DEFAULT_LT_RATE if classification == "LT" else DEFAULT_ST_RATE
        save = abs(total_loss) * rate
        wash_risk = lot.ticker in recent_buys
        out.append(HarvestOpportunity(
            holding_id=lot.holding_id, ticker=lot.ticker,
            qty_remaining=lot.qty_remaining, cost_basis=lot.cost_basis,
            current_price=price,
            unrealized_loss_usd=round(total_loss, 2),
            unrealized_loss_pct=round(loss_per_unit / lot.cost_basis, 4),
            days_held=days_held, classification=classification,
            wash_sale_risk=wash_risk,
            estimated_tax_save_usd=round(save, 2),
        ))
    out.sort(key=lambda o: o.unrealized_loss_usd)  # most-negative first
    return out


# ─── Realized P&L queries ───────────────────────────────────────────────────

def realized_pnl_summary(user_id: str, year: int) -> dict:
    """Aggregate ST + LT gain/loss for the given tax year."""
    start = f"{year}-01-01"
    end = f"{year + 1}-01-01"
    dispositions = db.get_dispositions(user_id, since=start, until=end)
    st = sum(float(d["st_gain"] or 0) for d in dispositions)
    lt_g = sum(float(d["lt_gain"] or 0) for d in dispositions)
    total = st + lt_g
    proceeds = sum(float(d["qty"]) * float(d["sell_price"]) for d in dispositions)
    # Net loss carryforward to next year — US allows $3000/yr of net cap loss
    # against ordinary income, rest carries forward.
    carryforward = 0.0
    if total < 0:
        used_against_income = min(abs(total), 3000.0)
        carryforward = abs(total) - used_against_income
    return {
        "year": year,
        "dispositions": len(dispositions),
        "total_proceeds": round(proceeds, 2),
        "short_term_realized": round(st, 2),
        "long_term_realized": round(lt_g, 2),
        "total_realized": round(total, 2),
        "estimated_tax": round(
            max(st, 0) * DEFAULT_ST_RATE + max(lt_g, 0) * DEFAULT_LT_RATE, 2,
        ),
        "loss_carryforward_to_next_year": round(carryforward, 2),
    }


def list_dispositions(user_id: str, limit: int = 200) -> list[dict]:
    rows = db.get_dispositions(user_id, limit=limit)
    out = []
    for d in rows:
        consumption = db.get_lot_consumption_for_disposition(d["id"])
        out.append({**dict(d), "consumption": [dict(c) for c in consumption]})
    return out


# ─── Form 8949 CSV export ───────────────────────────────────────────────────

def export_form_8949(user_id: str, year: int) -> str:
    """Generate a Form 8949 CSV.
    One row per lot consumption (i.e. one row per acquisition lot that was
    partially or fully disposed in the year). Split into ST (Part I) and LT
    (Part II) by the `classification` column.
    """
    start = f"{year}-01-01"
    end = f"{year + 1}-01-01"
    dispositions = db.get_dispositions(user_id, since=start, until=end)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "Part", "Description", "Date acquired", "Date sold",
        "Proceeds (USD)", "Cost basis (USD)", "Code", "Adjustment",
        "Gain/loss (USD)",
    ])
    for d in dispositions:
        consumption = db.get_lot_consumption_for_disposition(d["id"])
        for c in consumption:
            part = "I (ST)" if c["classification"] == "ST" else "II (LT)"
            description = f"{c['consumed_qty']:.8f} {d['ticker']}"
            proceeds = float(c["consumed_qty"]) * float(d["sell_price"])
            cost = float(c["consumed_qty"]) * float(c["cost_basis"])
            gain = float(c["realized_gain"])
            w.writerow([
                part, description,
                c["acquired_at"] if "acquired_at" in c.keys() else "",
                d["sell_date"],
                f"{proceeds:.2f}", f"{cost:.2f}", "", "",
                f"{gain:.2f}",
            ])
    return buf.getvalue()


# ─── Tax settings ───────────────────────────────────────────────────────────

DEFAULT_TAX_SETTINGS = {
    "jurisdiction": "US",
    "default_lot_method": DEFAULT_LOT_METHOD,
    "harvest_min_loss_usd": DEFAULT_HARVEST_MIN_LOSS_USD,
    "harvest_min_age_days": DEFAULT_HARVEST_MIN_AGE_DAYS,
    "st_rate": DEFAULT_ST_RATE,
    "lt_rate": DEFAULT_LT_RATE,
}


def get_tax_settings(user_id: str) -> dict:
    row = db.get_tax_settings(user_id)
    if not row:
        return dict(DEFAULT_TAX_SETTINGS)
    out = dict(DEFAULT_TAX_SETTINGS)
    for k in DEFAULT_TAX_SETTINGS:
        v = row.get(k)
        if v is not None:
            out[k] = v
    return out


def update_tax_settings(user_id: str, updates: dict) -> dict:
    settings = get_tax_settings(user_id)
    for k, v in updates.items():
        if k not in DEFAULT_TAX_SETTINGS:
            continue
        if k == "jurisdiction":
            if v in ("US", "UK", "DE"):
                settings[k] = v
        elif k == "default_lot_method":
            if v in LOT_METHODS:
                settings[k] = v
        elif k in ("harvest_min_loss_usd",):
            settings[k] = max(0.0, float(v))
        elif k == "harvest_min_age_days":
            settings[k] = max(0, int(v))
        elif k in ("st_rate", "lt_rate"):
            settings[k] = max(0.0, min(0.6, float(v)))
    db.upsert_tax_settings(user_id, settings)
    return settings

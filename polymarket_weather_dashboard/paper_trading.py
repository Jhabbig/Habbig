"""Paper-trading simulator.

Orders go through the same safety checks and audit log as live orders;
only the fill machinery differs. Simulated fills come from a snapshot
of the live Kalshi order book (or a caller-supplied book in tests),
applied with a couple of deliberate conservatisms:

* **Top-of-book sweep with no recursion past 5 levels** — keeps a
  market order from claiming infinite depth that wouldn't actually
  exist when the user goes live.
* **Limit orders fill only the marketable portion immediately** — any
  unfilled qty stays open with status ``working`` and is settled by
  ``settle_open_orders`` when the orderbook is re-fetched. We do not
  simulate matching against future trades.
* **Position math uses average cost** — realized PnL is computed on
  the closed portion of a position, mirroring how Kalshi reports it.

Money is denominated in **cents** throughout to avoid float drift.
The engine passes prices as integers in [1, 99]; positions track
``avg_price_cents`` to one cent of precision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class OrderbookLevel:
    price_cents: int
    size: int


@dataclass
class Orderbook:
    """Tiny dataclass for the simulator's view of a market.

    `yes_bids` are descending bids in cents (highest first), `yes_asks`
    are ascending asks (lowest first). Sizes are integer contracts.
    NO side mirrors via 100 - p where applicable.
    """
    ticker: str
    yes_bids: list[OrderbookLevel] = field(default_factory=list)
    yes_asks: list[OrderbookLevel] = field(default_factory=list)

    def best_yes_ask(self) -> Optional[OrderbookLevel]:
        return self.yes_asks[0] if self.yes_asks else None

    def best_yes_bid(self) -> Optional[OrderbookLevel]:
        return self.yes_bids[0] if self.yes_bids else None


@dataclass
class FillResult:
    """One executed fill (or part of one).

    Multiple fills can come back from a single order — e.g. a market
    order sweeping two ask levels — so the caller iterates this list
    and persists each row separately.
    """
    qty: int
    price_cents: int


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def simulate_fills(book: Orderbook, *, side: str, action: str, qty: int,
                   type_: str = "limit",
                   limit_price_cents: Optional[int] = None) -> tuple[list[FillResult], int]:
    """Simulate fills against an orderbook snapshot.

    Returns ``(fills, unfilled_qty)``. The unfilled qty is what stays
    open as a working limit order; it will always be 0 for market
    orders (we sweep until depth runs out, in which case we *reject*
    the unfilled remainder rather than queue it — Kalshi's spec for
    market orders).

    Buying YES walks the YES asks (cheapest first); selling YES walks
    the YES bids (highest first). The NO side flips price (100 - p)
    and references the YES book — Kalshi's contracts price YES + NO
    to 100 by construction.
    """
    if qty <= 0:
        return [], 0

    fills: list[FillResult] = []
    remaining = int(qty)

    if side == "yes" and action == "buy":
        levels = book.yes_asks[:5]
        worst_acceptable = limit_price_cents if type_ == "limit" else 99
        for lvl in levels:
            if lvl.price_cents > (worst_acceptable or 99):
                break
            take = min(remaining, lvl.size)
            fills.append(FillResult(qty=take, price_cents=lvl.price_cents))
            remaining -= take
            if remaining <= 0:
                break

    elif side == "yes" and action == "sell":
        levels = book.yes_bids[:5]
        worst_acceptable = limit_price_cents if type_ == "limit" else 1
        for lvl in levels:
            if lvl.price_cents < (worst_acceptable or 1):
                break
            take = min(remaining, lvl.size)
            fills.append(FillResult(qty=take, price_cents=lvl.price_cents))
            remaining -= take
            if remaining <= 0:
                break

    elif side == "no" and action == "buy":
        # Buying NO at price P means selling YES at (100 - P): walk yes_bids
        levels = book.yes_bids[:5]
        worst_acceptable_yes = (100 - (limit_price_cents or 99)) if type_ == "limit" else 1
        for lvl in levels:
            if lvl.price_cents < worst_acceptable_yes:
                break
            take = min(remaining, lvl.size)
            fills.append(FillResult(qty=take, price_cents=100 - lvl.price_cents))
            remaining -= take
            if remaining <= 0:
                break

    elif side == "no" and action == "sell":
        # Selling NO at P means buying YES at (100 - P): walk yes_asks
        levels = book.yes_asks[:5]
        worst_acceptable_yes = (100 - (limit_price_cents or 1)) if type_ == "limit" else 99
        for lvl in levels:
            if lvl.price_cents > worst_acceptable_yes:
                break
            take = min(remaining, lvl.size)
            fills.append(FillResult(qty=take, price_cents=100 - lvl.price_cents))
            remaining -= take
            if remaining <= 0:
                break

    return fills, remaining


# ─── Position tracker ─────────────────────────────────────────────────────────

@dataclass
class PaperPositionUpdate:
    """Returned by `update_position_after_fill` so the caller can record
    realized PnL in the audit log alongside the fill."""
    new_qty: int
    avg_price_cents: int
    realized_pnl_cents: int


def update_position_after_fill(*, prior_qty: int, prior_avg_cents: int,
                               action: str, fill_qty: int,
                               fill_price_cents: int) -> PaperPositionUpdate:
    """Apply one fill to a (qty, avg_cents) position.

    Direction convention:
        action='buy'  → qty increases (or short cover reduces)
        action='sell' → qty decreases (or short opens)
    Realized PnL is non-zero only on the *closing* portion of a
    position. For YES contracts where the user can be long, a sell
    that closes a long position realizes
        (sell_price - avg_cost) * fill_qty
    (in cents). Selling more than long opens a short; opening a short
    realizes nothing immediately.
    """
    direction = 1 if action == "buy" else -1
    realized = 0
    new_qty = prior_qty
    new_avg = prior_avg_cents

    delta = direction * fill_qty
    # Crossing through zero is split into "close existing" + "open new
    # opposite side" so PnL booking is clean.
    if prior_qty == 0 or (prior_qty > 0) == (delta > 0):
        # Same direction (or empty book): rolling average
        if prior_qty + delta == 0:
            new_qty = 0
            new_avg = 0
        else:
            total_cost = prior_qty * prior_avg_cents + delta * fill_price_cents
            new_qty = prior_qty + delta
            # avg_cents is signed-position-aware: keep it positive for the
            # quantity we hold
            new_avg = int(round(total_cost / new_qty)) if new_qty != 0 else 0
    else:
        # Opposite direction: realize PnL on the overlap
        closing = min(abs(prior_qty), abs(delta))
        # Long closed by sell: PnL = (sell - avg) * closing
        # Short closed by buy:  PnL = (avg - buy) * closing
        if prior_qty > 0:
            realized = (fill_price_cents - prior_avg_cents) * closing
        else:
            realized = (prior_avg_cents - fill_price_cents) * closing
        leftover = abs(delta) - closing
        new_qty = prior_qty + delta
        if new_qty == 0:
            new_avg = 0
        elif (new_qty > 0) == (delta > 0):
            new_avg = fill_price_cents
        else:
            new_avg = prior_avg_cents

    return PaperPositionUpdate(
        new_qty=new_qty,
        avg_price_cents=int(new_avg) if new_qty != 0 else 0,
        realized_pnl_cents=int(realized),
    )


# ─── DB-touching helpers ──────────────────────────────────────────────────────

def insert_paper_order(conn_factory, *, user_id: str, ticker: str,
                       side: str, action: str, qty: int,
                       limit_price_cents: Optional[int],
                       type_: str, status: str,
                       client_order_id: Optional[str] = None) -> int:
    """Persist a new paper order; returns its row id."""
    with conn_factory() as conn:
        cur = conn.execute(
            """INSERT INTO paper_orders
                  (user_id, ticker, side, action, qty, limit_price_cents,
                   type, status, client_order_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                       strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))""",
            (user_id, ticker, side, action, int(qty), limit_price_cents,
             type_, status, client_order_id),
        )
        return cur.lastrowid


def update_paper_order_status(conn_factory, order_id: int, status: str,
                              filled_qty: int = 0,
                              avg_fill_price_cents: Optional[int] = None) -> None:
    with conn_factory() as conn:
        conn.execute(
            """UPDATE paper_orders
               SET status = ?, filled_qty = filled_qty + ?,
                   avg_fill_price_cents = COALESCE(?, avg_fill_price_cents),
                   updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
               WHERE id = ?""",
            (status, int(filled_qty), avg_fill_price_cents, int(order_id)),
        )


def insert_paper_fill(conn_factory, *, user_id: str, order_id: int,
                      ticker: str, side: str, action: str, qty: int,
                      price_cents: int, realized_pnl_cents: int = 0) -> int:
    with conn_factory() as conn:
        cur = conn.execute(
            """INSERT INTO paper_fills
                  (user_id, order_id, ticker, side, action, qty,
                   price_cents, realized_pnl_cents, filled_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                       strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))""",
            (user_id, int(order_id), ticker, side, action, int(qty),
             int(price_cents), int(realized_pnl_cents)),
        )
        return cur.lastrowid


def upsert_paper_position(conn_factory, *, user_id: str, ticker: str,
                          side: str, qty: int, avg_price_cents: int) -> None:
    with conn_factory() as conn:
        conn.execute(
            """INSERT INTO paper_positions
                  (user_id, ticker, side, qty, avg_price_cents, updated_at)
               VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
               ON CONFLICT(user_id, ticker, side) DO UPDATE SET
                 qty = excluded.qty,
                 avg_price_cents = excluded.avg_price_cents,
                 updated_at = excluded.updated_at""",
            (user_id, ticker, side, int(qty), int(avg_price_cents)),
        )


def get_paper_position(conn_factory, user_id: str, ticker: str,
                       side: str) -> tuple[int, int]:
    """Return ``(qty, avg_price_cents)``; (0, 0) if no row."""
    with conn_factory(readonly=True) as conn:
        row = conn.execute(
            """SELECT qty, avg_price_cents FROM paper_positions
               WHERE user_id = ? AND ticker = ? AND side = ?""",
            (user_id, ticker, side),
        ).fetchone()
    if not row:
        return (0, 0)
    return (int(row["qty"] or 0), int(row["avg_price_cents"] or 0))


def list_paper_orders(conn_factory, user_id: str, status: Optional[str] = None,
                      limit: int = 100) -> list[dict]:
    with conn_factory(readonly=True) as conn:
        if status:
            rows = conn.execute(
                """SELECT * FROM paper_orders
                   WHERE user_id = ? AND status = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (user_id, status, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM paper_orders WHERE user_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (user_id, int(limit)),
            ).fetchall()
    return [dict(r) for r in rows]


def list_paper_positions(conn_factory, user_id: str) -> list[dict]:
    with conn_factory(readonly=True) as conn:
        rows = conn.execute(
            """SELECT * FROM paper_positions
               WHERE user_id = ? AND qty != 0
               ORDER BY ticker""",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def cancel_paper_order(conn_factory, user_id: str, order_id: int) -> bool:
    """Idempotent cancel — returns True if a working order was canceled.

    Refuses to cancel a fully-filled order; canceling a partially-
    filled order moves it to ``canceled`` and the filled portion stays
    in `paper_fills`.
    """
    with conn_factory() as conn:
        cur = conn.execute(
            """UPDATE paper_orders
               SET status = 'canceled',
                   updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
               WHERE id = ? AND user_id = ?
                 AND status IN ('working','partially_filled','accepted')""",
            (int(order_id), user_id),
        )
        return cur.rowcount > 0

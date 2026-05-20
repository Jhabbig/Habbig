"""Trade engine — the single entry point for placing or canceling orders.

Every order, paper or live, goes through one path:

    sanity check  → user-limit check → vault lookup (live only)
                  → fill simulation (paper) OR signed Kalshi call (live)
                  → audit log
                  → response

That's it. No code outside this module talks to Kalshi for trading or
manipulates `paper_orders`, `paper_fills`, or `paper_positions` — the
single-path discipline is what keeps the safety guarantees enforceable.

Modes
-----
``mode="paper"`` is the default. Orders are matched against a
caller-supplied orderbook snapshot (we don't go fetch one inside the
engine because the caller — typically `server.py` — already caches
orderbook fetches from the live Kalshi public endpoint).

``mode="live"`` requires an enrolled Kalshi credential. The engine
*never* upgrades a request from paper to live; the explicit string is
required on every call.

Audit
-----
Every decision (allow, reject, fill, cancel) writes one row to
`trade_audit` with the user, the action, and a JSON payload of the
detail. Privacy-sensitive fields (no full key material is ever in
scope here, but defensively we never log the order body for live
orders — only the order id Kalshi gives back).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass
from typing import Optional

import credential_vault as _vault
import kalshi_signing as _ks
import paper_trading as _paper
import trade_safety as _safety

logger = logging.getLogger(__name__)


@dataclass
class PlaceOrderResult:
    """Outcome of `place_order`. Either a successful order id (paper or
    Kalshi-issued) plus the fills, or an explicit rejection with code +
    reason that the caller can surface to the user."""
    ok: bool
    order_id: Optional[int] = None  # paper row id
    kalshi_order_id: Optional[str] = None
    status: Optional[str] = None
    fills: list = None  # list[dict]
    code: Optional[str] = None
    reason: Optional[str] = None
    extra: Optional[dict] = None


def _audit(conn_factory, *, user_id: str, action: str, detail: dict,
           ip_addr: Optional[str] = None) -> None:
    try:
        with conn_factory() as conn:
            conn.execute(
                """INSERT INTO trade_audit (user_id, action, detail, ip_addr)
                   VALUES (?, ?, ?, ?)""",
                (user_id, action,
                 json.dumps(detail, default=str, separators=(",", ":")),
                 ip_addr),
            )
    except Exception as e:
        # The audit log is best-effort — never let logging failures
        # break a real trade.
        logger.warning("trade_audit insert failed: %s", e)


def place_order(conn_factory, *, user_id: str, ticker: str, side: str,
                action: str, qty: int, type_: str = "limit",
                limit_price_cents: Optional[int] = None,
                mode: str = "paper",
                orderbook: Optional[_paper.Orderbook] = None,
                client_order_id: Optional[str] = None,
                ip_addr: Optional[str] = None) -> PlaceOrderResult:
    """Place one order. See module docstring for the pipeline.

    `client_order_id` is auto-generated when omitted so retries on
    network failure don't double-place a live order.
    """
    client_order_id = client_order_id or f"narve-{uuid.uuid4().hex[:16]}"

    sanity = _safety.check_sanity(
        ticker=ticker, side=side, action=action, qty=qty,
        limit_price_cents=limit_price_cents, type_=type_,
    )
    if not sanity.allow:
        _audit(conn_factory, user_id=user_id, action="rejected",
               detail={"stage": "sanity", "code": sanity.code,
                       "reason": sanity.reason, "ticker": ticker,
                       "side": side, "qty": qty, "mode": mode},
               ip_addr=ip_addr)
        return PlaceOrderResult(ok=False, code=sanity.code, reason=sanity.reason)

    user_check = _safety.check_user(
        conn_factory, user_id=user_id, ticker=ticker, qty=qty,
        limit_price_cents=limit_price_cents, action=action,
    )
    if not user_check.allow:
        _audit(conn_factory, user_id=user_id, action="rejected",
               detail={"stage": "user_limits", "code": user_check.code,
                       "reason": user_check.reason, "mode": mode,
                       "ticker": ticker, "qty": qty},
               ip_addr=ip_addr)
        return PlaceOrderResult(ok=False, code=user_check.code,
                                reason=user_check.reason)

    if mode == "paper":
        return _place_paper(conn_factory, user_id=user_id, ticker=ticker,
                            side=side, action=action, qty=qty, type_=type_,
                            limit_price_cents=limit_price_cents,
                            orderbook=orderbook,
                            client_order_id=client_order_id, ip_addr=ip_addr)
    if mode == "live":
        return _place_live(conn_factory, user_id=user_id, ticker=ticker,
                           side=side, action=action, qty=qty, type_=type_,
                           limit_price_cents=limit_price_cents,
                           client_order_id=client_order_id, ip_addr=ip_addr)
    return PlaceOrderResult(ok=False, code="bad_mode",
                            reason=f"unknown mode {mode!r}")


def _place_paper(conn_factory, *, user_id: str, ticker: str, side: str,
                 action: str, qty: int, type_: str,
                 limit_price_cents: Optional[int],
                 orderbook: Optional[_paper.Orderbook],
                 client_order_id: str, ip_addr: Optional[str]) -> PlaceOrderResult:
    if orderbook is None:
        # No book → we can't fill any of it; record the order as working
        # and let `settle_paper_orders_with_book` close it later.
        oid = _paper.insert_paper_order(
            conn_factory, user_id=user_id, ticker=ticker, side=side,
            action=action, qty=qty, limit_price_cents=limit_price_cents,
            type_=type_, status="working", client_order_id=client_order_id,
        )
        _audit(conn_factory, user_id=user_id, action="placed_paper_no_book",
               detail={"order_id": oid, "ticker": ticker, "side": side,
                       "action": action, "qty": qty,
                       "limit_price_cents": limit_price_cents},
               ip_addr=ip_addr)
        return PlaceOrderResult(ok=True, order_id=oid, status="working", fills=[])

    fills, unfilled = _paper.simulate_fills(
        orderbook, side=side, action=action, qty=qty, type_=type_,
        limit_price_cents=limit_price_cents,
    )
    if type_ == "market" and unfilled > 0:
        _audit(conn_factory, user_id=user_id, action="rejected",
               detail={"stage": "no_depth", "ticker": ticker, "qty": qty,
                       "side": side, "action": action, "filled_partial": len(fills)},
               ip_addr=ip_addr)
        return PlaceOrderResult(ok=False, code="no_depth",
                                reason="market order exceeded available depth")

    if not fills:
        if type_ == "limit":
            oid = _paper.insert_paper_order(
                conn_factory, user_id=user_id, ticker=ticker, side=side,
                action=action, qty=qty, limit_price_cents=limit_price_cents,
                type_=type_, status="working", client_order_id=client_order_id,
            )
            _audit(conn_factory, user_id=user_id, action="placed_paper_working",
                   detail={"order_id": oid, "ticker": ticker, "side": side,
                           "action": action, "qty": qty,
                           "limit_price_cents": limit_price_cents},
                   ip_addr=ip_addr)
            return PlaceOrderResult(ok=True, order_id=oid, status="working", fills=[])
        return PlaceOrderResult(ok=False, code="no_fill",
                                reason="market order did not fill against the book")

    filled_qty = sum(f.qty for f in fills)
    avg_price = round(sum(f.qty * f.price_cents for f in fills) / filled_qty)
    status = "filled" if unfilled == 0 else "partially_filled"

    oid = _paper.insert_paper_order(
        conn_factory, user_id=user_id, ticker=ticker, side=side,
        action=action, qty=qty, limit_price_cents=limit_price_cents,
        type_=type_, status=status, client_order_id=client_order_id,
    )
    _paper.update_paper_order_status(
        conn_factory, oid, status=status, filled_qty=filled_qty,
        avg_fill_price_cents=avg_price,
    )

    # Apply fills to the position one at a time so realized PnL booking
    # matches the historical sequence (vs aggregating which loses the
    # close-then-open-opposite breakdown).
    fill_records = []
    for f in fills:
        prior_qty, prior_avg = _paper.get_paper_position(conn_factory, user_id, ticker, side)
        upd = _paper.update_position_after_fill(
            prior_qty=prior_qty, prior_avg_cents=prior_avg,
            action=action, fill_qty=f.qty, fill_price_cents=f.price_cents,
        )
        _paper.upsert_paper_position(
            conn_factory, user_id=user_id, ticker=ticker, side=side,
            qty=upd.new_qty, avg_price_cents=upd.avg_price_cents,
        )
        _paper.insert_paper_fill(
            conn_factory, user_id=user_id, order_id=oid, ticker=ticker,
            side=side, action=action, qty=f.qty, price_cents=f.price_cents,
            realized_pnl_cents=upd.realized_pnl_cents,
        )
        fill_records.append({
            "qty": f.qty, "price_cents": f.price_cents,
            "realized_pnl_cents": upd.realized_pnl_cents,
        })

    _audit(conn_factory, user_id=user_id, action="placed_paper",
           detail={"order_id": oid, "ticker": ticker, "side": side,
                   "action": action, "qty": qty, "fills": fill_records,
                   "status": status},
           ip_addr=ip_addr)
    return PlaceOrderResult(ok=True, order_id=oid, status=status,
                            fills=fill_records)


def _place_live(conn_factory, *, user_id: str, ticker: str, side: str,
                action: str, qty: int, type_: str,
                limit_price_cents: Optional[int],
                client_order_id: str, ip_addr: Optional[str]) -> PlaceOrderResult:
    creds = _vault.load_credentials(conn_factory, user_id)
    if not creds:
        _audit(conn_factory, user_id=user_id, action="rejected",
               detail={"stage": "no_credentials", "mode": "live"},
               ip_addr=ip_addr)
        return PlaceOrderResult(ok=False, code="not_enrolled",
                                reason="no Kalshi credentials on file")
    base = _ks.KALSHI_DEMO_BASE if creds.is_demo else _ks.KALSHI_BASE
    client = _ks.KalshiSignedClient(creds.key_id, creds.private_key, base_url=base)

    yes_p = limit_price_cents if side == "yes" else None
    no_p = limit_price_cents if side == "no" else None
    status_code, body = client.place_order(
        ticker=ticker, side=side, action=action, count=int(qty),
        type_=type_, yes_price_cents=yes_p, no_price_cents=no_p,
        client_order_id=client_order_id,
    )

    body = body or {}
    kalshi_order_id = None
    if isinstance(body, dict):
        order_obj = body.get("order") or {}
        if isinstance(order_obj, dict):
            kalshi_order_id = order_obj.get("order_id") or order_obj.get("id")

    log_status = "submitted" if 200 <= status_code < 300 else "error"
    with conn_factory() as conn:
        cur = conn.execute(
            """INSERT INTO live_order_log
                  (user_id, kalshi_order_id, ticker, side, action, qty,
                   type, limit_price_cents, status, http_status,
                   client_order_id, response_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, kalshi_order_id, ticker, side, action, int(qty),
             type_, limit_price_cents, log_status, int(status_code),
             client_order_id,
             json.dumps(body, default=str, separators=(",", ":"))[:8000]),
        )
        log_id = cur.lastrowid

    _audit(conn_factory, user_id=user_id,
           action="placed_live" if log_status == "submitted" else "live_error",
           detail={"log_id": log_id, "kalshi_order_id": kalshi_order_id,
                   "http_status": status_code, "ticker": ticker,
                   "side": side, "action": action, "qty": qty},
           ip_addr=ip_addr)

    if log_status == "submitted":
        return PlaceOrderResult(ok=True, kalshi_order_id=kalshi_order_id,
                                status="submitted", fills=[],
                                extra={"http_status": status_code})
    return PlaceOrderResult(ok=False, code="kalshi_error",
                            reason=f"Kalshi returned HTTP {status_code}",
                            extra={"http_status": status_code, "body": body})


def cancel_order(conn_factory, *, user_id: str, mode: str,
                 order_id: Optional[int] = None,
                 kalshi_order_id: Optional[str] = None,
                 ip_addr: Optional[str] = None) -> dict:
    """Cancel a paper order by id or a live order by Kalshi order id."""
    if mode == "paper":
        if order_id is None:
            return {"ok": False, "code": "missing_order_id"}
        ok = _paper.cancel_paper_order(conn_factory, user_id, order_id)
        _audit(conn_factory, user_id=user_id,
               action="canceled_paper" if ok else "cancel_noop",
               detail={"order_id": order_id}, ip_addr=ip_addr)
        return {"ok": ok}
    if mode == "live":
        if not kalshi_order_id:
            return {"ok": False, "code": "missing_order_id"}
        creds = _vault.load_credentials(conn_factory, user_id)
        if not creds:
            return {"ok": False, "code": "not_enrolled"}
        base = _ks.KALSHI_DEMO_BASE if creds.is_demo else _ks.KALSHI_BASE
        client = _ks.KalshiSignedClient(creds.key_id, creds.private_key, base_url=base)
        status_code, body = client.cancel_order(kalshi_order_id)
        ok = 200 <= status_code < 300
        _audit(conn_factory, user_id=user_id,
               action="canceled_live" if ok else "cancel_live_error",
               detail={"kalshi_order_id": kalshi_order_id,
                       "http_status": status_code},
               ip_addr=ip_addr)
        return {"ok": ok, "http_status": status_code, "body": body}
    return {"ok": False, "code": "bad_mode"}


def get_summary(conn_factory, user_id: str) -> dict:
    """Combined paper-side stats — open orders, positions, today's
    realized PnL, daily wagered, limits — for the trade UI summary
    panel."""
    limits = _safety.get_user_limits(conn_factory, user_id)
    return {
        "user_id": user_id,
        "limits": asdict(limits),
        "daily_wagered_usd": _safety.get_daily_wagered_usd(conn_factory, user_id),
        "daily_realized_pnl_usd": _safety.get_daily_realized_pnl(conn_factory, user_id),
        "open_position_count": _safety.get_open_position_count(conn_factory, user_id),
        "enrolled_for_live": _vault.credentials_exist(conn_factory, user_id),
        "open_orders": _paper.list_paper_orders(conn_factory, user_id, status="working", limit=50),
        "positions": _paper.list_paper_positions(conn_factory, user_id),
    }


def _list_working_orders(conn_factory) -> list[dict]:
    """Pull every paper order with remaining open qty across all users."""
    with conn_factory(readonly=True) as conn:
        rows = conn.execute(
            """SELECT id, user_id, ticker, side, action, qty, filled_qty,
                      limit_price_cents, type, status
               FROM paper_orders
               WHERE status IN ('working','partially_filled','accepted')
                 AND filled_qty < qty"""
        ).fetchall()
    return [dict(r) for r in rows]


def settle_working_orders(conn_factory, orderbook_fetcher) -> dict:
    """Re-attempt every open paper order against a fresh orderbook.

    `orderbook_fetcher(ticker) -> Orderbook | None` is injected so the
    settlement loop can use the server's cached fetcher without this
    module depending on Flask/server.py. Returns a small stats dict the
    background loop can log.

    Behavior:
      * For each working order, fetch the current book and call
        `simulate_fills` with the *remaining* qty (qty − filled_qty).
      * Each new fill is persisted (position update + paper_fills row)
        exactly like a fresh order.
      * Order status moves to `partially_filled` if some new qty filled
        but not all, `filled` if the full original qty has now been
        reached, and stays `working` otherwise.
      * We skip the per-user safety check on settlement — these are
        already-accepted orders. The kill switch and per-user caps
        applied at place time still bound exposure.
    """
    open_orders = _list_working_orders(conn_factory)
    stats = {"checked": len(open_orders), "filled_orders": 0,
             "new_fills": 0, "errors": 0}
    if not open_orders:
        return stats

    # Group by ticker so we only fetch each orderbook once per pass
    books: dict = {}
    for o in open_orders:
        if o["ticker"] not in books:
            try:
                books[o["ticker"]] = orderbook_fetcher(o["ticker"])
            except Exception:
                books[o["ticker"]] = None
                stats["errors"] += 1

    for o in open_orders:
        book = books.get(o["ticker"])
        if book is None:
            continue
        remaining = int(o["qty"]) - int(o["filled_qty"] or 0)
        if remaining <= 0:
            continue
        try:
            fills, unfilled = _paper.simulate_fills(
                book, side=o["side"], action=o["action"], qty=remaining,
                type_=o["type"], limit_price_cents=o["limit_price_cents"],
            )
        except Exception:
            stats["errors"] += 1
            continue
        if not fills:
            continue

        newly_filled_qty = sum(f.qty for f in fills)
        new_avg_price = round(sum(f.qty * f.price_cents for f in fills) / newly_filled_qty)
        total_filled_after = int(o["filled_qty"] or 0) + newly_filled_qty
        new_status = "filled" if total_filled_after >= int(o["qty"]) else "partially_filled"

        _paper.update_paper_order_status(
            conn_factory, o["id"], status=new_status,
            filled_qty=newly_filled_qty, avg_fill_price_cents=new_avg_price,
        )
        # Apply each fill one at a time so realized PnL booking is right
        for f in fills:
            prior_qty, prior_avg = _paper.get_paper_position(
                conn_factory, o["user_id"], o["ticker"], o["side"])
            upd = _paper.update_position_after_fill(
                prior_qty=prior_qty, prior_avg_cents=prior_avg,
                action=o["action"], fill_qty=f.qty, fill_price_cents=f.price_cents,
            )
            _paper.upsert_paper_position(
                conn_factory, user_id=o["user_id"], ticker=o["ticker"],
                side=o["side"], qty=upd.new_qty, avg_price_cents=upd.avg_price_cents,
            )
            _paper.insert_paper_fill(
                conn_factory, user_id=o["user_id"], order_id=o["id"],
                ticker=o["ticker"], side=o["side"], action=o["action"],
                qty=f.qty, price_cents=f.price_cents,
                realized_pnl_cents=upd.realized_pnl_cents,
            )
            stats["new_fills"] += 1

        if new_status == "filled":
            stats["filled_orders"] += 1
        _audit(conn_factory, user_id=o["user_id"], action="settled_paper",
               detail={"order_id": o["id"], "ticker": o["ticker"],
                       "new_fills": len(fills),
                       "filled_qty_now": total_filled_after,
                       "status": new_status})
    return stats

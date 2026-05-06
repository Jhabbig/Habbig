"""Pre-trade safety checks.

Every order — paper or live — passes through this module before
hitting the engine. Three categories of check:

1. **Sanity** — qty > 0 integer, side ∈ {yes, no}, action ∈
   {buy, sell}, price within 1¢..99¢ for limit orders, ticker matches
   the Kalshi alphanumeric format. These reject malformed input
   regardless of user state.

2. **Per-user limits** — max single-order USD, max daily wagered USD,
   max open positions, max position size per market. Stored in
   `trade_user_limits`. Defaults are conservative — a brand-new user
   gets a $25 / $100 / 5-position cap until they opt in to higher.

3. **Kill switch** — admin-set per-user OR global. Trips when daily
   loss exceeds the user's stop, when too many consecutive rejections
   come back from Kalshi, or by manual admin override. While killed,
   the user can still cancel existing orders but cannot place new
   ones.

The decision is returned as a `SafetyDecision` object so the caller
can record both the outcome and the reason in the audit log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# Kalshi tickers look like KXMARKET-26AUG31-T80 — we accept up to 64
# alphanumeric / hyphen / underscore chars. Tighter than the API allows
# on purpose; reject novel formats so we don't pass garbage upstream.
TICKER_PATTERN = re.compile(r"^[A-Z0-9_\-]{2,64}$", re.IGNORECASE)

VALID_SIDES = {"yes", "no"}
VALID_ACTIONS = {"buy", "sell"}
VALID_TYPES = {"limit", "market"}

# Conservative defaults; admin or user-opt-in can raise these.
DEFAULT_MAX_ORDER_USD = 25.0
DEFAULT_MAX_DAILY_USD = 100.0
DEFAULT_MAX_OPEN_POSITIONS = 5
DEFAULT_MAX_POSITION_USD = 100.0
DEFAULT_DAILY_LOSS_LIMIT_USD = 50.0


@dataclass
class SafetyDecision:
    """Result of `check_order`. `allow=True` means proceed; `allow=False`
    means refuse with `code` (machine-readable) and `reason`
    (human-readable, safe to surface to the user)."""
    allow: bool
    code: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class UserLimits:
    """Per-user trading caps — read from `trade_user_limits` (or
    populated with defaults when the user has no row yet)."""
    user_id: str
    max_order_usd: float = DEFAULT_MAX_ORDER_USD
    max_daily_usd: float = DEFAULT_MAX_DAILY_USD
    max_open_positions: int = DEFAULT_MAX_OPEN_POSITIONS
    max_position_usd: float = DEFAULT_MAX_POSITION_USD
    daily_loss_limit_usd: float = DEFAULT_DAILY_LOSS_LIMIT_USD
    killed: bool = False
    kill_reason: Optional[str] = None


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_user_limits(conn_factory, user_id: str) -> UserLimits:
    """Load the user's limits row, or return defaults if missing."""
    if not user_id:
        return UserLimits(user_id="")
    with conn_factory(readonly=True) as conn:
        row = conn.execute(
            """SELECT max_order_usd, max_daily_usd, max_open_positions,
                      max_position_usd, daily_loss_limit_usd, killed, kill_reason
               FROM trade_user_limits WHERE user_id = ?""",
            (user_id,),
        ).fetchone()
    if not row:
        return UserLimits(user_id=user_id)
    return UserLimits(
        user_id=user_id,
        max_order_usd=float(row["max_order_usd"]) if row["max_order_usd"] is not None else DEFAULT_MAX_ORDER_USD,
        max_daily_usd=float(row["max_daily_usd"]) if row["max_daily_usd"] is not None else DEFAULT_MAX_DAILY_USD,
        max_open_positions=int(row["max_open_positions"]) if row["max_open_positions"] is not None else DEFAULT_MAX_OPEN_POSITIONS,
        max_position_usd=float(row["max_position_usd"]) if row["max_position_usd"] is not None else DEFAULT_MAX_POSITION_USD,
        daily_loss_limit_usd=float(row["daily_loss_limit_usd"]) if row["daily_loss_limit_usd"] is not None else DEFAULT_DAILY_LOSS_LIMIT_USD,
        killed=bool(row["killed"]),
        kill_reason=row["kill_reason"],
    )


def set_user_limits(conn_factory, user_id: str, **fields) -> None:
    """Upsert the user's limits row. Only known fields are accepted."""
    allowed = {"max_order_usd", "max_daily_usd", "max_open_positions",
               "max_position_usd", "daily_loss_limit_usd",
               "killed", "kill_reason"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    update_cols = ", ".join(f"{k} = excluded.{k}" for k in fields.keys())
    sql = (f"INSERT INTO trade_user_limits (user_id, {cols}) "
           f"VALUES (?, {placeholders}) "
           f"ON CONFLICT(user_id) DO UPDATE SET {update_cols}, "
           f"updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')")
    with conn_factory() as conn:
        conn.execute(sql, (user_id, *fields.values()))


def get_daily_wagered_usd(conn_factory, user_id: str) -> float:
    """Sum of all orders placed (paper + live) by the user today."""
    if not user_id:
        return 0.0
    today = _today_iso()
    with conn_factory(readonly=True) as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(qty * limit_price_cents / 100.0), 0) AS s
               FROM paper_orders
               WHERE user_id = ? AND substr(created_at, 1, 10) = ?""",
            (user_id, today),
        ).fetchone()
        paper_total = float(row["s"]) if row else 0.0
        row = conn.execute(
            """SELECT COALESCE(SUM(qty * limit_price_cents / 100.0), 0) AS s
               FROM live_order_log
               WHERE user_id = ? AND substr(ts, 1, 10) = ?""",
            (user_id, today),
        ).fetchone()
        live_total = float(row["s"]) if row else 0.0
    return round(paper_total + live_total, 4)


def get_daily_realized_pnl(conn_factory, user_id: str) -> float:
    """Realized PnL today across paper + live (paper only for now —
    live PnL comes from Kalshi fills which we mirror later)."""
    if not user_id:
        return 0.0
    today = _today_iso()
    with conn_factory(readonly=True) as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(realized_pnl_cents), 0) AS s
               FROM paper_fills
               WHERE user_id = ? AND substr(filled_at, 1, 10) = ?""",
            (user_id, today),
        ).fetchone()
    return round((float(row["s"]) if row else 0.0) / 100.0, 4)


def get_open_position_count(conn_factory, user_id: str) -> int:
    if not user_id:
        return 0
    with conn_factory(readonly=True) as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM paper_positions
               WHERE user_id = ? AND qty != 0""",
            (user_id,),
        ).fetchone()
    return int(row["n"]) if row else 0


def check_sanity(*, ticker: str, side: str, action: str, qty: int,
                 limit_price_cents: Optional[int] = None,
                 type_: str = "limit") -> SafetyDecision:
    """Pure validation — same answer regardless of user. Used by both
    paper and live paths so we don't double-implement."""
    if not ticker or not TICKER_PATTERN.match(ticker):
        return SafetyDecision(False, "bad_ticker", "ticker is malformed")
    if side not in VALID_SIDES:
        return SafetyDecision(False, "bad_side", f"side must be one of {sorted(VALID_SIDES)}")
    if action not in VALID_ACTIONS:
        return SafetyDecision(False, "bad_action", f"action must be one of {sorted(VALID_ACTIONS)}")
    if type_ not in VALID_TYPES:
        return SafetyDecision(False, "bad_type", f"type must be one of {sorted(VALID_TYPES)}")
    try:
        qty_int = int(qty)
    except (TypeError, ValueError):
        return SafetyDecision(False, "bad_qty", "qty must be an integer")
    if qty_int <= 0:
        return SafetyDecision(False, "bad_qty", "qty must be positive")
    if qty_int > 10_000:
        return SafetyDecision(False, "bad_qty", "qty exceeds hard ceiling 10000")
    if type_ == "limit":
        if limit_price_cents is None:
            return SafetyDecision(False, "bad_price", "limit orders require a price")
        try:
            p = int(limit_price_cents)
        except (TypeError, ValueError):
            return SafetyDecision(False, "bad_price", "price must be an integer in cents")
        if p < 1 or p > 99:
            return SafetyDecision(False, "bad_price",
                                  "limit price must be between 1¢ and 99¢")
    return SafetyDecision(allow=True)


def check_user(conn_factory, *, user_id: str, ticker: str, qty: int,
               limit_price_cents: Optional[int],
               action: str) -> SafetyDecision:
    """Per-user limit checks. Assumes sanity already passed.

    Combines: kill switch, max single-order USD, daily wagered USD,
    daily realized loss, max open positions. Position-per-market cap
    is checked via the engine because that needs current position
    state, not just counts.
    """
    if not user_id:
        return SafetyDecision(False, "no_user", "user not authenticated")
    limits = get_user_limits(conn_factory, user_id)
    if limits.killed:
        return SafetyDecision(False, "killed",
                              f"trading disabled: {limits.kill_reason or 'admin'}")
    # Market orders without a limit price: bound by 99¢ (the maximum any
    # YES/NO contract can ever cost). Using max_position_usd as a proxy
    # would over-reject — that's a position sizing rule, not a price.
    price = (limit_price_cents / 100.0) if limit_price_cents is not None else 0.99
    order_usd = round(price * int(qty), 4)
    if order_usd > limits.max_order_usd:
        return SafetyDecision(False, "over_order_cap",
                              f"order ${order_usd:.2f} exceeds your ${limits.max_order_usd:.2f} per-order cap")
    daily_wagered = get_daily_wagered_usd(conn_factory, user_id)
    if daily_wagered + order_usd > limits.max_daily_usd:
        return SafetyDecision(False, "over_daily_cap",
                              f"order would push daily wagered to "
                              f"${daily_wagered + order_usd:.2f} (limit ${limits.max_daily_usd:.2f})")
    realized_today = get_daily_realized_pnl(conn_factory, user_id)
    if realized_today <= -limits.daily_loss_limit_usd:
        return SafetyDecision(False, "daily_stop_hit",
                              f"daily loss ${realized_today:.2f} hit your stop "
                              f"of ${limits.daily_loss_limit_usd:.2f}")
    # Only buys count toward the open-positions cap (sells reduce, not increase)
    if action == "buy":
        open_n = get_open_position_count(conn_factory, user_id)
        if open_n >= limits.max_open_positions:
            return SafetyDecision(False, "too_many_positions",
                                  f"already have {open_n} open positions "
                                  f"(limit {limits.max_open_positions})")
    return SafetyDecision(allow=True)

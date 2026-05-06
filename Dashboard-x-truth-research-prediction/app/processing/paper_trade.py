"""Paper-trade ledger logic.

The dashboard surfaces "best bets" but never tracked whether following them
would have made money. This module closes that loop:

  - Open a $1 paper trade whenever a freshly-ranked prediction clears the
    EV/credibility/risk thresholds.
  - When the matching market resolves, settle the trade and record P&L.

Stake is fixed at $1/signal so the running P&L equals the realised return per
signal. That keeps the math comparable across sources and across time.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import select

from app.config import yaml_config
from app.db import AsyncSession
from app.models import PaperTrade, Prediction, Source

logger = logging.getLogger(__name__)


_PAPER_CFG = yaml_config.get("paper_trading", {}) or {}
DEFAULT_MIN_EV: float = float(_PAPER_CFG.get("min_ev_score", 0.10))
DEFAULT_MIN_CRED: float = float(_PAPER_CFG.get("min_global_credibility", 0.55))
DEFAULT_STAKE_USD: float = float(_PAPER_CFG.get("stake_usd", 1.0))


@dataclass
class TradeFilter:
    """Threshold knobs for both live opens and the backtest harness."""
    min_ev: float = DEFAULT_MIN_EV
    min_credibility: float = DEFAULT_MIN_CRED
    stake_usd: float = DEFAULT_STAKE_USD
    require_unlocked: bool = True
    require_no_risk_flag: bool = True


def _entry_price(market_yes_price: float, side: str) -> float:
    return market_yes_price if side.upper() == "YES" else max(0.0, 1.0 - market_yes_price)


def qualifies(pred: Prediction, source: Source | None, filt: TradeFilter) -> bool:
    if pred.market_slug is None or pred.market_implied_probability is None:
        return False
    if pred.ev_score is None or pred.ev_score < filt.min_ev:
        return False
    if filt.require_no_risk_flag and pred.risk_flag:
        return False
    if source is None:
        return False
    if filt.require_unlocked and not source.accuracy_unlocked:
        return False
    if source.global_credibility < filt.min_credibility:
        return False
    if pred.market_implied_probability <= 0.01 or pred.market_implied_probability >= 0.99:
        return False
    return True


def settle_pnl(stake: float, entry_price: float, won: bool) -> float:
    """Realised P&L on a $stake bet bought at entry_price (in [0, 1])."""
    if entry_price <= 0:
        return -stake
    if not won:
        return -stake
    return stake * (1.0 / entry_price - 1.0)


async def maybe_open_trade(
    session: AsyncSession,
    pred: Prediction,
    source: Source | None,
    handle: str,
    filt: TradeFilter | None = None,
) -> Optional[PaperTrade]:
    filt = filt or TradeFilter()
    if not qualifies(pred, source, filt):
        return None
    # One open trade per (handle, market_slug) to avoid spam-padding from the same source.
    existing = await session.exec(
        select(PaperTrade).where(
            PaperTrade.handle == handle,
            PaperTrade.market_slug == pred.market_slug,
            PaperTrade.resolved == False,  # noqa: E712
        )
    )
    if existing.first():
        return None
    side = (pred.bet_side or "YES").upper()
    entry = _entry_price(pred.market_implied_probability, side)
    trade = PaperTrade(
        prediction_id=pred.id,
        handle=handle,
        market_slug=pred.market_slug,
        platform="polymarket",  # the resolver doesn't currently distinguish; backtest treats both alike
        bet_side=side,
        stake_usd=filt.stake_usd,
        entry_price=entry,
        entry_ev_score=pred.ev_score or 0.0,
        entry_credibility=source.global_credibility if source else 0.0,
        opened_at=datetime.now(timezone.utc),
    )
    session.add(trade)
    return trade


async def settle_trades_for_market(session: AsyncSession, market_slug: str, won_yes: Optional[bool]) -> int:
    """Settle every open trade on this market.

    won_yes:
      True  -> YES side was the winning outcome
      False -> NO side won
      None  -> non-binary outcome; we leave trades open (resolver will fall back to text-match logic).
    """
    if won_yes is None:
        return 0
    open_trades = await session.exec(
        select(PaperTrade).where(PaperTrade.market_slug == market_slug, PaperTrade.resolved == False)  # noqa: E712
    )
    settled = 0
    for trade in open_trades.all():
        won = (trade.bet_side.upper() == "YES" and won_yes) or (trade.bet_side.upper() == "NO" and not won_yes)
        trade.resolved = True
        trade.resolved_correct = won
        trade.pnl_usd = settle_pnl(trade.stake_usd, trade.entry_price, won)
        trade.closed_at = datetime.now(timezone.utc)
        session.add(trade)
        settled += 1
    return settled


async def summary(session: AsyncSession) -> dict:
    """Aggregate ledger metrics — open count, settled count, total P&L, hit rate, ROI."""
    closed = await session.exec(select(PaperTrade).where(PaperTrade.resolved == True))  # noqa: E712
    closed_rows = closed.all()
    open_q = await session.exec(select(PaperTrade).where(PaperTrade.resolved == False))  # noqa: E712
    open_count = len(open_q.all())
    if not closed_rows:
        return {
            "open": open_count,
            "settled": 0,
            "wins": 0,
            "losses": 0,
            "hit_rate": None,
            "total_pnl_usd": 0.0,
            "total_staked_usd": 0.0,
            "roi": None,
        }
    wins = sum(1 for t in closed_rows if t.resolved_correct)
    losses = len(closed_rows) - wins
    total_pnl = sum((t.pnl_usd or 0.0) for t in closed_rows)
    total_stake = sum(t.stake_usd for t in closed_rows)
    return {
        "open": open_count,
        "settled": len(closed_rows),
        "wins": wins,
        "losses": losses,
        "hit_rate": wins / len(closed_rows) if closed_rows else None,
        "total_pnl_usd": round(total_pnl, 2),
        "total_staked_usd": round(total_stake, 2),
        "roi": round(total_pnl / total_stake, 4) if total_stake > 0 else None,
    }


async def backtest(session: AsyncSession, filt: TradeFilter) -> dict:
    """Replay every resolved prediction under the given filter and report P&L.

    This is the offline complement to ``maybe_open_trade`` — same qualification
    logic, same settlement math, applied to every historical prediction whose
    market has resolved. The point is to validate the (EV, credibility)
    threshold knobs before turning them on in production.
    """
    from app.models import RawPost, SourcePredictionRecord
    # Pre-load every source keyed by handle so the inner loop does an O(1)
    # dict lookup rather than re-hitting the DB per prediction.
    src_result = await session.exec(select(Source))
    sources_by_handle: dict[str, Source] = {s.handle: s for s in src_result.all()}
    spr_result = await session.exec(select(SourcePredictionRecord))
    handle_for_pred: dict[int, str] = {r.prediction_id: r.handle for r in spr_result.all() if r.prediction_id}
    # Backfill from RawPost for predictions without an SPR row.
    rp_result = await session.exec(select(RawPost))
    handle_for_post: dict[str, str] = {p.id: p.author_handle for p in rp_result.all()}

    stmt = select(Prediction).where(Prediction.resolved == True, Prediction.resolved_correct.isnot(None))  # noqa: E712
    rows = (await session.exec(stmt)).all()
    n = wins = 0
    total_pnl = 0.0
    by_category: dict[str, dict[str, float]] = {}
    for pred in rows:
        handle = handle_for_pred.get(pred.id) or handle_for_post.get(pred.raw_post_id, "")
        source = sources_by_handle.get(handle)
        if not qualifies(pred, source, filt):
            continue
        n += 1
        side = (pred.bet_side or "YES").upper()
        entry = _entry_price(pred.market_implied_probability or 0.5, side)
        won = bool(pred.resolved_correct)
        if won:
            wins += 1
        pnl = settle_pnl(filt.stake_usd, entry, won)
        total_pnl += pnl
        cat = pred.category or "other"
        cb = by_category.setdefault(cat, {"n": 0, "wins": 0, "pnl": 0.0})
        cb["n"] += 1
        cb["wins"] += 1 if won else 0
        cb["pnl"] += pnl
    return {
        "n_signals": n,
        "wins": wins,
        "losses": n - wins,
        "hit_rate": wins / n if n else None,
        "total_pnl_usd": round(total_pnl, 2),
        "roi": round(total_pnl / (filt.stake_usd * n), 4) if n else None,
        "by_category": {k: {"n": v["n"], "wins": v["wins"], "pnl": round(v["pnl"], 2)} for k, v in by_category.items()},
        "filter": {"min_ev": filt.min_ev, "min_credibility": filt.min_credibility, "stake_usd": filt.stake_usd},
    }

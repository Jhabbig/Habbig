"""CLI dashboard — summary of positions, signals, and PnL."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from datastore import DataStore
from config import Config

logger = logging.getLogger(__name__)


def print_run_summary(
    markets_scanned: int,
    signals: list,
    trades_executed: int,
    config: Optional[Config] = None,
) -> None:
    """Print a clean summary after each bot run."""
    config = config or Config()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    actionable = [s for s in signals if s.action != "NO_TRADE"]
    buy_yes = [s for s in signals if s.action == "BUY_YES"]
    buy_no = [s for s in signals if s.action == "BUY_NO"]

    mode = "PAPER" if config.PAPER_MODE else "LIVE"

    print(f"\n{'='*70}")
    print(f"  POLYMARKET WEATHER BOT — {mode} MODE")
    print(f"  {now}")
    print(f"{'='*70}")
    print(f"  Markets scanned:    {markets_scanned}")
    print(f"  Signals found:      {len(actionable)} actionable / {len(signals)} total")
    print(f"    BUY YES:          {len(buy_yes)}")
    print(f"    BUY NO:           {len(buy_no)}")
    print(f"  Trades executed:    {trades_executed}")
    print(f"  Edge threshold:     {config.EDGE_THRESHOLD*100:.0f}%")
    print(f"{'='*70}")

    if actionable:
        print(f"\n  {'ACTION':<10} {'EDGE':>6} {'MODEL':>6} {'MKT':>6}  {'CITY':<12} {'QUESTION'}")
        print(f"  {'—'*10} {'—'*6} {'—'*6} {'—'*6}  {'—'*12} {'—'*40}")
        for s in actionable:
            print(
                f"  {s.action:<10} {s.edge*100:>+5.1f}% {s.model_prob*100:>5.1f}% "
                f"{s.market_prob*100:>5.1f}%  {s.market.city:<12} "
                f"{s.market.question[:45]}"
            )

    print()


def print_daily_report(store: DataStore, config: Optional[Config] = None) -> None:
    """Print daily summary from database."""
    config = config or Config()
    stats = store.get_today_stats()
    recent_trades = store.get_recent_trades(10)
    pnl = store.get_pnl_summary()

    mode = "PAPER" if config.PAPER_MODE else "LIVE"

    print(f"\n{'='*70}")
    print(f"  DAILY REPORT — {stats['date']} — {mode} MODE")
    print(f"{'='*70}")
    print(f"  Signals scanned:    {stats['signals_total']}")
    print(f"  Actionable signals: {stats['signals_actionable']}")
    print(f"  Trades placed:      {stats['trades_count']}")
    print(f"  Total wagered:      ${stats['total_amount']:.2f}")
    print(f"{'='*70}")

    if pnl["total_trades"] > 0:
        print(f"\n  ALL-TIME STATS")
        print(f"  Total trades:       {pnl['total_trades']}")
        print(f"  Total wagered:      ${pnl['total_wagered']:.2f}")

        if pnl["by_action"]:
            print(f"\n  By Action:")
            for action, data in pnl["by_action"].items():
                print(f"    {action:<12} {data['count']:>4} trades  ${data['total']:>8.2f}")

        if pnl["by_city"]:
            print(f"\n  By City:")
            for city, data in pnl["by_city"].items():
                print(f"    {city:<12} {data['count']:>4} trades  ${data['total']:>8.2f}")

    if recent_trades:
        print(f"\n  RECENT TRADES")
        print(f"  {'TIME':<20} {'ACTION':<10} {'SIDE':<5} {'AMT':>7} {'PRICE':>6} {'EDGE':>6}  {'CITY'}")
        print(f"  {'—'*20} {'—'*10} {'—'*5} {'—'*7} {'—'*6} {'—'*6}  {'—'*12}")
        for t in recent_trades:
            ts = t["timestamp"][:16].replace("T", " ")
            paper_tag = " [P]" if t["paper_mode"] else ""
            print(
                f"  {ts:<20} {t['action']:<10} {t['side']:<5} "
                f"${t['amount']:>6.2f} {t['price']:>5.3f} {t['edge']*100:>+5.1f}%  "
                f"{t['city']}{paper_tag}"
            )

    print()

"""Telegram notifications for high-conviction signals.

Sends a one-line bullet per qualifying paper-trade entry. Tokens are stored
encrypted on the User row (Fernet); this module only reads the resolved
plain-text values.

Notification rule: fires when the scheduler opens a paper-trade — the
qualification rules in ``processing/paper_trade.py`` are already where we
encode "this is a tradeable signal", so we don't try to second-guess them here.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

import httpx
from sqlmodel import select

from app.db import AsyncSession, engine
from app.models import PaperTrade, User
from app.security import decrypt_field

logger = logging.getLogger(__name__)


async def _telegram_subscribers() -> list[tuple[str, str]]:
    """Return (bot_token, chat_id) pairs for every user opted in to Telegram alerts."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.exec(
            select(User).where(
                User.telegram_bot_token != "",
                User.telegram_chat_id != "",
                User.telegram_alerts_enabled == True,  # noqa: E712
            )
        )
        return [
            (decrypt_field(u.telegram_bot_token), u.telegram_chat_id)
            for u in result.all()
        ]


def _format_trade(trade: PaperTrade, market_question: str | None = None) -> str:
    label = market_question or trade.market_slug
    return (
        f"📈 *narve.ai signal*\n"
        f"`{label[:120]}`\n"
        f"BUY *{trade.bet_side}* @ {trade.entry_price:.2f}  ·  EV {trade.entry_ev_score:+.2f}  ·  cred {trade.entry_credibility:.2f}\n"
        f"Source: @{trade.handle}"
    )


async def _post_telegram(client: httpx.AsyncClient, bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = await client.post(url, data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
        if resp.status_code >= 400:
            logger.warning("Telegram returned %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)


async def notify_new_trades(trades: Iterable[PaperTrade]) -> int:
    """Fan-out a list of just-opened trades to every subscriber. Returns number of messages sent."""
    trades = list(trades)
    if not trades:
        return 0
    subscribers = await _telegram_subscribers()
    if not subscribers:
        return 0
    messages = [_format_trade(t) for t in trades]
    sent = 0
    async with httpx.AsyncClient() as client:
        for token, chat_id in subscribers:
            for text in messages:
                await _post_telegram(client, token, chat_id, text)
                sent += 1
                # tiny delay so we don't trip Telegram's burst limiter
                await asyncio.sleep(0.05)
    return sent

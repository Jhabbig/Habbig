"""Outbound Telegram notifications — invoked by job handlers.

Kept separate from the bot's inbound handlers because the send path
doesn't need python-telegram-bot's Application glue; a plain POST to
the Bot API works and removes the heavy dep from the job worker's
process.

Jobs:
  send_telegram_alert(chat_id, message, parse_mode)
    Generic single-chat send. Used by the resolution job + insider
    alert job that already exist.

  send_telegram_best_bets(user_id)
    Wraps the best-bets fetch + per-user threshold filter + fmt. Called
    as part of the morning-briefing cron so Telegram-linked users get
    the same content as the email digest.

  send_telegram_market_mover(event_id)
    Fires from the market-movement detector. Uses stored thresholds
    on the telegram_connections row to decide who to notify.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

from jobs.registry import register_job


log = logging.getLogger("jobs.telegram")


_TG_API = "https://api.telegram.org"


def _bot_token() -> Optional[str]:
    val = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    return val or None


async def _send_raw(chat_id: int, text: str, parse_mode: Optional[str] = None) -> None:
    token = _bot_token()
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not set; skipping send to %s", chat_id)
        return
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        resp = await client.post(
            f"{_TG_API}/bot{token}/sendMessage", json=payload,
        )
        if resp.status_code != 200:
            log.warning("telegram send failed chat=%s status=%s body=%s",
                        chat_id, resp.status_code, resp.text[:200])


@register_job("send_telegram_alert")
async def send_telegram_alert(
    chat_id: int, message: str, parse_mode: str = "MarkdownV2",
) -> dict:
    """Generic one-shot send. Callers pre-format ``message``."""
    await _send_raw(chat_id, message, parse_mode=parse_mode)
    return {"sent": True, "chat_id": chat_id}


@register_job("send_telegram_best_bets")
async def send_telegram_best_bets(user_id: int) -> dict:
    """Send the current best-bets list to a specific user.

    Invoked from the morning-briefing cron per-user so each user's
    thresholds are honoured. Does nothing if the user isn't Telegram-
    linked.
    """
    import db
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM telegram_connections "
            "WHERE user_id = ? AND is_active = 1",
            (user_id,),
        ).fetchone()
    if not row:
        return {"sent": False, "reason": "not_linked"}

    if not row["send_best_bets"]:
        return {"sent": False, "reason": "opted_out"}

    from bots.formatters import format_best_bet_telegram, load_best_bets
    bets = load_best_bets(
        limit=5,
        min_ev=float(row["min_ev_threshold"] or 0.05),
        min_cred=float(row["min_credibility"] or 0.7),
    )
    if not bets:
        return {"sent": False, "reason": "no_bets"}

    header = "🌅 *narve\\.ai morning brief*"
    await _send_raw(int(row["telegram_chat_id"]), header, parse_mode="MarkdownV2")
    for bet in bets:
        await _send_raw(
            int(row["telegram_chat_id"]),
            format_best_bet_telegram(bet),
            parse_mode="MarkdownV2",
        )
    return {"sent": True, "count": len(bets)}


@register_job("send_telegram_market_mover")
async def send_telegram_market_mover(
    event_id: int, market_slug: str, summary: str,
) -> dict:
    """Fan out a market-mover alert to every linked user whose
    thresholds admit it."""
    import db
    with db.conn() as c:
        rows = c.execute(
            "SELECT telegram_chat_id FROM telegram_connections "
            "WHERE is_active = 1 AND send_market_movers = 1",
        ).fetchall()
    sent = 0
    for r in rows:
        try:
            await _send_raw(
                int(r["telegram_chat_id"]),
                summary,
                parse_mode="MarkdownV2",
            )
            sent += 1
        except Exception as exc:
            log.warning("market-mover send failed: %s", exc)
    return {"sent": sent, "event_id": event_id, "market_slug": market_slug}

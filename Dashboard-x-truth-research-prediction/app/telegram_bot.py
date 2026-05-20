"""Telegram query bot — answers `/edge`, `/source`, `/stats` commands.

Each user runs their own bot (token stored in profile, Fernet-encrypted at
rest), so this module polls every opted-in user's bot in turn and replies to
any pending commands. Stateless: we just persist the last processed update_id
in memory per (token, user_id) and ignore older messages.

Why polling not webhooks: webhooks require a public HTTPS endpoint per bot.
Polling is one-process, no infrastructure, fine for the volumes we expect.
The trade-off is ~5s latency between command and reply.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx
from sqlmodel import select

from app.db import AsyncSession, engine
from app.models import Source, User
from app.security import decrypt_field

logger = logging.getLogger(__name__)


# In-memory cursor — per (decrypted token) we remember the last update_id so
# we don't re-process the same command on every poll. Process restart resets
# this and we may re-reply once, which is harmless.
_LAST_UPDATE: dict[str, int] = {}


def _format_source_reply(s: Source) -> str:
    cred = f"{s.global_credibility:.2f}"
    acc = f"{s.accuracy_global:.0%}" if s.accuracy_global is not None else "n/a"
    brier = f"{s.brier_score:.3f} (n={s.brier_n})" if s.brier_score is not None else "n/a"
    return (
        f"*@{s.handle}* ({s.platform})\n"
        f"Credibility: `{cred}`\n"
        f"Accuracy: `{acc}` ({s.correct_qualifying}/{s.qualifying_predictions})\n"
        f"Brier: `{brier}`\n"
        f"Trusted: `{s.trusted}`  ·  Rated: `{s.accuracy_unlocked}`"
    )


async def _global_stats() -> str:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        from app.models import PaperTrade, Prediction
        n_preds = (await session.exec(select(Prediction))).all()
        n_resolved = [p for p in n_preds if p.resolved]
        n_correct = [p for p in n_resolved if p.resolved_correct]
        trades = (await session.exec(select(PaperTrade))).all()
        closed = [t for t in trades if t.resolved]
        pnl = sum((t.pnl_usd or 0.0) for t in closed)
    hit = (len(n_correct) / len(n_resolved)) if n_resolved else None
    hit_str = f"{hit:.0%}" if hit is not None else "n/a"
    return (
        "*narve.ai stats*\n"
        f"Predictions: `{len(n_preds)}`  ·  resolved `{len(n_resolved)}`\n"
        f"Hit rate: `{hit_str}`\n"
        f"Paper trades: `{len(trades)}`  ·  closed `{len(closed)}`\n"
        f"Paper P&L: `{pnl:+.2f}`"
    )


async def _handle_command(text: str) -> Optional[str]:
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    parts = text.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/start", "/help"):
        return (
            "*narve.ai signals bot*\n"
            "`/edge <handle>` — top recent EV signals for a source\n"
            "`/source <handle>` — credibility profile\n"
            "`/stats` — global pipeline stats\n"
            "`/help` — this message"
        )

    if cmd == "/stats":
        return await _global_stats()

    if cmd == "/source" and arg:
        handle = arg.lstrip("@")
        async with AsyncSession(engine, expire_on_commit=False) as session:
            s = (await session.exec(select(Source).where(Source.handle == handle))).first()
        return _format_source_reply(s) if s else f"No source `@{arg}` tracked yet."

    if cmd == "/edge" and arg:
        handle = arg.lstrip("@")
        async with AsyncSession(engine, expire_on_commit=False) as session:
            from app.models import Prediction, RawPost
            stmt = (
                select(Prediction, RawPost)
                .join(RawPost, Prediction.raw_post_id == RawPost.id)
                .where(RawPost.author_handle == handle, Prediction.ev_score.isnot(None))
                .order_by(Prediction.ev_score.desc())
                .limit(5)
            )
            rows = (await session.exec(stmt)).all()
        if not rows:
            return f"No tracked signals for `@{handle}` yet."
        lines = [f"*Top signals from @{handle}:*"]
        for pred, _post in rows:
            mip = pred.market_implied_probability
            price_str = f"{mip:.2f}" if mip is not None else "—"
            line = (
                f"• {pred.bet_side or 'YES'} @ {price_str} "
                f"(EV {pred.ev_score:+.2f}) — `{(pred.market_question or pred.market_slug or 'unmatched')[:80]}`"
            )
            lines.append(line)
        return "\n".join(lines)

    return None


async def _poll_one_user(client: httpx.AsyncClient, user: User) -> int:
    """Poll one user's bot, reply to any pending commands. Returns # replies sent."""
    token = decrypt_field(user.telegram_bot_token or "")
    if not token:
        return 0
    offset = _LAST_UPDATE.get(token, 0)
    try:
        resp = await client.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": offset + 1 if offset else 0, "timeout": 0, "limit": 20},
            timeout=10,
        )
        if resp.status_code != 200:
            return 0
        data = resp.json()
    except Exception as exc:
        logger.debug("Telegram getUpdates failed for user %d: %s", user.id, exc)
        return 0

    updates = data.get("result", []) or []
    replied = 0
    for update in updates:
        upd_id = update.get("update_id", 0)
        if upd_id > _LAST_UPDATE.get(token, 0):
            _LAST_UPDATE[token] = upd_id
        msg = update.get("message") or update.get("channel_post") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        text = msg.get("text") or ""
        if not chat_id or not text:
            continue
        # Authorisation: only reply to messages from the chat_id stored on the user.
        # This stops anyone who finds the bot from running our commands against it.
        if str(chat_id) != (user.telegram_chat_id or "").strip():
            continue
        reply = await _handle_command(text)
        if reply is None:
            continue
        try:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": reply, "parse_mode": "Markdown"},
                timeout=10,
            )
            replied += 1
        except Exception as exc:
            logger.debug("Telegram sendMessage failed: %s", exc)
    return replied


async def poll_telegram_commands() -> dict:
    """Iterate every user with a Telegram bot and answer pending commands."""
    stats = {"users_polled": 0, "replies_sent": 0}
    async with AsyncSession(engine, expire_on_commit=False) as session:
        users = (await session.exec(
            select(User).where(User.telegram_bot_token != "", User.telegram_alerts_enabled == True)  # noqa: E712
        )).all()
    if not users:
        return stats
    async with httpx.AsyncClient() as client:
        for u in users:
            stats["users_polled"] += 1
            stats["replies_sent"] += await _poll_one_user(client, u)
    return stats

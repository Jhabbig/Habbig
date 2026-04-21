"""Telegram bot — standalone long-running process.

Run on the server as its own process:

    nohup python3 bots/telegram_bot.py > /tmp/telegram_bot.log 2>&1 &

Requires ``python-telegram-bot==21.0`` and TELEGRAM_BOT_TOKEN in env.

Commands:
  /start       — link with one-shot token to narve.ai account
  /best        — top EV bets right now (respects user threshold)
  /market X    — bundle for a specific market slug or URL
  /source H    — credibility card for a source handle
  /signal      — most-recent insider signals
  /portfolio   — aggregate position summary
  /settings    — thresholds + mute toggles

This module intentionally keeps bot-specific glue (message dispatch,
markup) and calls into the shared code paths (``db``, ``bots.formatters``)
for content. That way the same formatter feeds both bots and the web
alert bell.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
import time
from typing import Any, Optional

# Put gateway on sys.path so ``import db`` works when this script is
# launched from the repo root (``python3 bots/telegram_bot.py``).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "gateway"))

from bots.formatters import (  # noqa: E402
    format_best_bet_telegram,
    load_best_bets,
)


log = logging.getLogger("telegram_bot")


TELEGRAM_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
APP_URL_ENV = "APP_URL"


def _app_url() -> str:
    return os.environ.get(APP_URL_ENV, "https://narve.ai").rstrip("/")


def _new_link_token() -> str:
    """48-char URL-safe token used once to link Telegram ↔ narve."""
    return secrets.token_urlsafe(36)[:48]


def _upsert_link_row(chat_id: int, username: Optional[str]) -> str:
    """Idempotently create / refresh the telegram_connections row.

    Returns the link_token the /start handler embeds in the deep link.
    If a row already exists for this chat_id we overwrite the
    link_token (so a user who missed the first link just runs /start
    again).
    """
    import db  # late import: db loads the whole gateway config
    now = int(time.time())
    token = _new_link_token()
    with db.conn() as c:
        existing = c.execute(
            "SELECT id FROM telegram_connections WHERE telegram_chat_id = ?",
            (chat_id,),
        ).fetchone()
        if existing:
            c.execute(
                "UPDATE telegram_connections SET "
                "  telegram_username = ?, link_token = ?, connected_at = ? "
                "WHERE id = ?",
                (username, token, now, existing["id"]),
            )
        else:
            # user_id is NULL until /connect/telegram on the web pairs.
            c.execute(
                "INSERT INTO telegram_connections "
                "(user_id, telegram_chat_id, telegram_username, link_token, "
                " connected_at, is_active) "
                "VALUES (NULL, ?, ?, ?, ?, 0)",
                (chat_id, username, token, now),
            )
    return token


def _find_user_by_chat(chat_id: int) -> Optional[dict]:
    import db
    with db.conn() as c:
        row = c.execute(
            "SELECT tc.*, u.id AS uid, u.email "
            "FROM telegram_connections tc "
            "LEFT JOIN users u ON u.id = tc.user_id "
            "WHERE tc.telegram_chat_id = ? AND tc.user_id IS NOT NULL",
            (chat_id,),
        ).fetchone()
    return dict(row) if row else None


# ── Handlers ────────────────────────────────────────────────────────────────


async def cmd_start(update, context):
    chat = update.effective_chat
    user = update.effective_user
    token = _upsert_link_row(chat.id, user.username if user else None)
    link = f"{_app_url()}/connect/telegram?token={token}"
    await update.message.reply_text(
        "Welcome to narve.ai 👋\n\n"
        "Tap the link to connect your narve account:\n"
        f"{link}\n\n"
        "Once connected, /best will show current high-EV markets, "
        "/market <slug> looks up a specific market, and /source <handle> "
        "shows a source's credibility card.",
    )


async def cmd_best(update, context):
    chat_id = update.effective_chat.id
    user_row = _find_user_by_chat(chat_id)
    if not user_row:
        await update.message.reply_text(
            "You're not linked yet — run /start first.",
        )
        return
    bets = load_best_bets(limit=5,
                         min_ev=float(user_row.get("min_ev_threshold") or 0.05),
                         min_cred=float(user_row.get("min_credibility") or 0.7))
    if not bets:
        await update.message.reply_text("No fresh high-EV bets right now.")
        return
    for bet in bets:
        await update.message.reply_text(
            format_best_bet_telegram(bet),
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
        )


async def cmd_market(update, context):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /market <slug-or-url>")
        return
    slug = args[0]
    # Accept either bare slug or a full polymarket URL.
    if "polymarket.com/event/" in slug:
        try:
            slug = slug.split("/event/", 1)[1].split("?", 1)[0].strip("/")
        except Exception:
            pass
    # Reuse the extension bundle path for the content.
    try:
        from extension_routes import _compose_bundle  # type: ignore[import]
        bundle = await _compose_bundle(slug)
    except Exception as exc:
        log.warning("market lookup failed: %s", exc)
        bundle = None
    if not bundle:
        await update.message.reply_text("No narve coverage for that market.")
        return
    await update.message.reply_text(
        format_best_bet_telegram({
            "market_slug": slug,
            "question": bundle.get("market_question") or slug,
            "betyc_probability": bundle.get("betyc_yes_probability"),
            "market_price": bundle.get("market_yes_price"),
            "edge_pct": (bundle.get("betyc_edge") or 0) * 100
                        if bundle.get("betyc_edge") is not None else None,
            "confidence": bundle.get("betyc_confidence"),
            "source_count": bundle.get("source_count") or 0,
            "top_sources": bundle.get("top_sources") or [],
            "side": "yes" if (bundle.get("betyc_edge") or 0) > 0 else "no",
            "category": "market",
        }),
        parse_mode="MarkdownV2",
        disable_web_page_preview=True,
    )


async def cmd_source(update, context):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /source <handle>")
        return
    handle = args[0].lstrip("@")
    try:
        import db
        cred = db.get_source_credibility(handle) if hasattr(
            db, "get_source_credibility"
        ) else None
    except Exception:
        cred = None
    if not cred:
        await update.message.reply_text(f"No rated source @{handle}.")
        return
    score = round(float(cred["global_credibility"] or 0), 2)
    accuracy = (cred["correct_predictions"] or 0) / max(
        int(cred["total_predictions"] or 1), 1,
    )
    await update.message.reply_text(
        f"@{handle} — credibility {score:.2f}\n"
        f"{int(accuracy * 100)}% accuracy across "
        f"{int(cred['total_predictions'] or 0)} tracked predictions.\n"
        f"{_app_url()}/sources/{handle}",
        disable_web_page_preview=True,
    )


async def cmd_signal(update, context):
    # Stub: until insider-signal aggregation is wired for bots, just
    # point the user at the web dashboard. The sends job will push
    # these unsolicited when they fire.
    await update.message.reply_text(
        f"Insider signals stream to the dashboard: {_app_url()}/dashboard/insider",
    )


async def cmd_portfolio(update, context):
    chat_id = update.effective_chat.id
    user_row = _find_user_by_chat(chat_id)
    if not user_row:
        await update.message.reply_text("Run /start to link first.")
        return
    from portfolio import positions
    summary = positions.summary(int(user_row["user_id"]))
    if not summary["active_positions"]:
        await update.message.reply_text(
            "No active positions tracked yet. "
            f"Connect Polymarket/Kalshi at {_app_url()}/settings",
            disable_web_page_preview=True,
        )
        return
    lines = [
        f"Active positions: {summary['active_positions']}",
        f"Total value: ${summary['total_value_usd']:,.2f}",
        f"Unrealised P/L: ${summary['unrealised_pnl_usd']:+,.2f}",
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_settings(update, context):
    chat_id = update.effective_chat.id
    user_row = _find_user_by_chat(chat_id)
    if not user_row:
        await update.message.reply_text("Run /start to link first.")
        return
    ev = float(user_row.get("min_ev_threshold") or 0.05)
    cred = float(user_row.get("min_credibility") or 0.7)
    await update.message.reply_text(
        f"Current thresholds:\n"
        f"• Min EV: {ev * 100:.1f}%\n"
        f"• Min credibility: {cred:.2f}\n\n"
        f"Change them at {_app_url()}/settings",
        disable_web_page_preview=True,
    )


# ── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    token = os.environ.get(TELEGRAM_TOKEN_ENV, "").strip()
    if not token:
        log.error("%s not set — bot will not start", TELEGRAM_TOKEN_ENV)
        sys.exit(1)

    # Lazy import so ``python3 bots/telegram_bot.py --check`` can run
    # without the dep (useful for CI smoke tests).
    from telegram.ext import Application, CommandHandler  # type: ignore[import]

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("best", cmd_best))
    app.add_handler(CommandHandler("market", cmd_market))
    app.add_handler(CommandHandler("source", cmd_source))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("settings", cmd_settings))
    log.info("Telegram bot starting")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()

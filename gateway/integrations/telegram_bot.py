"""Telegram bot for narve.ai prediction market intelligence (F15).

Commands:
  /start         — Welcome message with link to narve.ai
  /subscribe     — Link Telegram account to narve.ai (requires link code)
  /edge [N]      — Top N edge markets (default 5)
  /source @handle — Source credibility profile
  /alerts on|off — Toggle market mover alerts via Telegram

Setup:
  1. Create a bot via @BotFather on Telegram
  2. Set TELEGRAM_BOT_TOKEN in .env
  3. The bot starts automatically alongside the gateway if the token is set

The bot runs in polling mode (no webhook needed) in a background asyncio task.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

# Telegram handle format: 1-30 alphanumeric / underscore characters.
_HANDLE_RE = re.compile(r"^[a-zA-Z0-9_]{1,30}$")

log = logging.getLogger("integrations.telegram")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def is_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN)


async def start_bot() -> None:
    """Start the Telegram bot in polling mode. Non-blocking.

    Only called if TELEGRAM_BOT_TOKEN is set. Uses python-telegram-bot
    library. Falls back gracefully if the library is not installed.
    """
    if not TELEGRAM_BOT_TOKEN:
        log.info("Telegram bot: TELEGRAM_BOT_TOKEN not set, skipping")
        return

    try:
        from telegram import Update, Bot
        from telegram.ext import Application, CommandHandler, ContextTypes
    except ImportError:
        log.warning("python-telegram-bot not installed, skipping Telegram bot")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # ── Command handlers ────────────────────────────────────────────────

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Welcome message."""
        await update.message.reply_text(
            "Welcome to narve.ai — prediction market intelligence.\n\n"
            "Commands:\n"
            "/subscribe <code> — Link your narve.ai account\n"
            "/edge — Top edge markets\n"
            "/source @handle — Source credibility\n"
            "/alerts on|off — Toggle alerts\n\n"
            "Visit https://narve.ai to get started."
        )

    async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Link Telegram account to narve.ai."""
        import db
        args = context.args or []
        if not args:
            await update.message.reply_text(
                "Usage: /subscribe <link_code>\n"
                "Get your link code from narve.ai Settings page."
            )
            return

        link_code = args[0].strip()
        chat_id = str(update.effective_chat.id)
        username = update.effective_user.username or ""

        # SECURITY (H15): the previous implementation accepted ANY claimed
        # invite_tokens.token as a Telegram link code. That let an attacker
        # who learned another user's invite token link the victim's
        # narve.ai account to the attacker's Telegram chat (account
        # takeover for alerts + read access to the victim's alert feed).
        #
        # The correct flow is a short-lived, user-scoped one-shot code:
        # the user initiates the link from the authenticated web UI, a
        # 6-digit code is written to `pending_telegram_links(user_id,
        # code, expires_at)`, the user types `/subscribe <code>` on
        # Telegram and the code is consumed exactly once.
        #
        # TODO: table pending_telegram_links(user_id INTEGER NOT NULL,
        #       code TEXT NOT NULL UNIQUE, expires_at INTEGER NOT NULL,
        #       created_at INTEGER NOT NULL) must be created, and a
        #       corresponding web-UI endpoint must mint codes. Until the
        #       table exists this handler refuses all link attempts
        #       rather than falling back to the unsafe invite-token
        #       lookup.
        user_id = None
        now_ts = int(time.time())
        try:
            with db.conn() as c:
                pending = c.execute(
                    "SELECT user_id, expires_at FROM pending_telegram_links "
                    "WHERE code = ?",
                    (link_code,),
                ).fetchone()
                if pending and int(pending["expires_at"]) > now_ts:
                    user_id = pending["user_id"]
                    # One-shot: consume the code immediately so it cannot
                    # be replayed, even on DB errors below.
                    c.execute(
                        "DELETE FROM pending_telegram_links WHERE code = ?",
                        (link_code,),
                    )
        except Exception as e:
            # Most likely: table does not exist yet. Fail closed.
            log.warning("Telegram subscribe: pending_telegram_links lookup failed: %s", e)
            await update.message.reply_text(
                "Telegram linking is temporarily unavailable. Please try again later."
            )
            return

        if user_id is None:
            await update.message.reply_text(
                "Invalid or expired link code. Generate a fresh code from your narve.ai Settings page."
            )
            return

        now = int(time.time())
        try:
            with db.conn() as c:
                c.execute(
                    "INSERT INTO telegram_user_links (user_id, telegram_chat_id, telegram_username, linked_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(telegram_chat_id) DO UPDATE SET user_id = excluded.user_id, "
                    "telegram_username = excluded.telegram_username, linked_at = excluded.linked_at",
                    (user_id, chat_id, username, now),
                )
            await update.message.reply_text("Linked! You'll receive market alerts here.")
        except Exception as e:
            log.warning("Telegram subscribe failed: %s", e)
            await update.message.reply_text("Failed to link. Try again or contact support.")

    async def cmd_edge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show top edge markets."""
        import db
        from backend.markets import unified_markets
        from backend.markets.polymarket_client import PolymarketClient
        from backend.markets.kalshi_client import KalshiClient

        limit = 5
        if context.args:
            try:
                limit = max(1, min(int(context.args[0]), 10))
            except ValueError:
                pass

        try:
            poly = PolymarketClient()
            kalshi = KalshiClient(
                base_url=os.environ.get("KALSHI_API_BASE", "https://trading-api.kalshi.com/trade-api/v2"),
            )
            markets = await unified_markets.fetch_unified_markets(poly, kalshi, cache_ttl=300)
            active = [m for m in markets if m.status == "active"]
            enriched = unified_markets.enrich_markets_with_intelligence(active)
            await poly.close()
            await kalshi.close()

            with_edge = [m for m in enriched if m.betyc_ev_score is not None and m.betyc_prediction_count >= 1]
            with_edge.sort(key=lambda m: abs(m.betyc_ev_score or 0), reverse=True)
            top = with_edge[:limit]

            if not top:
                await update.message.reply_text("No edge markets found. Check back later.")
                return

            lines = ["Top edge markets:\n"]
            for i, m in enumerate(top, 1):
                edge_pct = int((m.betyc_ev_score or 0) * 100)
                sign = "+" if edge_pct > 0 else ""
                lines.append(
                    f"{i}. {m.title[:60]}\n"
                    f"   Market: {int(m.yes_price * 100)}% | narve.ai: {int((m.yes_price + (m.betyc_ev_score or 0)) * 100)}% | Edge: {sign}{edge_pct}pp"
                )
            await update.message.reply_text("\n".join(lines))

        except Exception as e:
            log.warning("Telegram /edge failed: %s", e)
            await update.message.reply_text("Error fetching markets. Try again.")

    async def cmd_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show source credibility."""
        import db
        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: /source @handle")
            return

        handle = args[0].lstrip("@")
        # SECURITY (L10): reject malformed handles instead of passing
        # arbitrary input down to the DB layer (possible cache-key or
        # log-injection vector, and friendly to the user either way).
        if not _HANDLE_RE.match(handle):
            await update.message.reply_text(
                "Invalid handle. Use letters, numbers, or underscore (1–30 chars). Example: /source @nate_silver"
            )
            return
        cred = db.get_source_credibility(handle)
        if not cred:
            await update.message.reply_text(f"Source @{handle} not found.")
            return

        await update.message.reply_text(
            f"@{handle}\n"
            f"Credibility: {cred['global_credibility']:.2f}\n"
            f"Accuracy unlocked: {'Yes' if cred['accuracy_unlocked'] else 'No'}\n"
            f"Total predictions: {cred['total_predictions']}\n"
            f"Correct: {cred['correct_predictions']}\n"
            f"Categories: {cred['categories_active']}"
        )

    async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Toggle alerts."""
        import db
        args = context.args or []
        chat_id = str(update.effective_chat.id)

        with db.conn() as c:
            link = c.execute(
                "SELECT * FROM telegram_user_links WHERE telegram_chat_id = ?",
                (chat_id,),
            ).fetchone()

        if not link:
            await update.message.reply_text("Link your account first: /subscribe <code>")
            return

        if args and args[0].lower() in ("off", "0", "false", "disable"):
            with db.conn() as c:
                c.execute(
                    "UPDATE telegram_user_links SET alerts_enabled = 0 WHERE telegram_chat_id = ?",
                    (chat_id,),
                )
            await update.message.reply_text("Alerts disabled.")
        else:
            with db.conn() as c:
                c.execute(
                    "UPDATE telegram_user_links SET alerts_enabled = 1 WHERE telegram_chat_id = ?",
                    (chat_id,),
                )
            await update.message.reply_text("Alerts enabled!")

    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("edge", cmd_edge))
    app.add_handler(CommandHandler("source", cmd_source))
    app.add_handler(CommandHandler("alerts", cmd_alerts))

    # Start polling in background
    log.info("Telegram bot starting in polling mode")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    log.info("Telegram bot is running")


async def send_telegram_alert(chat_id: str, message: str) -> bool:
    """Send a message to a Telegram chat. Used by notification jobs."""
    if not TELEGRAM_BOT_TOKEN:
        return False

    try:
        from telegram import Bot
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML")
        return True
    except ImportError:
        return False
    except Exception as e:
        log.warning("Telegram send failed to %s: %s", chat_id, e)
        return False

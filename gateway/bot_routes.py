"""Web routes the bots (Telegram, Discord) call back into.

Only one route today: ``GET /connect/telegram?token=<link_token>``.
The Telegram bot sends a deep-link with a link_token after ``/start``;
the user clicks it, lands on narve.ai while logged in, and we write a
telegram_connections row linking the chat_id to the user_id.

Bonus: the landing JSON for the Discord bot's OAuth flow is served
here too, though the Discord bot handles most of its own state.

Registered via ``from bot_routes import register; register(app)`` from
server.py (no business logic in server.py).
"""

from __future__ import annotations

import html as _html
import logging
import time

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse


log = logging.getLogger("bot_routes")


def _current_user(request: Request) -> dict | None:
    return getattr(request.state, "user", None)


def register(app) -> None:

    @app.get("/connect/telegram", response_class=HTMLResponse)
    async def connect_telegram(request: Request, token: str = ""):
        """Finalise Telegram → narve link using the one-shot link_token.

        Flow:
          1. Telegram /start → bot creates row with link_token, shares URL.
          2. User clicks URL → this handler.
          3. We require an active narve session (user clicks while logged in).
          4. On success, we stamp user_id onto the row and invalidate the
             link_token so it can't be re-used.
        """
        token = (token or "").strip()
        if not token or len(token) > 200:
            raise HTTPException(status_code=400, detail="Missing or invalid token")

        user = _current_user(request)
        if user is None:
            # Preserve the token so the login redirect can bounce back.
            return HTMLResponse(
                f"<script>location.href='/login?next={_html.escape('/connect/telegram?token=' + token)}'</script>",
                status_code=401,
            )
        user_id = int(user.get("id") or user.get("user_id") or 0)

        import db
        with db.conn() as c:
            row = c.execute(
                "SELECT id, user_id, telegram_chat_id FROM telegram_connections "
                "WHERE link_token = ?",
                (token,),
            ).fetchone()
            if not row:
                return HTMLResponse(
                    "<h1>Link expired</h1>"
                    "<p>Please /start the bot again to get a fresh link.</p>",
                    status_code=400,
                )
            if row["user_id"] and int(row["user_id"]) != user_id:
                # Token already consumed by another user — shouldn't
                # happen with the one-shot flow but guard anyway.
                return HTMLResponse(
                    "<h1>Already used</h1>"
                    "<p>This link was claimed by a different account.</p>",
                    status_code=409,
                )

            now = int(time.time())
            c.execute(
                "UPDATE telegram_connections SET "
                "  user_id = ?, link_token = NULL, connected_at = ?, "
                "  is_active = 1 "
                "WHERE id = ?",
                (user_id, now, int(row["id"])),
            )
            chat_id = row["telegram_chat_id"]

        log.info("Telegram connected: user=%s chat=%s", user_id, chat_id)
        return HTMLResponse(
            "<!DOCTYPE html><html><body style='font-family:Inter,sans-serif;"
            "background:#0d0d0d;color:#fff;display:flex;align-items:center;"
            "justify-content:center;height:100vh;margin:0'>"
            "<div style='text-align:center'>"
            "<h1 style='font-family:Instrument Serif,serif;font-style:italic'>narve.ai</h1>"
            "<p>Telegram connected. You can close this tab.</p>"
            "</div></body></html>",
        )

    @app.get("/connect/discord/status")
    async def connect_discord_status(request: Request):
        """Minimal status endpoint the Discord bot polls to verify a
        user has linked their account. Returns the number of linked
        Discord users, never reveals IDs."""
        user = _current_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Sign in first")
        import db
        uid = int(user.get("id") or user.get("user_id") or 0)
        with db.conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM discord_user_connections "
                "WHERE user_id = ?",
                (uid,),
            ).fetchone()
        return JSONResponse({"connected": int(row["n"] or 0)})

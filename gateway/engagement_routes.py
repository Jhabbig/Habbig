"""In-app re-engagement prompts.

Two endpoints:

  * ``GET  /api/engagement/prompt``  — returns null for healthy users,
    a JSON envelope {type, message, cta_url} for at_risk or critical.
    Consulted by narve-app.js on dashboard load; the banner component
    renders whatever message comes back.
  * ``POST /api/engagement/prompt/dismiss`` — records a per-user, per-tier
    dismissal so the banner hides for 7 days.

Both read ``churn_signals`` populated nightly by the compute_churn_signals
job. If the row is missing or stale (> 3 days since computed_at), we fall
back to "no prompt" rather than surface dubious guidance.

Registered on import via the same pattern as billing_routes.py — loaded
lazily from server.py's late-import block.
"""

from __future__ import annotations

import html
import logging
import time
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

import db
import server
from server import app, current_user


log = logging.getLogger("engagement_routes")


# Dismissals remain sticky for this many days before the banner can
# come back. Matches the product spec's "cooldown 7 days".
DISMISSAL_COOLDOWN_DAYS = 7

# A churn_signals row older than this is treated as stale — better to
# show nothing than a potentially wrong prompt.
STALE_SIGNAL_DAYS = 3


def _prompt_for_tier(tier: str) -> Optional[dict]:
    """Return the {type, message, cta_url} payload for the given tier.
    Returns None for 'healthy' or any unknown tier.
    """
    if tier == "at_risk":
        return {
            "type": "suggestion",
            "tier": "at_risk",
            "message": "3 new high-EV signals in your categories",
            "cta_url": "/dashboard/best-bets",
            "cta_label": "View signals",
        }
    if tier == "critical":
        return {
            "type": "win_back",
            "tier": "critical",
            "message": "Your credibility-scored signals are missing you",
            "cta_url": "/dashboard",
            "cta_label": "Jump back in",
        }
    return None


def _is_dismissed(user_id: int, tier: str, now_ts: int) -> bool:
    cutoff_ts = now_ts - DISMISSAL_COOLDOWN_DAYS * 86400
    with db.conn() as c:
        row = c.execute(
            "SELECT dismissed_at FROM engagement_prompt_dismissals "
            "WHERE user_id = ? AND prompt_tier = ?",
            (user_id, tier),
        ).fetchone()
    if not row:
        return False
    # dismissed_at is a DATETIME string; compare epoch via strftime.
    with db.conn() as c:
        epoch_row = c.execute(
            "SELECT CAST(strftime('%s', ?) AS INTEGER) AS e",
            (row["dismissed_at"],),
        ).fetchone()
    epoch = int(epoch_row["e"] if epoch_row and epoch_row["e"] else 0)
    return epoch > cutoff_ts


def _current_signal(user_id: int) -> Optional[dict]:
    with db.conn() as c:
        row = c.execute(
            "SELECT risk_tier, risk_score, computed_at FROM churn_signals "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "risk_tier": row["risk_tier"],
        "risk_score": row["risk_score"],
        "computed_at": row["computed_at"],
    }


@app.get("/api/engagement/prompt", include_in_schema=False)
async def api_engagement_prompt(request: Request):
    """Return the in-app prompt (or null) for the logged-in user.

    Unauthenticated callers get {prompt: null} (not a 401) because the
    dashboard calls this from first-paint JS; a 401 would flash an error
    before the login redirect kicks in.
    """
    user = current_user(request)
    if not user:
        return JSONResponse({"prompt": None})

    signal = _current_signal(user["user_id"])
    if not signal or not signal["risk_tier"]:
        return JSONResponse({"prompt": None})

    # Stale-row guard — if the cron hasn't run in a while, prefer silence.
    if signal["computed_at"]:
        try:
            with db.conn() as c:
                age_row = c.execute(
                    "SELECT CAST((strftime('%s','now') - strftime('%s', ?)) AS INTEGER) AS age",
                    (signal["computed_at"],),
                ).fetchone()
            age_sec = int(age_row["age"] if age_row and age_row["age"] else 0)
        except Exception:
            age_sec = 0
        if age_sec > STALE_SIGNAL_DAYS * 86400:
            return JSONResponse({"prompt": None})

    tier = signal["risk_tier"]
    prompt = _prompt_for_tier(tier)
    if not prompt:
        return JSONResponse({"prompt": None})

    # Respect dismissal cooldown.
    if _is_dismissed(user["user_id"], tier, int(time.time())):
        return JSONResponse({"prompt": None})

    return JSONResponse({"prompt": prompt})


@app.post("/api/engagement/prompt/dismiss", include_in_schema=False)
async def api_engagement_prompt_dismiss(request: Request):
    """Record a dismissal. Banner hides for DISMISSAL_COOLDOWN_DAYS days."""
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "unauthenticated"}, status_code=401)

    tier = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            tier = body.get("tier")
    except Exception:
        tier = None
    if tier not in ("at_risk", "critical"):
        return JSONResponse({"ok": False, "error": "invalid_tier"}, status_code=400)

    with db.conn() as c:
        # Upsert: overwriting dismissed_at restarts the 7-day cooldown.
        c.execute(
            """
            INSERT INTO engagement_prompt_dismissals (user_id, prompt_tier, dismissed_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, prompt_tier) DO UPDATE SET
              dismissed_at = CURRENT_TIMESTAMP
            """,
            (user["user_id"], tier),
        )
    return JSONResponse({"ok": True, "tier": tier})

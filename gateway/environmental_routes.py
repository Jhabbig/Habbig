"""Environmental-impact routes (Pro).

  GET  /api/markets/{slug}/environmental           cached analysis
  POST /api/markets/{slug}/environmental/refresh   force a Claude regen
  GET  /api/markets/environmental/top              top-impact markets
  PATCH /api/user/preferences/environmental        show flag + unit

Force-refresh is rate-limited to 5/day per user.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Form, HTTPException, Request
from fastapi.responses import JSONResponse

from ai import environmental as env


log = logging.getLogger("environmental_routes")


REFRESH_LIMIT_PER_DAY = 5
VALID_UNITS = {"co2_mt", "trees", "cars", "homes", "flights"}


def _db_path() -> Path:
    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path(__file__).parent / p)
    return Path(__file__).parent / "auth.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _require_pro_user(request: Request) -> dict:
    import server
    user = server.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Auth required")
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE user_id = ?", (user["user_id"],),
            ).fetchall()
            subs = {r["dashboard_key"]: dict(r) for r in rows}
        finally:
            conn.close()
        plan = server._user_plan_info(user, subs, int(time.time())).get("plan") or "none"
    except Exception:
        plan = "none"
    if plan not in ("pro", "enterprise") and not user.get("is_admin"):
        raise HTTPException(status_code=402, detail="Pro subscription required")
    return user


def _user_unit(user_id: int) -> str:
    conn = _connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        col = "preferred_unit" if "preferred_unit" in cols else (
            "env_unit" if "env_unit" in cols else None
        )
        if not col:
            return "co2_mt"
        row = conn.execute(f"SELECT {col} FROM users WHERE id = ?", (user_id,)).fetchone()
        val = row[col] if row else None
    finally:
        conn.close()
    return val if val in VALID_UNITS else "co2_mt"


async def get_environmental(request: Request, market_slug: str):
    user = _require_pro_user(request)
    market_question = request.query_params.get("question") or market_slug.replace("-", " ")
    category = request.query_params.get("category") or ""
    try:
        yes_price = float(request.query_params.get("yes_price")) if request.query_params.get("yes_price") else None
    except ValueError:
        yes_price = None
    payload = await env.generate_environmental_impact(
        market_slug, market_question,
        category=category, yes_price=yes_price,
    )
    return JSONResponse(env.apply_user_unit_preference(payload, _user_unit(user["user_id"])))


async def refresh_environmental(request: Request, market_slug: str):
    user = _require_pro_user(request)
    # Rate-limit force refresh 5/day.
    import datetime as _dt
    today = _dt.datetime.utcnow().strftime("%Y-%m-%d")
    key = f"env_refresh:{user['user_id']}:{today}"
    if hasattr(request.app.state, key) and getattr(request.app.state, key) >= REFRESH_LIMIT_PER_DAY:
        raise HTTPException(status_code=429, detail="Daily refresh limit reached")
    setattr(request.app.state, key,
            int(getattr(request.app.state, key, 0)) + 1)

    market_question = request.query_params.get("question") or market_slug.replace("-", " ")
    category = request.query_params.get("category") or ""
    payload = await env.generate_environmental_impact(
        market_slug, market_question, category=category, force=True,
    )
    return JSONResponse(env.apply_user_unit_preference(payload, _user_unit(user["user_id"])))


async def top_environmental(request: Request):
    _require_pro_user(request)
    try:
        limit = max(1, min(50, int(request.query_params.get("limit") or "20")))
    except ValueError:
        limit = 20

    # Scan ai_cache for env entries — cheap because we only keep 24h of rows.
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT value_json FROM ai_cache WHERE feature = 'environmental' "
            "AND expires_at > ? ORDER BY created_at DESC LIMIT ?",
            (int(time.time()), limit * 5),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()

    import json as _json
    relevant: list[dict] = []
    for r in rows:
        try:
            payload = _json.loads(r["value_json"])
        except (TypeError, _json.JSONDecodeError):
            continue
        if not payload.get("is_relevant"):
            continue
        relevant.append(payload)
    relevant.sort(
        key=lambda p: abs((p.get("yes_co2_impact_mt") or 0)) +
                      abs((p.get("no_co2_impact_mt") or 0)),
        reverse=True,
    )
    return JSONResponse({"markets": relevant[:limit]})


async def update_preferences(
    request: Request,
    show_environmental_impact: Optional[int] = Form(None),
    preferred_unit: Optional[str] = Form(None),
):
    import server
    user = server.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Auth required")
    if preferred_unit and preferred_unit not in VALID_UNITS:
        raise HTTPException(status_code=400, detail="Invalid unit")

    conn = _connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        show_col = "show_environmental_impact" if "show_environmental_impact" in cols else (
            "env_show" if "env_show" in cols else None
        )
        unit_col = "preferred_unit" if "preferred_unit" in cols else (
            "env_unit" if "env_unit" in cols else None
        )
        fields: list[str] = []
        args: list[Any] = []
        if show_environmental_impact is not None and show_col:
            fields.append(f"{show_col} = ?"); args.append(int(bool(show_environmental_impact)))
        if preferred_unit and unit_col:
            fields.append(f"{unit_col} = ?"); args.append(preferred_unit)
        if not fields:
            return JSONResponse({"updated": False}, status_code=400)
        args.append(user["user_id"])
        conn.execute(
            f"UPDATE users SET {', '.join(fields)} WHERE id = ?",
            tuple(args),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"updated": True})


def register(app) -> None:
    app.add_api_route("/api/markets/{market_slug}/environmental", get_environmental,
                      methods=["GET"])
    app.add_api_route("/api/markets/{market_slug}/environmental/refresh",
                      refresh_environmental, methods=["POST"])
    app.add_api_route("/api/markets/environmental/top", top_environmental,
                      methods=["GET"])
    app.add_api_route("/api/user/preferences/environmental", update_preferences,
                      methods=["PATCH"])

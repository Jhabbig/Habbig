"""HTTP routes for portfolio connect + read + Kelly calc.

All routes here assume request.state.user is already populated by the
existing session middleware. The access gate is "Trading add-on" — the
same check applied to the existing /api/markets/connections endpoints.

Register via ``from portfolio.routes import register; register(app)``
from server.py (no business logic in server.py).
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from portfolio import kalshi, kelly, polymarket, positions


log = logging.getLogger("portfolio.routes")


def _require_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    # sqlite3.Row doesn't expose .get, so uniform copy into dict.
    try:
        return dict(user)
    except Exception:
        return user  # type: ignore[return-value]


def _user_id(user: dict) -> int:
    return int(user.get("id") or user.get("user_id") or 0)


def register(app) -> None:

    # ── Polymarket connect ──────────────────────────────────────────────
    @app.post("/api/portfolio/polymarket/connect")
    async def connect_polymarket(request: Request):
        user = _require_user(request)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        wallet = (body.get("wallet_address") or "").strip()
        if not polymarket.is_valid_address(wallet):
            return JSONResponse(
                {"error": "wallet_address must be a 0x-prefixed 40-char hex string"},
                status_code=400,
            )
        polymarket.upsert_connection(_user_id(user), wallet)
        return JSONResponse({"connected": True, "wallet_address": wallet.lower()})

    # ── Kalshi connect ──────────────────────────────────────────────────
    @app.post("/api/portfolio/kalshi/connect")
    async def connect_kalshi(request: Request):
        user = _require_user(request)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        if not email or not password:
            return JSONResponse(
                {"error": "email and password required"}, status_code=400,
            )

        # Idempotency: a double-click / retry within 10 s must NOT trigger
        # two Kalshi login calls (rate-limited upstream) and must NOT
        # emit two encrypted-token writes. Key on the client-supplied
        # Idempotency-Key header, or fall back to a hash of the
        # (email) submitted — the same user retrying the same email is
        # what we're protecting against, so "same email twice in 10 s"
        # is a safe fingerprint. We do NOT include the password in the
        # fingerprint (logging hygiene).
        from security.idempotency import with_idempotency
        uid = _user_id(user)
        client_key = request.headers.get("Idempotency-Key")

        async def _do_connect() -> dict:
            try:
                result = await kalshi.login(email, password)
            except Exception as exc:
                log.info("kalshi login failed: %s", exc)
                return {"_status": 401, "error": "Kalshi login failed"}
            token = result.get("token") or result.get("access_token")
            if not token:
                return {"_status": 502, "error": "Unexpected response from Kalshi"}
            ok = kalshi.upsert_connection(
                user_id=uid,
                email=email,
                token=token,
                member_id=result.get("member_id"),
                token_expires_at=result.get("expires_at"),
            )
            if not ok:
                # CREDENTIALS_ENCRYPTION_KEY missing — refuse the plaintext.
                return {
                    "_status": 503,
                    "error": "Server not configured for Kalshi connections",
                }
            return {
                "_status": 200,
                "connected": True,
                "member_id": result.get("member_id"),
            }

        result = await with_idempotency(
            user_id=uid,
            op="kalshi_connect",
            client_key=client_key,
            ttl_seconds=10,
            body=_do_connect,
            fallback_fingerprint=email,
        )
        status = int(result.pop("_status", 200))
        return JSONResponse(result, status_code=status)

    # ── Positions + summary ─────────────────────────────────────────────
    @app.get("/api/portfolio/summary")
    async def api_portfolio_summary(request: Request):
        user = _require_user(request)
        return JSONResponse(positions.summary(_user_id(user)))

    @app.get("/api/portfolio/positions")
    async def api_portfolio_positions(
        request: Request, platform: Optional[str] = None,
    ):
        user = _require_user(request)
        if platform and platform not in ("polymarket", "kalshi"):
            raise HTTPException(status_code=400, detail="Unknown platform")
        return JSONResponse({
            "positions": positions.list_positions(_user_id(user), platform),
        })

    # ── Kelly calculator ────────────────────────────────────────────────
    @app.post("/api/kelly/calculate")
    async def api_kelly_calculate(request: Request):
        user = _require_user(request)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        try:
            our_prob = float(body.get("our_probability", 0))
            market_prob = float(body.get("market_price", 0))
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "our_probability and market_price must be numbers"},
                status_code=400,
            )

        # Bankroll can be overridden per-call (e.g. for whatif scenarios)
        # but defaults to the value stored on the user.
        bankroll_override = body.get("bankroll_usd")
        if bankroll_override is not None:
            try:
                bankroll = float(bankroll_override)
            except (TypeError, ValueError):
                return JSONResponse(
                    {"error": "bankroll_usd must be a number"},
                    status_code=400,
                )
        else:
            bankroll = kelly.get_user_bankroll(_user_id(user))

        table = kelly.sizing_table(our_prob, market_prob, bankroll)
        return JSONResponse(table)

    # ── Bankroll setter ─────────────────────────────────────────────────
    @app.post("/api/kelly/bankroll")
    async def api_set_bankroll(request: Request):
        user = _require_user(request)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        try:
            bankroll = float(body.get("bankroll_usd", 0))
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "bankroll_usd must be a number"}, status_code=400,
            )
        if bankroll < 0 or bankroll > 10_000_000:
            return JSONResponse(
                {"error": "bankroll_usd must be between 0 and 10000000"},
                status_code=400,
            )
        kelly.set_user_bankroll(_user_id(user), bankroll)
        return JSONResponse({"saved": True, "bankroll_usd": bankroll})

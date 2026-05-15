"""HTTP routes for portfolio connect + read + Kelly calc.

All routes here assume request.state.user is already populated by the
existing session middleware. The access gate is "Trading add-on" — the
same check applied to the existing /api/markets/connections endpoints.

Register via ``from portfolio.routes import register; register(app)``
from server.py (no business logic in server.py).

Gate policy
-----------
Every state-mutating route in this module MUST call
``_require_trading_addon(request)`` on its first line. The audit found
the entire file's set of POST routes were reachable by free-tier users,
which bypasses the monetisation contract for Polymarket/Kalshi
connections, position sync, Kelly calc, and bankroll persistence.

Read-only routes (``GET /api/portfolio/status``, summary, positions)
stay accessible to free users so the UI can render the upsell.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

import db
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


def _require_trading_addon(request: Request) -> dict:
    """Auth + Trading Add-on gate for state-mutating portfolio routes.

    Returns the user dict on success. Raises:
      * 401 if no authenticated session
      * 402 Payment Required if the user does not hold an active Trading
        Add-on (admins bypass via ``db.has_trading_addon``).

    402 is the right status here: the user is authenticated, but the
    request is gated behind a paid product. The client renders an
    upsell modal pointing at /pricing#trading-access.
    """
    user = _require_user(request)
    uid = _user_id(user)
    if uid <= 0:
        # Defensive: an authenticated session without an id is a bug,
        # not a payment problem — fall through as auth failure.
        raise HTTPException(status_code=401, detail="Authentication required")
    if not db.has_trading_addon(uid):
        raise HTTPException(
            status_code=402,
            detail="Trading Add-on required. See /pricing for access.",
        )
    return user


def register(app) -> None:

    # ── Trading add-on status (free, read-only) ─────────────────────────
    # Lets the dashboard render the upsell card without forcing the user
    # to provoke a 402 on a mutating endpoint first. Returns 200 for any
    # authenticated user — addon-less and addon-active alike — with a
    # boolean so the client can branch on `has_addon`.
    @app.get("/api/portfolio/status")
    async def api_portfolio_status(request: Request):
        user = _require_user(request)
        uid = _user_id(user)
        active = bool(db.has_trading_addon(uid)) if uid > 0 else False
        return JSONResponse({"has_addon": active})

    # ── Polymarket connect ──────────────────────────────────────────────
    # AUDIT 2026-05-15 (HIGH) — this parallel route used to accept an
    # unsigned ``{wallet_address}`` body and upsert directly, fully
    # bypassing the SIWE-required fix on ``/api/markets/connect/polymarket``
    # (commit e3248d5). The two surfaces wrote to different storage tables
    # (``polymarket_connections`` here vs ``user_market_credentials`` on
    # the markets route) so the parallel path was invisible to the
    # legacy-removal guard that closed the sibling hole.
    #
    # Fix: require the exact same SIWE proof (address, signature, message)
    # the markets-route demands. Validation is delegated to the helpers in
    # ``market_routes`` so there is one implementation of the rules; any
    # future change to the SIWE invariants applies to both paths.
    @app.post("/api/portfolio/polymarket/connect")
    async def connect_polymarket(request: Request):
        user = _require_trading_addon(request)
        uid = _user_id(user)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        # Lazy import — ``market_routes`` imports ``server`` indirectly,
        # which already imports ``portfolio.routes``. Pull SIWE helpers
        # at call time so the module-import graph stays one-way.
        import market_routes as _mr

        signature = (body.get("signature") or "").strip()
        message = body.get("message") or ""
        signed_address = (body.get("address") or "").strip()
        legacy_address = (body.get("wallet_address") or "").strip()

        # No SIWE fields? — point the caller at the canonical SIWE flow
        # and refuse to write. We never accept the unsigned shape, even
        # for a free-standing ``wallet_address`` body, because that was
        # exactly the audit-#14 HIGH bypass: write-without-ownership.
        if not signature or not message or not signed_address:
            if legacy_address:
                log.warning(
                    "Rejected unsigned Polymarket connect on portfolio path "
                    "for user_id=%s — client still POSTing wallet_address "
                    "without SIWE signature.",
                    uid,
                )
            return JSONResponse(
                {
                    "error": (
                        "Signature required: GET "
                        "/api/markets/connect/polymarket/nonce first, then "
                        "POST {address, signature, message} to this path "
                        "(or to /api/markets/connect/polymarket — both "
                        "verify the same SIWE proof)."
                    ),
                },
                status_code=400,
            )

        # ── SIWE verification (same logic as market_routes path) ────
        if not _mr._EVM_ADDRESS_RE.fullmatch(signed_address):
            return JSONResponse(
                {"error": "Valid wallet address required (0x followed by 40 hex characters)"},
                status_code=400,
            )

        parsed = _mr._siwe_parse_message(message)
        nonce = parsed["nonce"]
        if not nonce:
            return JSONResponse(
                {"error": "Missing nonce in signed message"}, status_code=400,
            )
        if parsed["domain"] != _mr.SIWE_DOMAIN:
            log.warning(
                "SIWE connect (portfolio): bad domain %r for user_id=%s",
                parsed["domain"], uid,
            )
            return JSONResponse(
                {"error": "Signed message domain mismatch"}, status_code=400,
            )
        if parsed["uri"] != _mr.SIWE_URI:
            return JSONResponse(
                {"error": "Signed message domain mismatch"}, status_code=400,
            )
        if parsed["chain_id"] != str(_mr.SIWE_CHAIN_ID):
            return JSONResponse(
                {"error": "Signed message chain id mismatch"}, status_code=400,
            )
        if parsed["version"] != _mr.SIWE_VERSION:
            return JSONResponse(
                {"error": "Signed message version mismatch"}, status_code=400,
            )
        if not parsed["address"]:
            return JSONResponse(
                {"error": "Missing address in signed message"}, status_code=400,
            )

        recovered = _mr._siwe_recover_signer(message, signature)
        if not recovered:
            return JSONResponse({"error": "Invalid signature"}, status_code=400)
        if parsed["address"].lower() != recovered.lower():
            log.warning(
                "SIWE connect (portfolio): in-body address does not match "
                "recovered signer for user_id=%s", uid,
            )
            return JSONResponse(
                {"error": "Signed message address does not match recovered signer"},
                status_code=400,
            )
        if recovered.lower() != signed_address.lower():
            log.warning(
                "SIWE connect (portfolio): signer mismatch for user_id=%s",
                uid,
            )
            return JSONResponse(
                {"error": "Signature does not match wallet address"},
                status_code=400,
            )

        ok, reason = _mr._siwe_consume_nonce(nonce, uid)
        if not ok:
            log.warning(
                "SIWE connect (portfolio): nonce check failed for user_id=%s — %s",
                uid, reason,
            )
            return JSONResponse({"error": reason}, status_code=400)

        # Final defense: a verified wallet must not already be attached
        # to a different narve account. Without this an attacker who
        # owns wallet W can attach it to their own account and harvest
        # the *positions* (public on-chain) of any victim who also tried
        # to attach W — but more importantly, the audit calls out that
        # the parallel storage tables let two users claim the same
        # address. Check both ``polymarket_connections`` (this route's
        # storage) and ``user_market_credentials`` (the markets route's
        # storage) so the "one wallet, one account" invariant holds
        # regardless of which surface attached it first.
        address = signed_address.lower()
        with db.conn() as c:
            other_poly = c.execute(
                "SELECT user_id FROM polymarket_connections "
                "WHERE LOWER(wallet_address) = ? AND user_id != ?",
                (address, uid),
            ).fetchone()
            other_creds = c.execute(
                "SELECT user_id FROM user_market_credentials "
                "WHERE source = 'polymarket' "
                "  AND LOWER(polymarket_wallet_address) = ? "
                "  AND user_id != ? AND is_active = 1",
                (address, uid),
            ).fetchone()
        if other_poly or other_creds:
            log.warning(
                "SIWE connect (portfolio): wallet already attached to "
                "another account; refusing to attach to user_id=%s", uid,
            )
            return JSONResponse(
                {"error": "Wallet is already attached to another account."},
                status_code=409,
            )

        polymarket.upsert_connection(uid, address)
        log.info(
            "SIWE connect (portfolio): user_id=%s connected Polymarket wallet %s",
            uid, address[:10] + "...",
        )
        return JSONResponse(
            {
                "connected": True,
                "wallet_address": address,
                "verified": True,
            }
        )

    # ── Kalshi connect ──────────────────────────────────────────────────
    @app.post("/api/portfolio/kalshi/connect")
    async def connect_kalshi(request: Request):
        user = _require_trading_addon(request)
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

        # AUDIT #14 HIGH — Kalshi credential-stuffing throttle.
        # ``test_kalshi_throttle.py`` pinned three independent buckets that
        # the route was missing: the upstream Kalshi login was reachable at
        # the rate of ``with_idempotency``'s 10-second debounce (≈360 calls/h
        # per user) with arbitrary victim emails. Stack three limits so a
        # single compromised paying account, a single user, AND a single
        # source IP each have a separate ceiling — credential-spray needs
        # all three to break before the route fires upstream.
        #
        # Ordering: target-email first (cheapest to evaluate, narrowest
        # blast radius), then per-user, then per-IP burst. Each hit returns
        # 429 with ``Retry-After`` so honest clients can back off.
        from server import _is_rate_limited, _get_client_ip  # type: ignore
        uid_for_rl = _user_id(user)
        client_ip = _get_client_ip(request)
        # Per-target-email: 5 attempts per hour against any one Kalshi
        # account. Honest fat-finger / forgotten-password fits under five;
        # spray across many victim emails trips per-user/per-IP first.
        if _is_rate_limited(
            f"kalshi_connect_target:{email.lower()}", 5, 3600,
        ):
            return JSONResponse(
                {"error": "Too many connect attempts for this email. "
                          "Please wait before trying again."},
                status_code=429,
                headers={"Retry-After": "3600"},
            )
        # Per-narve-user: 10 attempts per hour total. Caps a single
        # compromised paying session from being weaponised as a sprayer.
        if _is_rate_limited(
            f"kalshi_connect_user:{uid_for_rl}", 10, 3600,
        ):
            return JSONResponse(
                {"error": "Too many Kalshi connect attempts. "
                          "Please wait before trying again."},
                status_code=429,
                headers={"Retry-After": "3600"},
            )
        # Per-source-IP burst: 30 attempts per 10 minutes. Wider window so
        # we don't punish a household NAT; tight enough to catch a single
        # IP rotating accounts/emails fast.
        if _is_rate_limited(
            f"kalshi_connect_ip:{client_ip}", 30, 600,
        ):
            return JSONResponse(
                {"error": "Too many Kalshi connect attempts from this "
                          "network. Please wait before trying again."},
                status_code=429,
                headers={"Retry-After": "600"},
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
        user = _require_trading_addon(request)
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
        user = _require_trading_addon(request)
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

    # ── Position sync ───────────────────────────────────────────────────
    # Fans out to both Polymarket + Kalshi sync_positions for the caller.
    # Gated behind the Trading Add-on: position-sync is the heavy-lifting
    # part of the monetised flow (talks to upstream exchanges, normalises
    # positions, writes user_positions rows), so free users must hit the
    # upsell. ``not_connected`` from either side is surfaced as a per-
    # platform field — we don't 4xx on missing connections because a user
    # may legitimately connect only one of the two platforms.
    @app.post("/api/portfolio/sync")
    async def sync_positions(request: Request):
        user = _require_trading_addon(request)
        uid = _user_id(user)
        try:
            poly_result = await polymarket.sync_positions(uid)
        except Exception as exc:
            log.warning("polymarket sync raised for user=%s: %s", uid, exc)
            poly_result = {"count": 0, "error": str(exc)}
        try:
            kalshi_result = await kalshi.sync_positions(uid)
        except Exception as exc:
            log.warning("kalshi sync raised for user=%s: %s", uid, exc)
            kalshi_result = {"count": 0, "error": str(exc)}
        return JSONResponse({
            "polymarket": poly_result,
            "kalshi": kalshi_result,
        })

    # ── Disconnect: Polymarket ──────────────────────────────────────────
    # User-initiated removal of the platform credential. We keep the row
    # in user_market_credentials so the UI can still show "Reconnect", but
    # drop the cached positions so the dashboard doesn't display stale
    # holdings. Gated behind the Trading Add-on — disconnecting is a
    # state-mutating action on a paid-product surface.
    @app.post("/api/portfolio/polymarket/disconnect")
    async def disconnect_polymarket(request: Request):
        user = _require_trading_addon(request)
        uid = _user_id(user)
        db.disconnect_market_credential(uid, "polymarket")
        db.delete_user_positions(uid, platform="polymarket")
        log.info("user=%s disconnected polymarket", uid)
        return JSONResponse({"disconnected": True, "platform": "polymarket"})

    # ── Disconnect: Kalshi ──────────────────────────────────────────────
    # Same shape as the Polymarket disconnect. Scrubs the encrypted Kalshi
    # token via ``db.disconnect_market_credential`` (member_id is kept so
    # the UI can show "Reconnect jake@email.com") and clears the cached
    # positions row.
    @app.post("/api/portfolio/kalshi/disconnect")
    async def disconnect_kalshi(request: Request):
        user = _require_trading_addon(request)
        uid = _user_id(user)
        db.disconnect_market_credential(uid, "kalshi")
        db.delete_user_positions(uid, platform="kalshi")
        log.info("user=%s disconnected kalshi", uid)
        return JSONResponse({"disconnected": True, "platform": "kalshi"})

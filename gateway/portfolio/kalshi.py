"""Kalshi connection + position fetch.

Kalshi requires a password login in exchange for a bearer token. The
token expires on the order of 24-48h; we store it encrypted (Fernet)
under the user's ``kalshi_connections`` row. Re-login happens lazily
when sync fails with 401 — we do NOT retain the password.

Environment:
  KALSHI_API_BASE               (default https://trading-api.kalshi.com/trade-api/v2)
  CREDENTIALS_ENCRYPTION_KEY    (Fernet key; 32 url-safe b64 bytes)

The encryption key is also documented in .env.example with generation
instructions. A missing key on connect returns 503 — we refuse to
accept credentials we can't store encrypted.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import httpx


log = logging.getLogger("portfolio.kalshi")


def _api_base() -> str:
    return os.environ.get(
        "KALSHI_API_BASE", "https://trading-api.kalshi.com/trade-api/v2",
    ).rstrip("/")


def _fernet():
    """Return a Fernet instance, or None if the key isn't configured.

    We lazy-import so the ``cryptography`` package stays an optional
    dep for dev clones that don't touch portfolio integration.
    """
    key = os.environ.get("CREDENTIALS_ENCRYPTION_KEY", "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        log.warning("cryptography package missing — Kalshi connect disabled")
        return None
    try:
        return Fernet(key.encode())
    except Exception as exc:  # pragma: no cover — bad key shape
        log.error("CREDENTIALS_ENCRYPTION_KEY is invalid: %s", exc)
        return None


def encrypt_token(token: str) -> Optional[str]:
    f = _fernet()
    if not f:
        return None
    return f.encrypt(token.encode()).decode()


def decrypt_token(ciphertext: str) -> Optional[str]:
    f = _fernet()
    if not f:
        return None
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except Exception as exc:
        log.warning("Kalshi token decrypt failed: %s", exc)
        return None


async def login(email: str, password: str) -> dict:
    """Call Kalshi /login. Raises httpx.HTTPError on HTTP failures.

    Returns the raw JSON (``{token, member_id, ...}``); storage is the
    caller's job so we can encrypt immediately and discard the plaintext.
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        resp = await client.post(
            f"{_api_base()}/login",
            json={"email": email, "password": password},
        )
        resp.raise_for_status()
        return resp.json()


def upsert_connection(
    *, user_id: int, email: str, token: str, member_id: Optional[str] = None,
    token_expires_at: Optional[int] = None,
) -> bool:
    """Encrypt and store the Kalshi session for ``user_id``.

    Returns True on success, False if encryption isn't available
    (missing key → we refuse to store plaintext).
    """
    import db
    encrypted = encrypt_token(token)
    if encrypted is None:
        return False
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "INSERT INTO kalshi_connections "
            "(user_id, email, encrypted_token, member_id, connected_at, "
            " token_expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "  email = excluded.email, "
            "  encrypted_token = excluded.encrypted_token, "
            "  member_id = excluded.member_id, "
            "  connected_at = excluded.connected_at, "
            "  token_expires_at = excluded.token_expires_at, "
            "  sync_error = NULL, "
            "  sync_error_count = 0",
            (user_id, email.lower(), encrypted, member_id, now, token_expires_at),
        )
    return True


def get_connection(user_id: int) -> Optional[dict]:
    import db
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM kalshi_connections WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


async def fetch_positions(token: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        resp = await client.get(
            f"{_api_base()}/portfolio/positions",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, dict):
        data = data.get("market_positions") or data.get("positions") or []
    return data if isinstance(data, list) else []


def _normalise(row: dict) -> Optional[dict]:
    ticker = row.get("ticker") or row.get("market_ticker")
    if not ticker:
        return None
    # Kalshi gives separate yes/no position counts; we emit one row per
    # non-zero side so the UI can render them independently.
    positions: list[dict] = []
    for side_key, side_label in (("position", "yes"), ("no_position", "no")):
        shares = row.get(side_key)
        if shares is None or abs(shares) < 1e-9:
            continue
        positions.append({
            "platform": "kalshi",
            "market_id": f"kalshi:{ticker}",
            "market_question": row.get("title"),
            "side": side_label,
            "shares": float(shares) / 100.0,  # Kalshi uses cents; scale to units
            "entry_price": _cents_to_usd(row.get("resting_order_avg_price")),
            "current_price": _cents_to_usd(row.get("last_price")),
            "position_value_usd": _cents_to_usd(row.get("market_exposure")),
            "unrealised_pnl_usd": _cents_to_usd(row.get("unrealized_pnl")),
            "realised_pnl_usd": _cents_to_usd(row.get("realized_pnl")) or 0.0,
            "opened_at": row.get("created_at"),
        })
    return positions  # type: ignore[return-value]


def _cents_to_usd(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v) / 100.0
    except (TypeError, ValueError):
        return None


async def sync_positions(user_id: int) -> dict:
    import db
    conn_row = get_connection(user_id)
    if not conn_row:
        return {"count": 0, "error": "not_connected"}
    token = decrypt_token(conn_row["encrypted_token"])
    if token is None:
        return {"count": 0, "error": "decrypt_failed"}

    now = int(time.time())
    try:
        raw = await fetch_positions(token)
    except httpx.HTTPStatusError as exc:
        # 401 → token expired. Caller can prompt for re-connect.
        status = exc.response.status_code
        with db.conn() as c:
            c.execute(
                "UPDATE kalshi_connections SET "
                "  sync_error = ?, "
                "  sync_error_count = sync_error_count + 1 "
                "WHERE user_id = ?",
                (f"HTTP {status}", user_id),
            )
        return {"count": 0, "error": f"http_{status}"}
    except Exception as exc:
        with db.conn() as c:
            c.execute(
                "UPDATE kalshi_connections SET "
                "  sync_error = ?, "
                "  sync_error_count = sync_error_count + 1 "
                "WHERE user_id = ?",
                (str(exc)[:500], user_id),
            )
        log.warning("kalshi sync failed for user=%s: %s", user_id, exc)
        return {"count": 0, "error": str(exc)}

    # Flatten the list-of-lists the normaliser returns.
    normalised: list[dict] = []
    for row in raw:
        result = _normalise(row)
        if result:
            normalised.extend(result)  # type: ignore[arg-type]

    with db.conn() as c:
        c.execute(
            "DELETE FROM user_positions "
            "WHERE user_id = ? AND platform = 'kalshi'",
            (user_id,),
        )
        for n in normalised:
            c.execute(
                "INSERT OR REPLACE INTO user_positions "
                "(user_id, platform, market_id, market_question, side, "
                " shares, entry_price, current_price, position_value_usd, "
                " unrealised_pnl_usd, realised_pnl_usd, opened_at, last_synced_at) "
                "VALUES (?, 'kalshi', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id, n["market_id"], n.get("market_question"),
                    n["side"], n["shares"], n.get("entry_price"),
                    n.get("current_price"), n.get("position_value_usd"),
                    n.get("unrealised_pnl_usd"), n.get("realised_pnl_usd") or 0.0,
                    n.get("opened_at"), now,
                ),
            )
        c.execute(
            "UPDATE kalshi_connections SET "
            "  last_synced_at = ?, sync_error = NULL, sync_error_count = 0 "
            "WHERE user_id = ?",
            (now, user_id),
        )
    return {"count": len(normalised), "error": None}

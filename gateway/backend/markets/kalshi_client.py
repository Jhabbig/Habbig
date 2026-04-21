"""Kalshi API wrapper — auth, markets, trading, portfolio.

Supports an optional service account for fetching public market data. Kalshi v2
requires a Bearer token on every endpoint, including market listing, so we can
lazy-login once with service credentials and cache the token for all public
fetches. Per-user tokens are still used for trading / portfolio endpoints.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

log = logging.getLogger("gateway.kalshi")

KALSHI_API_BASE = "https://trading-api.kalshi.com/trade-api/v2"

# Service tokens are refreshed this long before their estimated expiry
SERVICE_TOKEN_TTL_SEC = 20 * 3600  # assume ~24h token lifetime, refresh at 20h
SERVICE_TOKEN_REFRESH_MARGIN = 3600  # refresh if <1h remaining

# Explicit per-call timeout (M14): even though the httpx.AsyncClient is
# constructed with a client-level timeout, passing ``timeout=`` on every
# request guarantees that an accidental client swap to one with
# ``timeout=None`` cannot leave us hanging on an unresponsive Kalshi
# endpoint.
REQUEST_TIMEOUT_SEC = 15.0

# Exponential backoff after login failures (L9). Prevents both
# brute-force lockout by Kalshi and log-flood on sustained outages.
_LOGIN_BACKOFF_START = 30.0     # seconds
_LOGIN_BACKOFF_MAX = 600.0      # 10 minutes
_LOGIN_BACKOFF_FACTOR = 2.0


class KalshiClient:
    """Async wrapper around the Kalshi trading API v2."""

    def __init__(
        self,
        base_url: str = KALSHI_API_BASE,
        timeout: float = 15.0,
        *,
        service_email: Optional[str] = None,
        service_password: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None
        self._timeout = timeout
        # Service account (optional) — used to fetch public market data.
        #
        # SECURITY (M15): we DO NOT retain the plaintext password on
        # ``self``. A long-lived process that holds the password in
        # memory is a juicy target for core-dump / heap-inspection
        # attacks, and it is rarely needed after the first successful
        # login (the refresh token / service token carries the session
        # forward). Instead we hold the password in a closure-scoped
        # ``_password_provider`` callable that is cleared as soon as
        # login succeeds. Callers who supply ``service_password`` get
        # exactly one login attempt; after that the refresh token is
        # the only credential in memory.
        self._service_email = service_email
        _pw = service_password  # local so the closure captures it
        def _provider() -> Optional[str]:
            return _pw
        self._password_provider: Optional[callable] = _provider if service_password else None  # type: ignore[assignment]
        # Deliberately NOT stored: self._service_password. See above.
        self._service_token: Optional[str] = None
        self._service_token_expires_at: float = 0.0
        self._service_refresh_token: Optional[str] = None
        # Exponential backoff state for failed logins (L9).
        self._login_next_attempt_at: float = 0.0
        self._login_backoff: float = _LOGIN_BACKOFF_START
        # Lock is created lazily on first use to avoid binding to a loop at import time
        self._service_login_lock: Optional[asyncio.Lock] = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=5.0)
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    # ── Service token (lazy-login, cached, auto-refresh) ─────────────────────

    async def _get_service_token(self, *, force_refresh: bool = False) -> Optional[str]:
        """Return a cached service-account token, logging in if needed.

        Returns None if no service credentials are configured. On 401/403 from
        the service login, returns None — caller falls back to unauthenticated
        requests (which Kalshi currently rejects for public data).
        """
        if not self._service_email or self._password_provider is None:
            return None

        now = time.time()
        if (
            not force_refresh
            and self._service_token
            and self._service_token_expires_at - now > SERVICE_TOKEN_REFRESH_MARGIN
        ):
            return self._service_token

        # Serialise concurrent logins so we only issue one POST /login at a time
        if self._service_login_lock is None:
            self._service_login_lock = asyncio.Lock()
        async with self._service_login_lock:
            # Re-check after acquiring the lock in case another task refreshed
            now = time.time()
            if (
                not force_refresh
                and self._service_token
                and self._service_token_expires_at - now > SERVICE_TOKEN_REFRESH_MARGIN
            ):
                return self._service_token

            # L9: honour the exponential-backoff window. We silently
            # return None while backing off so callers fall back to
            # cached market data rather than hammering /login.
            if now < self._login_next_attempt_at:
                log.debug(
                    "Kalshi login suppressed by backoff (%.1fs remaining)",
                    self._login_next_attempt_at - now,
                )
                return None

            # M15: pull the password from the closure, use it once,
            # then move on. We never copy it onto ``self``.
            password = self._password_provider() if self._password_provider else None
            if not password:
                return None

            result = await self.login(self._service_email, password)
            # Scrub the local reference promptly.
            password = None  # noqa: F841

            if "error" in result or not result.get("token"):
                log.error(
                    "Kalshi service-account login failed: %s",
                    result.get("error", "unknown"),
                )
                self._service_token = None
                self._service_token_expires_at = 0.0
                # Schedule the next login attempt with exponential backoff.
                self._login_next_attempt_at = time.time() + self._login_backoff
                self._login_backoff = min(
                    self._login_backoff * _LOGIN_BACKOFF_FACTOR,
                    _LOGIN_BACKOFF_MAX,
                )
                return None

            # Success — store ONLY the session + refresh tokens. The
            # plaintext password is already out of scope here.
            self._service_token = result["token"]
            self._service_token_expires_at = time.time() + SERVICE_TOKEN_TTL_SEC
            self._service_refresh_token = result.get("refresh_token") or self._service_refresh_token
            # Reset backoff on success.
            self._login_next_attempt_at = 0.0
            self._login_backoff = _LOGIN_BACKOFF_START
            log.info("Kalshi service-account login succeeded")
            return self._service_token

    async def _public_headers(self) -> dict[str, str]:
        """Return headers for public data fetches — includes service token if available."""
        token = await self._get_service_token()
        if token:
            return self._auth_headers(token)
        return {}

    # ── Authentication ───────────────────────────────────────────────────────

    async def login(self, email: str, password: str) -> dict:
        """Authenticate with Kalshi. Returns {token, member_id} or {error}.

        The password is used once to obtain a session token and is NEVER stored.
        """
        client = await self._ensure_client()
        try:
            resp = await client.post(
                f"{self.base_url}/login",
                json={"email": email, "password": password},
                timeout=REQUEST_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "token": data.get("token", ""),
                "member_id": data.get("member_id", ""),
            }
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in (401, 403):
                return {"error": "Invalid credentials", "status_code": status}
            return {"error": f"Kalshi API error ({status})", "status_code": status}
        except httpx.RequestError as e:
            log.error("Kalshi login request error: %s", e)
            return {"error": "Kalshi API unavailable"}

    # ── Markets (public, no auth needed) ─────────────────────────────────────

    async def get_markets(
        self,
        *,
        limit: int = 100,
        cursor: str = "",
        status: str = "open",
    ) -> dict:
        """Fetch markets with pagination. Returns {markets, cursor}.

        Kalshi v2 requires auth on /markets. If a service account is
        configured, its token is attached automatically; on 401 the token is
        force-refreshed and the call is retried once.
        """
        client = await self._ensure_client()
        params: dict = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor

        async def _call(headers: dict[str, str]) -> httpx.Response:
            return await client.get(
                f"{self.base_url}/markets",
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SEC,
            )

        try:
            headers = await self._public_headers()
            resp = await _call(headers)
            if resp.status_code in (401, 403) and self._service_email:
                # Cached token may be stale — force refresh and retry once
                log.info("Kalshi service token rejected, refreshing and retrying")
                headers = await self._public_headers_force_refresh()
                resp = await _call(headers)
            resp.raise_for_status()
            data = resp.json()
            return {
                "markets": data.get("markets", []),
                "cursor": data.get("cursor", ""),
            }
        except httpx.HTTPStatusError as e:
            log.error("Kalshi markets HTTP %d: %s", e.response.status_code, e)
            return {"markets": [], "cursor": ""}
        except httpx.RequestError as e:
            log.error("Kalshi markets request error: %s", e)
            return {"markets": [], "cursor": ""}

    async def _public_headers_force_refresh(self) -> dict[str, str]:
        token = await self._get_service_token(force_refresh=True)
        return self._auth_headers(token) if token else {}

    async def get_all_markets(self, *, max_pages: int = 10) -> list[dict]:
        """Paginate through all open markets."""
        all_markets: list[dict] = []
        cursor = ""
        for _ in range(max_pages):
            result = await self.get_markets(cursor=cursor)
            batch = result["markets"]
            if not batch:
                break
            all_markets.extend(batch)
            cursor = result["cursor"]
            if not cursor:
                break
        return all_markets

    async def get_market(self, ticker: str) -> Optional[dict]:
        """Fetch a single market by ticker. Uses service-account auth if configured."""
        client = await self._ensure_client()

        async def _call(headers: dict[str, str]) -> httpx.Response:
            return await client.get(
                f"{self.base_url}/markets/{ticker}",
                headers=headers,
                timeout=REQUEST_TIMEOUT_SEC,
            )

        try:
            headers = await self._public_headers()
            resp = await _call(headers)
            if resp.status_code in (401, 403) and self._service_email:
                headers = await self._public_headers_force_refresh()
                resp = await _call(headers)
            resp.raise_for_status()
            data = resp.json()
            return data.get("market", data)
        except httpx.HTTPStatusError:
            return None
        except httpx.RequestError as e:
            log.error("Kalshi market detail error: %s", e)
            return None

    # ── Trading (requires auth token) ────────────────────────────────────────

    async def get_balance(self, token: str) -> dict:
        """Get user's balance."""
        client = await self._ensure_client()
        try:
            resp = await client.get(
                f"{self.base_url}/portfolio/balance",
                headers=self._auth_headers(token),
                timeout=REQUEST_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                return {"error": "token_expired", "status_code": e.response.status_code}
            return {"error": f"Kalshi API error ({e.response.status_code})"}
        except httpx.RequestError as e:
            log.error("Kalshi balance error: %s", e)
            return {"error": "Kalshi API unavailable"}

    async def get_positions(self, token: str) -> dict:
        """Get user's open positions. Returns {positions: [...]} or {error: ...}."""
        client = await self._ensure_client()
        try:
            resp = await client.get(
                f"{self.base_url}/portfolio/positions",
                headers=self._auth_headers(token),
                timeout=REQUEST_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            data = resp.json()
            return {"positions": data.get("market_positions", [])}
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                return {"error": "token_expired", "positions": []}
            log.error("Kalshi positions HTTP %d", e.response.status_code)
            return {"error": f"HTTP {e.response.status_code}", "positions": []}
        except httpx.RequestError as e:
            log.error("Kalshi positions error: %s", e)
            return {"error": "Kalshi API unavailable", "positions": []}

    async def get_orders(self, token: str) -> dict:
        """Get user's orders. Returns {orders: [...]} or {error: ...}."""
        client = await self._ensure_client()
        try:
            resp = await client.get(
                f"{self.base_url}/portfolio/orders",
                headers=self._auth_headers(token),
                timeout=REQUEST_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            data = resp.json()
            return {"orders": data.get("orders", [])}
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                return {"error": "token_expired", "orders": []}
            return {"error": f"HTTP {e.response.status_code}", "orders": []}
        except httpx.RequestError as e:
            log.error("Kalshi orders error: %s", e)
            return {"error": "Kalshi API unavailable", "orders": []}

    async def place_order(
        self,
        token: str,
        *,
        ticker: str,
        action: str = "buy",
        side: str = "yes",
        order_type: str = "market",
        count: int = 1,
        price: Optional[int] = None,
    ) -> dict:
        """Place an order on Kalshi.

        Args:
            ticker: Market ticker
            action: "buy" or "sell"
            side: "yes" or "no"
            order_type: "market" or "limit"
            count: Number of contracts
            price: Limit price in cents (1-99), required for limit orders
        """
        if side not in ("yes", "no"):
            return {"error": f"Invalid side: {side}"}
        if action not in ("buy", "sell"):
            return {"error": f"Invalid action: {action}"}
        if count < 1:
            return {"error": "Count must be at least 1"}
        client = await self._ensure_client()
        body: dict = {
            "ticker": ticker,
            "action": action,
            "type": order_type,
            "count": count,
            "side": side,
        }
        # Only set the side-specific price for limit orders; omit the other
        # side entirely rather than sending it as null (Kalshi rejects null).
        if order_type == "limit" and price is not None:
            if side == "yes":
                body["yes_price"] = price
            else:
                body["no_price"] = price
        try:
            resp = await client.post(
                f"{self.base_url}/portfolio/orders",
                json=body,
                headers=self._auth_headers(token),
                timeout=REQUEST_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            data = resp.json()
            order = data.get("order", data)
            # Kalshi returns filled_count (contracts filled so far) and
            # remaining_count. Use filled_count, falling back to 0.
            return {
                "order_id": order.get("order_id", ""),
                "status": order.get("status", ""),
                "filled": int(order.get("filled_count", 0) or 0),
                "remaining": int(order.get("remaining_count", 0) or 0),
            }
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in (401, 403):
                return {"error": "token_expired", "status_code": status}
            body_text = e.response.text
            log.error("Kalshi order error HTTP %d: %s", status, body_text)
            return {"error": body_text, "status_code": status}
        except httpx.RequestError as e:
            log.error("Kalshi order request error: %s", e)
            return {"error": "Kalshi API unavailable"}

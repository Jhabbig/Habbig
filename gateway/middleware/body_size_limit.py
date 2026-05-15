"""Request body size cap — the memory-DoS backstop.

Audit HIGH FIX D. Sits at the outermost edge of the middleware stack
(LAST in ``app.add_middleware()`` order so it's FIRST in dispatch).
Rejects any inbound request whose body would exceed ``MAX_BODY_BYTES``
(default 2 MB) with HTTP 413 *before* any downstream middleware reads
``await request.body()`` or ``request.json()``. Without this, an
attacker can POST a multi-megabyte JSON blob and force every middleware
on the chain to buffer it in memory before the application-level
validator notices.

Design constraints
------------------

* **Cheap when honest.** A client that sends a valid ``Content-Length``
  header — and that header is ≤ cap — pays exactly one integer compare
  and falls through.

* **Honest when streaming.** Clients that don't send ``Content-Length``
  (chunked transfer-encoding, proxies stripping the header) are caught
  by wrapping the ASGI ``receive`` callable and tallying bytes as they
  arrive. The first chunk over the cap aborts with 413.

* **GET/HEAD/DELETE/OPTIONS are skipped.** They MAY carry a body per
  RFC 9110, but in practice ours don't, and applying the cap would
  punish CORS preflights.

* **Per-route exemption.** A small set of routes (configured via
  ``BODY_SIZE_LIMIT_EXEMPT_PREFIXES`` env var, comma-separated) can opt
  into a larger cap controlled by ``BODY_SIZE_LIMIT_EXEMPT_BYTES``
  (default 10 MB). Use sparingly; intended for image uploads.

Errors return a small JSON payload rather than the default Starlette
plain-text 413 so the client gets a parseable shape consistent with the
rest of the API.
"""

from __future__ import annotations

import logging
import os
from typing import Tuple

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


log = logging.getLogger("middleware.body_size_limit")


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
        if val > 0:
            return val
    except ValueError:
        pass
    log.warning("invalid %s=%r — using default %d", name, raw, default)
    return default


def _list_env(name: str) -> Tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return ()
    return tuple(p.strip() for p in raw.split(",") if p.strip())


# Default 2 MB cap.
MAX_BODY_BYTES = _int_env("MAX_BODY_BYTES", 2 * 1024 * 1024)

# Per-prefix higher cap. Default 10 MB.
EXEMPT_MAX_BODY_BYTES = _int_env("BODY_SIZE_LIMIT_EXEMPT_BYTES", 10 * 1024 * 1024)

# Comma-separated path prefixes that get EXEMPT_MAX_BODY_BYTES.
EXEMPT_PREFIXES = _list_env("BODY_SIZE_LIMIT_EXEMPT_PREFIXES")

# Methods that conventionally do not carry a request body.
_NO_BODY_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE", "TRACE"})


def _cap_for_path(path: str) -> int:
    for prefix in EXEMPT_PREFIXES:
        if path.startswith(prefix):
            return EXEMPT_MAX_BODY_BYTES
    return MAX_BODY_BYTES


def _too_large_response(cap: int, observed: int | None = None) -> JSONResponse:
    payload = {
        "error": "Payload Too Large",
        "max_bytes": cap,
    }
    if observed is not None:
        payload["received_bytes"] = observed
    return JSONResponse(payload, status_code=413)


class BodySizeLimitMiddleware:
    """ASGI middleware enforcing ``MAX_BODY_BYTES`` per request.

    Implemented at the raw ASGI layer (not ``BaseHTTPMiddleware``) so it
    can intercept ``receive`` *before* any other middleware buffers the
    body.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = (scope.get("method") or "").upper()
        if method in _NO_BODY_METHODS:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        cap = _cap_for_path(path)

        # Fast path: trust Content-Length if present and well-formed.
        cl_header = _header_value(scope, b"content-length")
        if cl_header is not None:
            try:
                cl = int(cl_header)
            except ValueError:
                await _send_too_large(send, cap)
                return
            if cl < 0:
                await _send_too_large(send, cap)
                return
            if cl > cap:
                log.info(
                    "413 content-length over cap path=%s cl=%d cap=%d",
                    path, cl, cap,
                )
                await _send_too_large(send, cap, observed=cl)
                return
            await self.app(scope, receive, send)
            return

        # Slow path: chunked / no Content-Length — tally as bytes arrive.
        wrapped_receive = _make_counting_receive(receive, cap, path)
        sent_response = {"value": False}

        async def _send_intercept(message: Message) -> None:
            sent_response["value"] = True
            await send(message)

        try:
            await self.app(scope, wrapped_receive, _send_intercept)
        except _BodyTooLarge as exc:
            log.info(
                "413 chunked body over cap path=%s observed=%d cap=%d",
                path, exc.observed, exc.cap,
            )
            if not sent_response["value"]:
                await _send_too_large(send, exc.cap, observed=exc.observed)


def _header_value(scope: Scope, name: bytes) -> str | None:
    for k, v in scope.get("headers") or ():
        if k == name:
            try:
                return v.decode("latin-1")
            except Exception:
                return None
    return None


class _BodyTooLarge(Exception):
    """Internal signal — raised by the counting receive when the cap trips."""

    def __init__(self, observed: int, cap: int) -> None:
        super().__init__(f"body too large: {observed} > {cap}")
        self.observed = observed
        self.cap = cap


def _make_counting_receive(receive: Receive, cap: int, path: str) -> Receive:
    """Wrap ``receive`` to count body bytes and abort if cap is exceeded."""
    state = {"total": 0, "tripped": False}

    async def _receive() -> Message:
        if state["tripped"]:
            return {"type": "http.disconnect"}
        message = await receive()
        if message.get("type") == "http.request":
            body = message.get("body") or b""
            state["total"] += len(body)
            if state["total"] > cap:
                state["tripped"] = True
                raise _BodyTooLarge(state["total"], cap)
        return message

    return _receive


async def _send_too_large(send: Send, cap: int, observed: int | None = None) -> None:
    """Emit a 413 response directly via the ASGI send channel."""
    response = _too_large_response(cap, observed)
    await response(
        {"type": "http"},  # type: ignore[arg-type]
        _noop_receive,
        send,
    )


async def _noop_receive() -> Message:
    return {"type": "http.disconnect"}

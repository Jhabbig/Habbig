"""Sentry initialization for the love-dashboard subproduct.

Modeled on gateway/observability/sentry_setup.py and the sibling
whale-dashboard/observability.py. Everything is fail-soft: if
sentry-sdk is not installed or no DSN is configured, init_sentry()
just logs and continues. The dashboard must never crash because
observability is missing.

Call init_sentry() at the very top of server.py, BEFORE FastAPI is
imported — the Sentry FastAPI integration only instruments apps that
exist after init.
"""

from __future__ import annotations

import logging
import os
from typing import Optional


log = logging.getLogger("love.observability")


# Headers we never want Sentry to see. x-gateway-secret authenticates the
# gateway -> subproduct hop; leaking it to Sentry would let anyone with
# Sentry-issue read access impersonate the gateway.
_SENSITIVE_HEADER_NAMES = {
    "authorization",
    "x-gateway-secret",
    "x-anthropic-api-key",
    "cookie",
    "set-cookie",
    "x-csrf-token",
}

_SENSITIVE_KEY_HINTS = (
    "password", "token", "secret", "key", "authorization",
    "cookie", "session", "jwt", "bearer", "api_key", "apikey",
)


def _scrub_headers(headers: object) -> None:
    if not isinstance(headers, dict):
        return
    for k in list(headers.keys()):
        if k.lower() in _SENSITIVE_HEADER_NAMES:
            headers[k] = "[Filtered]"


def _scrub_fields(data: object) -> None:
    if not isinstance(data, dict):
        return
    for k in list(data.keys()):
        if any(hint in k.lower() for hint in _SENSITIVE_KEY_HINTS):
            data[k] = "[Filtered]"


def scrub_sensitive_data(event: dict, hint: Optional[dict] = None) -> Optional[dict]:
    """Sentry before_send hook.

    Scrubs credential-y headers (including x-gateway-secret), cookies,
    and credential-y keys in request.data / event.extra. Never raises -
    scrubbing must never crash the SDK.
    """
    try:
        req = event.get("request") or {}
        _scrub_headers(req.get("headers"))
        cookies = req.get("cookies")
        if isinstance(cookies, dict):
            for k in list(cookies.keys()):
                cookies[k] = "[Filtered]"
        _scrub_fields(req.get("data"))
        _scrub_fields(event.get("extra"))
        query = req.get("query_string")
        if isinstance(query, str) and any(
            h in query.lower() for h in _SENSITIVE_KEY_HINTS
        ):
            req["query_string"] = "[Filtered]"
    except Exception:  # pragma: no cover - scrubbing must never crash
        pass
    return event


def init_sentry(platform: str = "love") -> bool:
    """Initialize Sentry if SENTRY_DSN_LOVE (or SENTRY_DSN) is set.

    Uses a subproduct-specific DSN so error volume can be attributed
    per sibling. Falls back to apex SENTRY_DSN if the subproduct-specific
    one is unset (operators can point all dashboards at one project if
    they prefer).

    Returns True if initialised, False otherwise. Never raises.
    """
    dsn = (
        os.getenv("SENTRY_DSN_LOVE", "").strip()
        or os.getenv("SENTRY_DSN", "").strip()
    )
    if not dsn:
        log.info("init_sentry: no DSN configured - running without Sentry")
        return False

    try:
        import sentry_sdk
    except ImportError:
        log.warning("init_sentry: sentry-sdk not installed - skipping")
        return False

    integrations = []
    try:
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        integrations.append(FastApiIntegration(transaction_style="endpoint"))
    except ImportError:
        pass

    try:
        sentry_sdk.init(
            dsn=dsn,
            integrations=integrations,
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            profiles_sample_rate=float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0.1")),
            environment=os.getenv("NARVE_ENV", os.getenv("ENVIRONMENT", "production")),
            release=os.getenv("NARVE_RELEASE", os.getenv("APP_VERSION", "unknown")),
            before_send=scrub_sensitive_data,
            send_default_pii=False,
        )
        with sentry_sdk.configure_scope() as scope:
            scope.set_tag("platform", platform)
    except Exception as e:
        log.warning("init_sentry: failed to initialise: %s", e)
        return False

    return True

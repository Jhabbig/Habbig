"""Sentry initialization and sensitive-data scrubbing.

Used by both the gateway server and scraper service. Backend and frontend
use DIFFERENT DSNs — the public frontend DSN is deliberately separate so a
leaked public key cannot access backend error data.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Optional


_SENSITIVE_FIELD_HINTS = (
    "password", "token", "secret", "key", "card", "cvv", "cvc",
    "ssn", "pin", "credit", "bank", "account_number",
)
_SENSITIVE_HEADER_NAMES = {"authorization", "x-csrf-token", "cookie", "set-cookie"}


def scrub_sensitive_data(event: dict, hint: Optional[dict] = None) -> Optional[dict]:
    """Sentry before_send hook.

    Removes anything that could contain credentials, session tokens, or
    financial data before the event ever leaves the server.
    """
    try:
        req = event.get("request") or {}
        headers = req.get("headers") or {}
        if isinstance(headers, dict):
            for key in list(headers.keys()):
                if key.lower() in _SENSITIVE_HEADER_NAMES:
                    headers[key] = "[Filtered]"
        cookies = req.get("cookies") or {}
        if isinstance(cookies, dict):
            for key in list(cookies.keys()):
                cookies[key] = "[Filtered]"
        data = req.get("data")
        if isinstance(data, dict):
            for key in list(data.keys()):
                if any(hint in key.lower() for hint in _SENSITIVE_FIELD_HINTS):
                    data[key] = "[Filtered]"
        query = req.get("query_string")
        if isinstance(query, str) and any(h in query.lower() for h in _SENSITIVE_FIELD_HINTS):
            req["query_string"] = "[Filtered]"
        extra = event.get("extra") or {}
        if isinstance(extra, dict):
            for key in list(extra.keys()):
                if any(hint in key.lower() for hint in _SENSITIVE_FIELD_HINTS):
                    extra[key] = "[Filtered]"
    except Exception:  # pragma: no cover — never crash while scrubbing
        pass
    return event


def init_sentry(platform: str = "backend") -> bool:
    """Initialize Sentry if SENTRY_DSN is set.

    Returns True if initialised, False otherwise. Safe to call repeatedly.
    """
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        logging.getLogger("sentry").warning("sentry-sdk not installed; skipping init")
        return False

    integrations = [
        LoggingIntegration(level=logging.WARNING, event_level=logging.ERROR),
    ]
    try:
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        integrations.append(FastApiIntegration(transaction_style="endpoint"))
    except ImportError:
        pass
    try:
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
        integrations.append(SqlalchemyIntegration())
    except ImportError:
        pass

    # Local import to avoid a circular dependency:
    # observability/__init__.py imports from this module.
    from observability import detect_release

    sentry_sdk.init(
        dsn=dsn,
        integrations=integrations,
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
        profiles_sample_rate=float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0.1")),
        environment=os.getenv("ENVIRONMENT", "production"),
        release=detect_release(),
        before_send=scrub_sensitive_data,
        send_default_pii=False,
    )
    with sentry_sdk.configure_scope() as scope:
        scope.set_tag("platform", platform)
    return True


def set_user_context(user_id: int, email: Optional[str] = None, tier: Optional[str] = None) -> None:
    """Attach user context to the current Sentry scope.

    The user id is hashed before being sent to Sentry so raw internal ids
    never leave the server. Email is intentionally dropped — correlating
    across events is handled via the hashed id.
    """
    try:
        import sentry_sdk
        hashed_id = hashlib.sha256(f"narve:{user_id}".encode()).hexdigest()[:16]
        sentry_sdk.set_user({"id": hashed_id})
        if tier:
            with sentry_sdk.configure_scope() as scope:
                scope.set_tag("tier", tier)
    except Exception:
        pass


def tag_request(**tags: Any) -> None:
    """Add arbitrary tags to the current scope."""
    try:
        import sentry_sdk
        with sentry_sdk.configure_scope() as scope:
            for k, v in tags.items():
                scope.set_tag(k, v)
    except Exception:
        pass

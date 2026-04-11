"""Sentry init for the scraper service.

Standalone so the scraper does not depend on the gateway package layout.
Shares the same scrub_sensitive_data logic as the gateway observability module.
"""

from __future__ import annotations

import logging
import os
from typing import Optional


_SENSITIVE_FIELD_HINTS = (
    "password", "token", "secret", "key", "card", "cvv", "cvc",
    "ssn", "pin", "credit", "bank", "account_number",
)
_SENSITIVE_HEADER_NAMES = {"authorization", "x-csrf-token", "cookie", "set-cookie"}


def scrub_sensitive_data(event: dict, hint: Optional[dict] = None) -> Optional[dict]:
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
        extra = event.get("extra") or {}
        if isinstance(extra, dict):
            for key in list(extra.keys()):
                if any(hint in key.lower() for hint in _SENSITIVE_FIELD_HINTS):
                    extra[key] = "[Filtered]"
    except Exception:
        pass
    return event


def init_sentry(platform: str = "scraper") -> bool:
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

    sentry_sdk.init(
        dsn=dsn,
        integrations=integrations,
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
        profiles_sample_rate=float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0.1")),
        environment=os.getenv("ENVIRONMENT", "production"),
        release=os.getenv("APP_VERSION", "1.0.0"),
        before_send=scrub_sensitive_data,
        send_default_pii=False,
    )
    with sentry_sdk.configure_scope() as scope:
        scope.set_tag("platform", platform)
    return True


def tag_scraper_platform(scraper_platform: str) -> None:
    try:
        import sentry_sdk
        with sentry_sdk.configure_scope() as scope:
            scope.set_tag("scraper_platform", scraper_platform)
    except Exception:
        pass

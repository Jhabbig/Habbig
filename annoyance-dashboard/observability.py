"""Sentry init + structured JSON logging for the annoyance dashboard.

Modeled on gateway/observability/sentry_setup.py and
gateway/logging_config.py. Everything here is fail-soft: if Sentry is not
installed or SENTRY_DSN_ANNOYANCE is unset, we just log a warning and
continue. The dashboard must never crash because observability is missing.

Call init_sentry() at the very top of server.py, BEFORE FastAPI is
imported — the Sentry FastAPI integration only instruments apps that
exist after init.
"""

from __future__ import annotations

import logging
import logging.handlers
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


log = logging.getLogger("annoyance.observability")


# Headers we never want Sentry (or the ring-buffer) to see.
_SENSITIVE_HEADER_NAMES = {
    "authorization",
    "x-gateway-secret",
    "x-anthropic-api-key",
    "cookie",
    "set-cookie",
    "x-csrf-token",
}

# Post-content fields get redacted at log level >= WARNING so a crashing
# classifier doesn't dump user posts into Sentry.
_CONTENT_FIELDS = ("content", "text", "body", "excerpt", "sample_excerpts_json")

_SENSITIVE_KEY_HINTS = (
    "password", "token", "secret", "key", "authorization",
    "cookie", "session", "jwt", "bearer", "api_key", "apikey",
)


def _scrub_headers(headers: Any) -> None:
    if not isinstance(headers, dict):
        return
    for k in list(headers.keys()):
        if k.lower() in _SENSITIVE_HEADER_NAMES:
            headers[k] = "[Filtered]"


def _scrub_fields(data: Any, *, redact_content: bool) -> None:
    if not isinstance(data, dict):
        return
    for k in list(data.keys()):
        lowered = k.lower()
        if any(hint in lowered for hint in _SENSITIVE_KEY_HINTS):
            data[k] = "[Filtered]"
        elif redact_content and lowered in _CONTENT_FIELDS:
            data[k] = "[Redacted]"


def _always_redact_content_keys(data: Any) -> None:
    """Pre-release safety: unconditionally wipe any 'content'-ish key.

    Unlike the level-gated scrubber, this runs for every Sentry event —
    we never want user post text leaving the box, even on INFO-level
    breadcrumbs or ad-hoc `capture_message` calls.
    """
    if not isinstance(data, dict):
        return
    for k in list(data.keys()):
        if k.lower() in _CONTENT_FIELDS:
            data[k] = "[Redacted]"


def _scrub_exception_frame_locals(event: dict) -> None:
    """Walk event['exception']['values'][*]['stacktrace']['frames'][*]['vars'].

    Exception frame locals are where a crashing classifier leaks the
    post text it was trying to classify (the local var is usually
    literally called ``content``). Scrub every frame's vars dict for
    any 'content'-ish key AND for any sensitive credential name.
    """
    try:
        exceptions = (event.get("exception") or {}).get("values") or []
        for exc in exceptions:
            stack = exc.get("stacktrace") or {}
            for frame in stack.get("frames") or []:
                frame_vars = frame.get("vars")
                if not isinstance(frame_vars, dict):
                    continue
                for k in list(frame_vars.keys()):
                    lowered = k.lower()
                    if lowered in _CONTENT_FIELDS:
                        frame_vars[k] = "[Redacted]"
                    elif any(hint in lowered for hint in _SENSITIVE_KEY_HINTS):
                        frame_vars[k] = "[Filtered]"
    except Exception:  # pragma: no cover
        pass


def scrub_sensitive_data(event: dict, hint: Optional[dict] = None) -> Optional[dict]:
    """Sentry before_send hook.

    Always scrubs:
      * credential-y headers (authorization, x-gateway-secret, x-anthropic-api-key, …)
      * cookies
      * credential-y keys in request.data / extra
      * 'content' key in event['extra'] and in every exception frame's locals
        (pre-release safety — we NEVER want user post text in Sentry)

    At WARNING or higher, additionally redacts content-ish keys in
    request.data so a crashing handler dumping its request body can't
    leak a post payload either.
    """
    try:
        level = (event.get("level") or "info").lower()
        redact_content_in_request = level in ("warning", "error", "fatal", "critical")

        req = event.get("request") or {}
        _scrub_headers(req.get("headers"))
        cookies = req.get("cookies")
        if isinstance(cookies, dict):
            for k in list(cookies.keys()):
                cookies[k] = "[Filtered]"
        _scrub_fields(req.get("data"), redact_content=redact_content_in_request)
        _scrub_fields(event.get("extra"), redact_content=redact_content_in_request)

        # Unconditional content scrub — runs at every level, covers extra
        # and every exception frame's locals dict.
        _always_redact_content_keys(event.get("extra"))
        _scrub_exception_frame_locals(event)

        query = req.get("query_string")
        if isinstance(query, str) and any(
            h in query.lower() for h in _SENSITIVE_KEY_HINTS
        ):
            req["query_string"] = "[Filtered]"
    except Exception:  # pragma: no cover — scrubbing must never crash
        pass
    return event


def init_sentry(platform: str = "annoyance") -> bool:
    """Initialize Sentry if SENTRY_DSN_ANNOYANCE is set.

    Uses a dashboard-specific DSN so error volume can be attributed per
    sibling. Falls back to SENTRY_DSN if the dashboard-specific one is
    unset (so operators can point all dashboards at one project if they
    prefer).

    Returns True if initialised, False otherwise. Never raises.
    """
    dsn = (
        os.getenv("SENTRY_DSN_ANNOYANCE", "").strip()
        or os.getenv("SENTRY_DSN", "").strip()
    )
    if not dsn:
        log.warning("init_sentry: no DSN configured — running without Sentry")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        log.warning("init_sentry: sentry-sdk not installed — skipping")
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
        sentry_sdk.init(
            dsn=dsn,
            integrations=integrations,
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            profiles_sample_rate=float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0.1")),
            environment=os.getenv("ENVIRONMENT", "production"),
            release=os.getenv("APP_VERSION", "0.1.0"),
            before_send=scrub_sensitive_data,
            send_default_pii=False,
        )
        with sentry_sdk.configure_scope() as scope:
            scope.set_tag("platform", platform)
    except Exception as e:
        log.warning("init_sentry: failed to initialise: %s", e)
        return False

    return True


# ── Structured JSON logging ─────────────────────────────────────────────────


_RESERVED_RECORD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
})


def _scrub_log_value(key: str, value: Any, *, redact_content: bool) -> Any:
    lowered = key.lower()
    for hint in _SENSITIVE_KEY_HINTS:
        if hint in lowered:
            return "[REDACTED]"
    if redact_content and lowered in _CONTENT_FIELDS:
        return "[REDACTED]"
    return value


class JSONFormatter(logging.Formatter):
    """Emit one JSON object per record, compatible with BetterStack ingest."""

    def __init__(self, *, service: str = "annoyance", environment: str = "production"):
        super().__init__()
        self.service = service
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        redact_content = record.levelno >= logging.WARNING
        data: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "environment": self.environment,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_"):
                continue
            data[key] = _scrub_log_value(key, value, redact_content=redact_content)
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        try:
            return json.dumps(data, default=str)
        except (TypeError, ValueError):
            safe = {k: str(v) for k, v in data.items()}
            return json.dumps(safe, default=str)


_CONFIGURED = False


def configure_logging(
    *,
    base_dir: Optional[Path] = None,
    service: str = "annoyance",
    force: bool = False,
) -> None:
    """Wire a JSON formatter to stdout + rotating file at logs/annoyance.log.

    Idempotent. Does NOT crash if base_dir cannot be created (tests).
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    if base_dir is None:
        base_dir = Path(__file__).parent

    environment = os.getenv("ENVIRONMENT", "production")
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)

    formatter = JSONFormatter(service=service, environment=environment)

    root = logging.getLogger()
    root.setLevel(level)

    # Replace any pre-existing handlers (e.g. from logging.basicConfig
    # elsewhere) so we start from a clean slate.
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        log_dir = base_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            str(log_dir / f"{service}.log"),
            maxBytes=50 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except Exception as e:
        # In tests / read-only filesystems we still want console logging.
        log.warning("configure_logging: file handler skipped: %s", e)

    # Quiet down noisy third-party loggers — their output still flows, but
    # INFO-level chatter about every HTTP request overwhelms the feed.
    for noisy in ("uvicorn.access", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def reset_for_tests() -> None:
    """Allow unit tests to re-configure logging from a clean slate."""
    global _CONFIGURED
    _CONFIGURED = False

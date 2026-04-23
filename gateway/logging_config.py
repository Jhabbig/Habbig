"""
Centralised logging configuration for all narve.ai services.

Usage:
    from logging_config import configure_logging, get_logger
    configure_logging()
    logger = get_logger(__name__)
    logger.info("Pipeline started", extra={"predictions_count": 47})

All logs are:
  - Structured JSON (parseable by BetterStack Logtail)
  - Include: timestamp, level, service, environment, logger, message
  - Auto-enriched with request_id, user_id when set via set_request_context()
  - Sent to BetterStack AND written to a local rotating file
  - Automatically scrubbed of sensitive fields (password, token, secret, etc.)

Service selection:
    Set SERVICE_NAME=app|scraper|worker in the environment.
    The matching LOGTAIL_TOKEN_{SERVICE_NAME_UPPER} is used if present.

Environment variables:
    SERVICE_NAME            # app | scraper | worker (default: "app")
    ENVIRONMENT             # production | dev (default: "production")
    LOG_LEVEL               # DEBUG | INFO | WARNING | ERROR (default: "INFO")
    LOGTAIL_TOKEN_APP       # BetterStack source token for main app
    LOGTAIL_TOKEN_SCRAPER   # BetterStack source token for scraper
    LOGTAIL_TOKEN_WORKER    # BetterStack source token for worker
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from logtail import LogtailHandler  # type: ignore
    LOGTAIL_AVAILABLE = True
except ImportError:
    LOGTAIL_AVAILABLE = False
    LogtailHandler = None  # type: ignore


# ── Config from environment ─────────────────────────────────────────────────

SERVICE_NAME = os.getenv("SERVICE_NAME", "app").lower().strip() or "app"
ENVIRONMENT = os.getenv("ENVIRONMENT", "production").strip() or "production"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper().strip()
APP_VERSION = os.getenv("APP_VERSION", "1.0.0")


# ── Request context (contextvars for async safety) ─────────────────────────

# Using contextvars instead of threading.local — async tasks spawned by the
# same event loop share thread-locals, which would leak request context
# between concurrent handlers. ContextVar is the async-safe equivalent.
_request_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "request_id", default=None
)
_user_id_var: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "user_id", default=None
)


def set_request_context(request_id: str, user_id: Optional[int] = None) -> None:
    """Call at the start of each request so every log inside gets request_id/user_id."""
    _request_id_var.set(request_id)
    _user_id_var.set(user_id)


def clear_request_context() -> None:
    """Call at the end of each request to reset context."""
    _request_id_var.set(None)
    _user_id_var.set(None)


def get_request_id() -> Optional[str]:
    return _request_id_var.get()


def get_user_id() -> Optional[int]:
    return _user_id_var.get()


# ── Structured JSON formatter ──────────────────────────────────────────────

SENSITIVE_KEY_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "auth",
    "cookie",
    "session",
    "jwt",
    "bearer",
    "card",
    "cvv",
    "cvc",
    "ssn",
    "pin",
    "private",
    "api_key",
    "apikey",
    "reset",
    "invite",
    "stripe",
    "webhook",
    "kalshi",
    "vapid",
)

# Safe fields that contain "token" / "key" in their name but are not secrets.
# Kept in a whitelist so we can emit them (e.g. when counting requests for a
# public token id or reporting which api_key_id failed).
SENSITIVE_ALLOWLIST = {
    "request_id",
    "csrf_error",
    "user_id",
    "session_id",  # bare id (not value) is fine
    "token_id",    # invite token db row id
    "posts_found_total",
}

_RESERVED_RECORD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
})


def _scrub_value(key: str, value: Any) -> Any:
    """Return the value unchanged unless the key looks sensitive."""
    lowered = key.lower()
    if lowered in SENSITIVE_ALLOWLIST:
        return value
    for hint in SENSITIVE_KEY_HINTS:
        if hint in lowered:
            return "[REDACTED]"
    return value


# Known-shape secret patterns to redact from log MESSAGE contents. These
# catch the cases that ``_scrub_value`` cannot: when a secret is
# interpolated into a format string (``log.info("...%s...", token)``)
# the arg value arrives at the formatter as an opaque string with no
# associated key, so we can only match on content.
#
# Kept intentionally small — every pattern here has to run on every log
# line, and a false positive silently hides real signal. Only patterns
# with a distinctive shape (bearer prefix, query-string param, key=
# assignment) are listed. Freeform emails / usernames / session ids
# are NOT matched here because the legitimate admin audit trail
# depends on them being visible.

import re  # noqa: E402

_MESSAGE_REDACT_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # Bearer tokens in auth headers or Authorization strings.
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{10,}"), "bearer [REDACTED]"),
    # Password embedded in a query string or URL fragment.
    (re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)=[^\s&\"']{6,}"),
     r"\1=[REDACTED]"),
    # Basic-auth user:pass in a URL (scheme://user:pass@host).
    (re.compile(r"(?i)([a-z]+)://([^:@\s/]+):([^@\s/]+)@"), r"\1://\2:[REDACTED]@"),
)


def _redact_message(msg: str) -> str:
    """Apply the known-shape regexes to a log message string. Returns
    the message unchanged when no pattern matches (the hot path)."""
    if not msg or len(msg) > 50_000:
        # Cap bounds a pathologically huge exception payload; skipping
        # the regex on extreme messages is safe because any embedded
        # secret is already logged and the damage is done.
        return msg
    out = msg
    for pat, repl in _MESSAGE_REDACT_PATTERNS:
        out = pat.sub(repl, out)
    return out


class StructuredFormatter(logging.Formatter):
    """Formats log records as JSON lines for BetterStack ingestion."""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": SERVICE_NAME,
            "environment": ENVIRONMENT,
            "logger": record.name,
            # Content-level regex pass catches bearer-prefix tokens,
            # key=value secrets in URLs, and basic-auth embedded creds
            # that survive the per-field key-based scrub above.
            "message": _redact_message(record.getMessage()),
        }

        # Attach any extra=... fields passed to the logger call
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_"):
                continue
            log_data[key] = _scrub_value(key, value)

        # Exception info
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            log_data["stack"] = self.formatStack(record.stack_info)

        # Request context (if set by middleware)
        req_id = get_request_id()
        if req_id is not None:
            log_data["request_id"] = req_id
        uid = get_user_id()
        if uid is not None:
            log_data["user_id"] = uid

        log_data["version"] = APP_VERSION

        try:
            return json.dumps(log_data, default=str)
        except (TypeError, ValueError):
            # Last-ditch: drop non-serialisable fields and retry.
            safe = {k: str(v) for k, v in log_data.items()}
            return json.dumps(safe, default=str)


class SecurityLogFilter(logging.Filter):
    """Only passes records whose logger is 'security' or a child."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name == "security" or record.name.startswith("security.")


# ── Ring buffer for admin panel "live tail" ────────────────────────────────

class InMemoryRingBuffer(logging.Handler):
    """
    Keeps the last N structured log records in memory so the admin panel
    can show a live tail without re-reading a file on every poll.

    Records are stored as dicts (the parsed JSON) — each call to emit() parses
    the formatter output so search/filter in the admin panel is fast.
    """

    # Loggers whose records are useless to admins and routinely emit at
    # ERROR level (asyncio fires "Task was destroyed but it is pending!"
    # from its own GC during shutdown / test teardown). Excluding them at
    # the ring-buffer level keeps the /admin/logs/errors panel actionable
    # and stops flaky cross-test leakage in the test suite.
    _EXCLUDED_LOGGER_PREFIXES = ("asyncio",)

    def __init__(self, capacity: int = 500):
        super().__init__()
        self.capacity = capacity
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        # Drop records from internal Python plumbing — see EXCLUDED above.
        logger_name = record.name or ""
        for excluded in self._EXCLUDED_LOGGER_PREFIXES:
            if logger_name == excluded or logger_name.startswith(excluded + "."):
                return
        try:
            formatted = self.format(record)
            try:
                parsed = json.loads(formatted)
            except (ValueError, TypeError):
                parsed = {"message": formatted, "level": record.levelname, "timestamp": datetime.now(timezone.utc).isoformat()}
            with self._lock:
                self._records.append(parsed)
                if len(self._records) > self.capacity:
                    # Trim in-place to the most-recent capacity entries.
                    self._records = self._records[-self.capacity:]
        except Exception:  # pragma: no cover — logging handlers must never crash
            self.handleError(record)

    def snapshot(self, *, level: Optional[str] = None,
                 service: Optional[str] = None,
                 contains: Optional[str] = None,
                 limit: int = 50) -> list[dict[str, Any]]:
        """Return the most-recent matching records (newest last)."""
        with self._lock:
            items = list(self._records)
        level_order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        if level:
            min_level = level_order.get(level.upper(), 0)
            items = [r for r in items if level_order.get(r.get("level", "INFO"), 0) >= min_level]
        if service and service != "all":
            items = [r for r in items if r.get("service") == service]
        if contains:
            needle = contains.lower()
            items = [r for r in items
                     if needle in json.dumps(r, default=str).lower()]
        return items[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def __len__(self) -> int:  # pragma: no cover — diagnostic only
        with self._lock:
            return len(self._records)


# Global ring buffer — owned by this module so handlers can be reconfigured
# without losing the in-memory log history.
ring_buffer = InMemoryRingBuffer(capacity=int(os.getenv("LOG_RING_CAPACITY", "500")))


# ── Main configuration entry point ─────────────────────────────────────────

_CONFIGURED = False


def configure_logging(
    *,
    base_dir: Optional[Path] = None,
    force: bool = False,
) -> None:
    """
    Configure logging for the current service.

    Idempotent — repeat calls are a no-op unless force=True. Safe to call
    multiple times from different entry points (e.g. the gateway startup
    and test harness).
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    if base_dir is None:
        base_dir = Path(__file__).parent

    log_dir = base_dir / "logs"
    log_dir.mkdir(exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    # Remove any pre-existing handlers installed by third parties or
    # previous logging.basicConfig() calls so we start from a known state.
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)

    formatter = StructuredFormatter()

    # 1. Console handler — streams structured JSON to stdout so `docker logs`
    #    shows the same payload that BetterStack ingests.
    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    root_logger.addHandler(console)

    # 2. Rotating file handler — per-service log file, 50MB x 5 rotations.
    file_path = log_dir / f"{SERVICE_NAME}.log"
    file_handler = logging.handlers.RotatingFileHandler(
        str(file_path),
        maxBytes=50 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # 3. Security log — only records from the "security" logger land here.
    security_file = log_dir / "security.log"
    security_handler = logging.handlers.RotatingFileHandler(
        str(security_file),
        maxBytes=20 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    security_handler.setFormatter(formatter)
    security_handler.addFilter(SecurityLogFilter())
    root_logger.addHandler(security_handler)

    # 4. Ring buffer — always attached so the admin panel can tail logs
    #    even if BetterStack is not configured.
    ring_buffer.setFormatter(formatter)
    root_logger.addHandler(ring_buffer)

    # 5. BetterStack Logtail handler — only if a matching token is set and
    #    the logtail package is importable.
    token_key = f"LOGTAIL_TOKEN_{SERVICE_NAME.upper()}"
    logtail_token = os.getenv(token_key, "").strip()
    if logtail_token and LOGTAIL_AVAILABLE:
        try:
            logtail_handler = LogtailHandler(source_token=logtail_token)
            logtail_handler.setFormatter(formatter)
            root_logger.addHandler(logtail_handler)
        except Exception as exc:  # pragma: no cover — network/config issues
            logging.getLogger("logging_config").warning(
                "BetterStack Logtail handler failed to initialise: %s", exc
            )

    # Suppress noisy third-party loggers — they still get emitted, just at
    # a higher minimum level so they don't swamp the ring buffer / BetterStack.
    for noisy in ("uvicorn.access", "httpx", "httpcore", "playwright", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger with the given name. Does not auto-configure."""
    return logging.getLogger(name)


def is_logtail_configured() -> bool:
    """True if a BetterStack token is set for the current SERVICE_NAME."""
    token_key = f"LOGTAIL_TOKEN_{SERVICE_NAME.upper()}"
    return bool(os.getenv(token_key, "").strip()) and LOGTAIL_AVAILABLE


def reset_for_tests() -> None:
    """Reset module state so unit tests can reconfigure from scratch."""
    global _CONFIGURED
    _CONFIGURED = False
    ring_buffer.clear()
    clear_request_context()

"""
Security event logging — dedicated logger for CSRF failures, rate limit hits,
and suspicious activity.

Writes structured JSON to both the main log and a dedicated security log file
at logs/security.log (configured in configure_security_logging()).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import Request

security_logger = logging.getLogger("security")


def configure_security_logging(base_dir: Optional[Path] = None) -> None:
    """
    Set up the security logger to write to logs/security.log.
    Call once at app startup.
    """
    if base_dir is None:
        base_dir = Path(__file__).parent.parent

    log_dir = base_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "security.log"

    # Avoid adding duplicate handlers on reload
    if not security_logger.handlers:
        security_logger.setLevel(logging.WARNING)

        # File handler — JSON lines
        fh = logging.FileHandler(str(log_file))
        fh.setLevel(logging.WARNING)
        fh.setFormatter(logging.Formatter("%(message)s"))
        security_logger.addHandler(fh)

        # Also log to console for visibility
        sh = logging.StreamHandler()
        sh.setLevel(logging.WARNING)
        sh.setFormatter(logging.Formatter("%(asctime)s [SECURITY] %(message)s"))
        security_logger.addHandler(sh)


def _get_ip(request: Optional[Request] = None, ip: Optional[str] = None) -> str:
    """Extract IP via the canonical ``server._get_client_ip``.

    Audit MED FIX (audit_security_dir.md cross-cutting): the three
    helpers (``audit.py``, ``logger.py``, ``rate_limiter.py``) had
    drifted. The previous implementation here trusted ``cf-connecting-ip``
    unconditionally — so an attacker reaching the origin off-tunnel could
    forge any IP into the security log. ``server._get_client_ip`` only
    honours the header when the immediate peer is in
    ``_TRUSTED_PROXY_HOSTS``, which closes that hole. Deferred import
    keeps the security package importable from ``server`` itself.
    """
    if ip:
        return ip
    if request is None:
        return "unknown"
    try:
        from server import _get_client_ip as _server_get_client_ip
        return _server_get_client_ip(request)
    except Exception:
        # Server module not importable in this context (e.g. ad-hoc test
        # harness loading only the logger). Preserve the pre-fix fallback
        # so we never raise from the security-log path.
        cf_ip = request.headers.get("cf-connecting-ip")
        if cf_ip:
            return cf_ip.strip()
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else "unknown"


def log_csrf_failure(
    request: Optional[Request] = None,
    user_id: Optional[int] = None,
    reason: str = "invalid",
    ip: Optional[str] = None,
) -> None:
    """Log a CSRF validation failure."""
    security_logger.warning(json.dumps({
        "event": "csrf_failure",
        "reason": reason,
        "ip": _get_ip(request, ip),
        "path": request.url.path if request else "",
        "method": request.method if request else "",
        "user_id": user_id,
        "timestamp": int(time.time()),
    }))


def log_rate_limit_hit(
    key: str = "",
    endpoint: str = "",
    ip: str = "unknown",
    user_id: Optional[int] = None,
) -> None:
    """Log a rate limit hit."""
    security_logger.warning(json.dumps({
        "event": "rate_limit_hit",
        "key": key,
        "endpoint": endpoint,
        "ip": ip,
        "user_id": user_id,
        "timestamp": int(time.time()),
    }))


def log_suspicious_activity(
    request: Optional[Request] = None,
    reason: str = "",
    user_id: Optional[int] = None,
    ip: Optional[str] = None,
) -> None:
    """Log suspicious activity that warrants investigation."""
    security_logger.error(json.dumps({
        "event": "suspicious_activity",
        "reason": reason,
        "ip": _get_ip(request, ip),
        "path": request.url.path if request else "",
        "user_agent": request.headers.get("user-agent", "") if request else "",
        "user_id": user_id,
        "timestamp": int(time.time()),
    }))


def log_auth_event(
    event_type: str,
    ip: str = "unknown",
    user_id: Optional[int] = None,
    email: Optional[str] = None,
    detail: str = "",
) -> None:
    """Log authentication events (login, logout, lockout)."""
    security_logger.warning(json.dumps({
        "event": f"auth_{event_type}",
        "ip": ip,
        "user_id": user_id,
        "email": email,
        "detail": detail,
        "timestamp": int(time.time()),
    }))

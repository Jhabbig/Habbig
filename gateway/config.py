"""Config surface + startup validation.

Imported early in ``server.py`` so a misconfigured production server
fails loudly *before* any request lands rather than trickling a
cryptic runtime error deep in a handler. Dev mode (``PRODUCTION`` unset
or ``0``) is forgiving — we warn but keep booting, because the happy
path for local dev is often "just enough env to exercise one feature".

Usage:
    import config
    config.validate_config()   # call early in lifespan startup

If any REQUIRED var is missing or misformatted in PRODUCTION mode the
process exits with code 2. Other codebases use codes 1/3 for unrelated
things, so 2 is a unique "config error" signal for the init system.

The actual env-var reads are still done lazily at their point of use —
this module only VALIDATES presence + shape, it does not cache values.
Lazy reads keep config.py from becoming a god-module and let tests
monkey-patch individual vars without touching this file.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Callable, Optional


log = logging.getLogger("config")


# ── Shape validators ────────────────────────────────────────────────


def _starts_with(prefix: str) -> Callable[[str], bool]:
    return lambda v: v.startswith(prefix)


def _min_length(n: int) -> Callable[[str], bool]:
    return lambda v: len(v) >= n


def _any_nonempty(v: str) -> bool:
    return bool(v and v.strip())


def _is_float(v: str) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _in(*allowed: str) -> Callable[[str], bool]:
    allowed_set = set(allowed)
    return lambda v: v in allowed_set


# ── Spec ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VarSpec:
    """One row of the config contract.

    ``name``        canonical env var name.
    ``validator``   callable taking the value string, returns True if ok.
    ``description`` short sentence for error messages + /admin/config.
    ``example``     hint shown when a required var is missing. Never a
                    real secret — just a shape (e.g. "sk_test_...").
    """
    name: str
    validator: Callable[[str], bool]
    description: str
    example: str = ""


# REQUIRED in production. Without any of these the gateway refuses
# to start when PRODUCTION=1. In dev a missing entry drops to a
# warning — most of these have safe defaults when unset (e.g. the
# app falls back to an in-memory signer).
REQUIRED_VARS: tuple[VarSpec, ...] = (
    VarSpec(
        "SITE_ACCESS_TOKEN",
        _min_length(16),
        "Gate password (pre-release access). 16+ chars of token_urlsafe entropy.",
        example="python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"",
    ),
    VarSpec(
        "CREDENTIALS_ENCRYPTION_KEY",
        _min_length(32),
        "Fernet key for encrypting stored Polymarket/Kalshi credentials.",
        example="python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"",
    ),
    VarSpec(
        "GATEWAY_COOKIE_SECRET",
        _min_length(32),
        "HMAC key for signing session + CSRF cookies.",
        example="python3 -c \"import secrets; print(secrets.token_urlsafe(48))\"",
    ),
)

# REQUIRED when the matching feature is enabled. We check these
# conditionally — e.g. STRIPE_SECRET_KEY is only required if ANY
# STRIPE_PRICE_ID_* is set, and ANTHROPIC_API_KEY only when the
# Claude kill switch is off.
CONDITIONAL_VARS: tuple[tuple[str, VarSpec, str], ...] = (
    # (condition var, spec required when condition truthy, human reason)
    (
        "STRIPE_PRICE_ID_TRADERS_MONTHLY",
        VarSpec(
            "STRIPE_SECRET_KEY",
            _starts_with("sk_"),
            "Stripe API key (sk_test_* in dev, sk_live_* in prod).",
            example="sk_test_51Abc...",
        ),
        "Stripe price IDs configured — secret key required to call Stripe.",
    ),
    (
        "STRIPE_PRICE_ID_TRADERS_MONTHLY",
        VarSpec(
            "STRIPE_WEBHOOK_SECRET",
            _starts_with("whsec_"),
            "Stripe webhook signing secret — used to verify event payloads.",
            example="whsec_1a2b3c...",
        ),
        "Stripe price IDs configured — webhook secret required to verify events.",
    ),
)

# OPTIONAL — validated only if present. Catches common footguns like
# a sample-rate typed as a non-float.
OPTIONAL_SHAPES: tuple[VarSpec, ...] = (
    VarSpec(
        "SENTRY_TRACES_SAMPLE_RATE",
        lambda v: _is_float(v) and 0.0 <= float(v) <= 1.0,
        "Sentry trace sample rate, 0.0–1.0.",
    ),
    VarSpec(
        "SENTRY_PROFILES_SAMPLE_RATE",
        lambda v: _is_float(v) and 0.0 <= float(v) <= 1.0,
        "Sentry profile sample rate, 0.0–1.0.",
    ),
    VarSpec(
        "LOG_LEVEL",
        _in("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        "Python logging level.",
    ),
    VarSpec(
        "LOG_RING_CAPACITY",
        lambda v: v.isdigit() and int(v) > 0,
        "In-memory log ring size (positive integer).",
    ),
    VarSpec(
        "GLOBAL_RATE_LIMIT_PER_MIN",
        lambda v: v.isdigit() and int(v) > 0,
        "Global per-IP requests/minute limit (positive integer).",
    ),
    VarSpec(
        "CACHE_ENABLED",
        _in("true", "false", "1", "0"),
        "Market-data in-memory cache (true/false).",
    ),
    VarSpec(
        "RATE_LIMIT_ENABLED",
        _in("true", "false", "1", "0"),
        "Global rate limiter (true/false). Never disable in production.",
    ),
    VarSpec(
        "CSRF_ENABLED",
        _in("true", "false", "1", "0"),
        "CSRF middleware (true/false). Never disable in production.",
    ),
    VarSpec(
        "ANTHROPIC_API_KEY",
        _starts_with("sk-ant-"),
        "Anthropic API key — required when Claude features are live.",
    ),
)


# ── Runtime ──────────────────────────────────────────────────────────


def is_production() -> bool:
    return os.environ.get("PRODUCTION", "0").strip().lower() in ("1", "true", "yes")


def _fmt_error(spec: VarSpec, val: Optional[str]) -> str:
    if val is None or val == "":
        hint = f" (e.g. {spec.example})" if spec.example else ""
        return f"{spec.name}: missing{hint} — {spec.description}"
    return f"{spec.name}: present but failed validation — {spec.description}"


def validate_config() -> list[str]:
    """Walk the spec, return human-readable error strings.

    In production: any error → ``sys.exit(2)`` after printing the full
    list. In dev (PRODUCTION unset/0): error list is returned so the
    caller can log it but keep booting.

    Returns the list of errors so tests can assert on exact shapes.
    """
    errors: list[str] = []

    for spec in REQUIRED_VARS:
        val = os.environ.get(spec.name)
        if not val or not spec.validator(val):
            errors.append(_fmt_error(spec, val))

    # Conditional vars: a "trigger" env var tells us whether the
    # dependent one is truly required.
    for trigger_name, spec, reason in CONDITIONAL_VARS:
        trigger = os.environ.get(trigger_name)
        if not trigger:
            continue
        val = os.environ.get(spec.name)
        if not val or not spec.validator(val):
            errors.append(f"{_fmt_error(spec, val)} [trigger: {trigger_name} is set — {reason}]")

    for spec in OPTIONAL_SHAPES:
        val = os.environ.get(spec.name)
        if val is None or val == "":
            continue  # optional → fine when absent
        if not spec.validator(val):
            errors.append(_fmt_error(spec, val))

    if errors:
        prefix = "[CONFIG ERROR]" if is_production() else "[CONFIG WARNING]"
        for e in errors:
            print(f"{prefix} {e}", file=sys.stderr)
        if is_production():
            print(
                "\nRefusing to start in PRODUCTION mode with config errors.\n"
                "Fix the above and re-run. See SECRETS.md for the value format\n"
                "of each secret, and .env.example for the full variable set.",
                file=sys.stderr,
            )
            sys.exit(2)

    return errors


# Convenience accessors — the rest of the codebase reads these directly
# off os.environ today; centralising here lets future code pull a single
# source of truth if we move to a typed settings object.


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read an env var with an optional default. Present only so callers
    can ``from config import env`` when they want to document the read
    is meant to be tracked by the config-hygiene pass."""
    return os.environ.get(name, default)

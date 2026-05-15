"""Job registry — maps a string name to a coroutine function.

Jobs register themselves via the @register_job decorator at import time.
The backend looks up functions here when it dequeues a job.

Kept deliberately small so the ARQ and in-process backends can both drive
it with identical semantics.

HMAC helpers (HIGH-21 — Fix D)
------------------------------
``compute_job_hmac`` / ``verify_job_hmac`` tag enqueued rows so the
retry path can refuse to re-dispatch arbitrary rows planted by anything
other than ``enqueue_job``. See migrations/192_background_jobs_hmac.py
and jobs/backend.py::retry_job.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
from typing import Any, Awaitable, Callable


_Fn = Callable[..., Awaitable[Any]]
job_registry: dict[str, _Fn] = {}
cron_jobs: list[dict] = []


# Fix C — modules trusted to register background jobs. Anything outside
# this prefix list is rejected at decoration time so an attacker who
# plants code somewhere else in the gateway cannot wire up an arbitrary
# coroutine under a familiar job name and have it execute through
# ``enqueue_job`` -> ``retry_job`` -> cron.
_TRUSTED_JOB_MODULE_PREFIXES = (
    "gateway.jobs.",
    "gateway.scheduler.",
    "jobs.",
    "scheduler.",
    "tests.",
)


def _caller_is_trusted_job_module(fn: _Fn) -> bool:
    """Best-effort check that *fn* was defined in a module we trust to
    register jobs. Returns True on unknown / missing modules so we don't
    break callers in odd execution contexts (REPL, ad-hoc scripts).
    """
    mod = getattr(fn, "__module__", None) or ""
    if not mod:
        return True
    if mod in {"__main__", "builtins"}:
        return True
    if mod in {"jobs", "scheduler", "gateway.jobs", "gateway.scheduler"}:
        return True
    for prefix in _TRUSTED_JOB_MODULE_PREFIXES:
        if mod.startswith(prefix):
            return True
    return False


def register_job(name: str) -> Callable[[_Fn], _Fn]:
    """Decorator. Registers a coroutine under *name* in the global registry.

    Two guards:

    1. Duplicate registration is fatal. A second ``@register_job("x")``
       under the same name raises ``ValueError`` rather than silently
       overwriting.

    2. Registration is restricted to known job/scheduler modules.
       See ``_TRUSTED_JOB_MODULE_PREFIXES``. Stops a stored-RCE pivot
       from registering a fresh coroutine name and using ``retry_job``
       to dispatch it.
    """
    def deco(fn: _Fn) -> _Fn:
        if name in job_registry:
            raise ValueError(f"job already registered: {name}")
        if not _caller_is_trusted_job_module(fn):
            mod = getattr(fn, "__module__", "<unknown>")
            raise ValueError(
                f"register_job refusing untrusted module {mod!r}: "
                f"job name {name!r}"
            )
        job_registry[name] = fn
        return fn
    return deco


def register_cron(
    name: str,
    *,
    minute: int | None = None,
    hour: int | None = None,
    weekday: int | None = None,
    day: int | None = None,
) -> None:
    """Register a cron schedule for a previously-registered job.

    Semantics match arq.cron. `weekday=0` is Monday. `None` means any.
    """
    cron_jobs.append({
        "name": name,
        "minute": minute,
        "hour": hour,
        "weekday": weekday,
        "day": day,
    })


# -- HMAC helpers (HIGH-21 — Fix D) ----------------------------------------

_log = logging.getLogger("jobs.registry.hmac")
_PROCESS_FALLBACK_HMAC: bytes | None = None


def _job_hmac_secret() -> bytes:
    """Return the HMAC signing secret as bytes.

    Reuse the gateway's existing shared-secret env vars
    (``GATEWAY_SSO_SECRET`` primary, ``EMBED_SIGNING_SECRET`` fallback)
    so operators only manage one secret.
    """
    global _PROCESS_FALLBACK_HMAC
    env = (
        os.environ.get("GATEWAY_SSO_SECRET")
        or os.environ.get("EMBED_SIGNING_SECRET")
    )
    if env:
        return env.encode("utf-8")
    if _PROCESS_FALLBACK_HMAC is None:
        _log.warning(
            "GATEWAY_SSO_SECRET and EMBED_SIGNING_SECRET both unset - "
            "background_jobs HMAC uses an in-memory fallback. Set "
            "GATEWAY_SSO_SECRET in production so retry HMACs survive restart."
        )
        _PROCESS_FALLBACK_HMAC = secrets.token_urlsafe(32).encode("utf-8")
    return _PROCESS_FALLBACK_HMAC


def _canonical_payload(payload: Any) -> str:
    """Canonical JSON for HMAC input.

    sort_keys + separators give us a deterministic byte stream so the
    same payload always produces the same HMAC. ``default=str`` mirrors
    what ``_audit_insert`` uses to serialise payloads at enqueue time.
    """
    try:
        return json.dumps(
            payload or {},
            default=str,
            sort_keys=True,
            separators=(",", ":"),
        )
    except Exception:
        return repr(payload)


def compute_job_hmac(name: str, payload: Any) -> str:
    """HMAC-SHA256 hex over ``name`` + canonical(payload)."""
    canonical = _canonical_payload(payload)
    msg = f"{name}\n{canonical}".encode("utf-8")
    return hmac.new(_job_hmac_secret(), msg, hashlib.sha256).hexdigest()


def verify_job_hmac(name: str, payload: Any, sig: str | None) -> bool:
    """Constant-time HMAC check. Empty/missing sig -> False (reject)."""
    if not sig:
        return False
    expected = compute_job_hmac(name, payload)
    return hmac.compare_digest(expected, sig)

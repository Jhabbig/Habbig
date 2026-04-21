"""Web Push: VAPID key management, subscription storage, sending.

Layers:
  - VAPID keypair: ECDSA P-256 via ``cryptography``. The private key is read
    from ``PUSH_VAPID_PRIVATE_KEY`` (base64url-encoded raw 32-byte scalar);
    if unset, a keypair is generated on first call and persisted to disk
    at ``~/.narve/vapid.key`` so subsequent boots use the same one. The
    server op is expected to promote that file into the env for production.
  - Subscription storage: ``push_subscriptions`` table (see migration 034).
  - Sender: ``send_to_user(user_id, payload)`` loops over the user's rows,
    calls pywebpush, and deletes rows the push service reports as gone.

The whole module is safe to import even if ``pywebpush`` is missing — the
sender raises ``PushNotAvailable`` then, which the HTTP handlers surface as
a clean 503. That way ``import push`` never breaks server startup.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("gateway.push")


class PushNotAvailable(RuntimeError):
    """Raised when pywebpush/cryptography can't be loaded or keys are absent."""


# ── VAPID keypair ────────────────────────────────────────────────────────

_VAPID_KEY_FILE = Path.home() / ".narve" / "vapid.key"
_VAPID_SUBJECT = os.environ.get("PUSH_VAPID_SUBJECT", "mailto:hello@narve.ai")

_cached_private_pem: Optional[str] = None
_cached_public_b64url: Optional[str] = None


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _ensure_keypair() -> tuple[str, str]:
    """Return (private_pem, public_base64url). Generate + persist if missing.

    We store the private key as PEM on disk so a human can inspect it;
    we return the raw uncompressed public point base64url-encoded
    because that's what the browser Push API expects in
    ``applicationServerKey``.
    """
    global _cached_private_pem, _cached_public_b64url
    if _cached_private_pem and _cached_public_b64url:
        return _cached_private_pem, _cached_public_b64url

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError as exc:  # pragma: no cover
        raise PushNotAvailable("cryptography not installed") from exc

    private_pem_env = os.environ.get("PUSH_VAPID_PRIVATE_KEY_PEM")
    if private_pem_env:
        private_pem = private_pem_env
        priv = serialization.load_pem_private_key(
            private_pem.encode(), password=None
        )
    else:
        # In production we refuse to generate or read an on-disk keypair —
        # the private key must come from the environment (secrets manager,
        # systemd EnvironmentFile, etc.) so it's not sitting in $HOME on the
        # server where any local process can read it. The dev fallback is
        # only enabled when PRODUCTION is unset/falsy, matching the same
        # convention used elsewhere in the gateway (server.py, auth/cookies.py).
        _is_production = os.environ.get("PRODUCTION", "").lower() in (
            "1", "true", "yes", "on",
        )
        if _is_production:
            raise RuntimeError(
                "PUSH_VAPID_PRIVATE_KEY_PEM must be set in production; "
                "the filesystem fallback at ~/.narve/vapid.key is "
                "disabled when PRODUCTION=1."
            )
        if _VAPID_KEY_FILE.exists():
            private_pem = _VAPID_KEY_FILE.read_text()
            priv = serialization.load_pem_private_key(
                private_pem.encode(), password=None
            )
        else:
            priv = ec.generate_private_key(ec.SECP256R1())
            private_pem = priv.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode()
            try:
                _VAPID_KEY_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                # Create the file first, then tighten perms before writing
                # the secret — this closes the tiny window where a world-
                # readable file could briefly contain the PEM on a umask
                # 022 system.
                _VAPID_KEY_FILE.touch(mode=0o600, exist_ok=True)
                os.chmod(_VAPID_KEY_FILE, 0o600)
                _VAPID_KEY_FILE.write_text(private_pem)
                os.chmod(_VAPID_KEY_FILE, 0o600)
                log.info(
                    "push: generated VAPID keypair, persisted to %s (dev only)",
                    _VAPID_KEY_FILE,
                )
            except OSError as exc:
                log.warning(
                    "push: could not persist VAPID key to %s: %s",
                    _VAPID_KEY_FILE, exc,
                )

    pub = priv.public_key()
    public_point = pub.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_b64url = _b64url_encode(public_point)

    _cached_private_pem = private_pem
    _cached_public_b64url = public_b64url
    return private_pem, public_b64url


def vapid_public_key() -> str:
    """Base64url-encoded uncompressed P-256 public point. Safe on startup."""
    _, public = _ensure_keypair()
    return public


# ── Subscription storage ─────────────────────────────────────────────────

def save_subscription(
    user_id: int,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: Optional[str] = None,
) -> None:
    """Idempotent insert/update keyed on the push endpoint URL.

    Re-subscribing from the same browser yields the same endpoint, so we
    overwrite p256dh/auth (they rotate occasionally) and bind it to the
    current user — preventing a browser that's been handed off between
    accounts from firing notifications to the wrong user.
    """
    from db import conn

    with conn() as c:
        c.execute(
            """
            INSERT INTO push_subscriptions
                (user_id, endpoint, p256dh, auth, user_agent, created_at, last_used_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                user_id    = excluded.user_id,
                p256dh     = excluded.p256dh,
                auth       = excluded.auth,
                user_agent = excluded.user_agent,
                failure_count = 0,
                last_error = NULL
            """,
            (
                user_id, endpoint, p256dh, auth, user_agent,
                int(time.time()), int(time.time()),
            ),
        )


def delete_subscription(user_id: int, endpoint: str) -> int:
    from db import conn
    with conn() as c:
        cur = c.execute(
            "DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?",
            (user_id, endpoint),
        )
        return cur.rowcount


def list_subscriptions(user_id: int) -> list[sqlite3.Row]:
    from db import conn
    with conn() as c:
        return list(c.execute(
            "SELECT * FROM push_subscriptions WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall())


def _delete_by_endpoint(endpoint: str) -> None:
    from db import conn
    with conn() as c:
        c.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))


# ── Sender ───────────────────────────────────────────────────────────────

def _send_one(sub_row: sqlite3.Row, payload: dict) -> tuple[bool, str]:
    """Try to send a single push. Returns (ok, error_message)."""
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        raise PushNotAvailable("pywebpush not installed")

    private_pem, _ = _ensure_keypair()
    try:
        webpush(
            subscription_info={
                "endpoint": sub_row["endpoint"],
                "keys": {"p256dh": sub_row["p256dh"], "auth": sub_row["auth"]},
            },
            data=json.dumps(payload),
            vapid_private_key=private_pem,
            vapid_claims={"sub": _VAPID_SUBJECT},
            ttl=86400,
        )
        return True, ""
    except WebPushException as exc:  # pragma: no cover — network dependent
        # 404/410 from the push service = subscription is gone. Delete it.
        status = getattr(exc.response, "status_code", None) if exc.response else None
        if status in (404, 410):
            _delete_by_endpoint(sub_row["endpoint"])
            return False, f"expired ({status})"
        return False, f"{status}: {exc}"


def send_to_user(
    user_id: int,
    *,
    title: str,
    body: str = "",
    url: str = "/",
    tag: Optional[str] = None,
    icon: Optional[str] = None,
    data: Optional[dict] = None,
) -> dict:
    """Fire-and-forget delivery to every subscription the user has.

    Returns a summary dict ``{sent, failed, expired}`` for the caller to
    log. Raises ``PushNotAvailable`` if the dependency stack isn't ready —
    callers in background jobs should catch and log, not crash the job.
    """
    from db import conn

    payload = {
        "title": title,
        "body": body,
        "url": url,
        "tag": tag or "narve-general",
        "icon": icon or "/_gateway_static/img/icon-192.png",
        "data": data or {},
    }

    subs = list_subscriptions(user_id)
    if not subs:
        return {"sent": 0, "failed": 0, "expired": 0}

    sent = failed = expired = 0
    for sub in subs:
        ok, err = _send_one(sub, payload)
        if ok:
            sent += 1
            with conn() as c:
                c.execute(
                    "UPDATE push_subscriptions SET last_used_at = ?, failure_count = 0, last_error = NULL WHERE id = ?",
                    (int(time.time()), sub["id"]),
                )
        elif "expired" in err:
            expired += 1
        else:
            failed += 1
            with conn() as c:
                c.execute(
                    "UPDATE push_subscriptions SET failure_count = failure_count + 1, last_error = ? WHERE id = ?",
                    (err[:500], sub["id"]),
                )
    return {"sent": sent, "failed": failed, "expired": expired}


def send_to_users(user_ids: Iterable[int], **kwargs: Any) -> dict:
    totals = {"sent": 0, "failed": 0, "expired": 0}
    for uid in user_ids:
        r = send_to_user(uid, **kwargs)
        for k in totals:
            totals[k] += r[k]
    return totals

#!/usr/bin/env python3
"""
Web Push notifications (VAPID, payload-less).

Why payload-less? Web Push with an encrypted payload requires aes128gcm
content encoding (HKDF + AES-GCM with ECDH-derived keys). That's
implementable with `cryptography` but error-prone. The cleaner pattern
that all modern PWAs use:

  1. Server sends a payload-less push to wake the user's browser.
  2. Service worker `push` handler fetches the latest pending notification
     for this user from /api/notifications/pending.
  3. Service worker calls `registration.showNotification(...)`.

The browser's push service handles the cross-origin delivery; we only need
to:
  - Identify ourselves with a VAPID-signed JWT in the Authorization header.
  - POST an empty body to the subscription's endpoint URL.

VAPID key (P-256 EC) is generated once on first import and cached in
`.vapid_key` next to the existing `.secret_key`.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

import database as db

log = logging.getLogger("crypto.push")

VAPID_KEY_PATH = Path(__file__).parent / ".vapid_key"
VAPID_SUBJECT = "mailto:admin@narve.ai"   # required by VAPID spec
PUSH_TTL_SECONDS = 60                      # browsers ignore old pushes anyway


# ─── VAPID key management ───────────────────────────────────────────────────

def _b64url_nopad(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _ensure_vapid_key():
    """Generate or load the VAPID P-256 private key. Returns the
    EllipticCurvePrivateKey instance."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if VAPID_KEY_PATH.exists():
        pem = VAPID_KEY_PATH.read_bytes()
        return serialization.load_pem_private_key(pem, password=None)
    # Generate fresh key. Subsequent restarts reuse it — never rotate without
    # also nuking all stored subscriptions (they'd fail to authenticate).
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    VAPID_KEY_PATH.write_bytes(pem)
    VAPID_KEY_PATH.chmod(0o600)
    log.info("generated new VAPID key at %s", VAPID_KEY_PATH)
    return key


def get_vapid_public_key_b64() -> str:
    """Return the uncompressed public point, base64url-encoded.
    This is the `applicationServerKey` the browser uses in pushManager.subscribe."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = _ensure_vapid_key()
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    # 65 bytes: 0x04 || X(32) || Y(32). The browser expects this exact form.
    return _b64url_nopad(pub)


# ─── VAPID JWT signing ──────────────────────────────────────────────────────

def _vapid_jwt(endpoint_url: str) -> str:
    """Sign a JWT proving server identity to the push service.
    Claims: aud = origin of endpoint, exp = now + 12h, sub = VAPID_SUBJECT.
    Signed with the VAPID private key (ES256)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    key = _ensure_vapid_key()
    origin = urlparse(endpoint_url)
    aud = f"{origin.scheme}://{origin.netloc}"

    header = {"alg": "ES256", "typ": "JWT"}
    claims = {
        "aud": aud,
        "exp": int(time.time()) + 12 * 3600,
        "sub": VAPID_SUBJECT,
    }

    def b64j(d: dict) -> str:
        return _b64url_nopad(json.dumps(d, separators=(",", ":")).encode())

    signing_input = f"{b64j(header)}.{b64j(claims)}".encode()
    der_sig = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing_input.decode()}.{_b64url_nopad(raw_sig)}"


# ─── Send ───────────────────────────────────────────────────────────────────

def send_to_subscription(subscription: dict, ttl: int = PUSH_TTL_SECONDS) -> dict:
    """POST a payload-less push to one subscription's endpoint.
    Returns {ok, status, body}."""
    endpoint = subscription.get("endpoint")
    if not endpoint:
        return {"ok": False, "error": "no endpoint"}
    try:
        jwt = _vapid_jwt(endpoint)
        public_key = get_vapid_public_key_b64()
        headers = {
            "Authorization": f"vapid t={jwt}, k={public_key}",
            "TTL": str(ttl),
            "Content-Length": "0",
            # Indicate that we're sending no body. Browsers accept this.
            "Urgency": "high",
        }
        r = requests.post(endpoint, headers=headers, data=b"", timeout=10)
        ok = 200 <= r.status_code < 300
        return {"ok": ok, "status": r.status_code, "body": r.text[:300]}
    except requests.RequestException as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def notify_user(user_id: str, title: str, body: str, url: str = "/long-term",
                tag: str | None = None) -> dict:
    """High-level: enqueue a notification for one user, then ping every one
    of their subscribed devices to fetch it.

    The push payload is empty — the service worker fetches the most-recent
    pending notification from /api/notifications/pending and renders it
    locally. `tag` lets newer notifications replace older ones of the same
    kind (e.g. "circuit_breaker") instead of stacking up."""
    notif_id = db.insert_pending_notification(user_id, title, body, url, tag or "")
    subs = db.get_push_subscriptions(user_id)
    if not subs:
        return {"notification_id": notif_id, "sent": 0, "results": []}
    results = []
    dead_ids = []
    for s in subs:
        r = send_to_subscription({
            "endpoint": s["endpoint"],
            "p256dh": s["p256dh"],
            "auth": s["auth"],
        })
        results.append({"sub_id": s["id"], "status": r.get("status"), "ok": r.get("ok")})
        # 404 / 410 from push service = subscription expired. Clean up.
        if r.get("status") in (404, 410):
            dead_ids.append(s["id"])
    for d in dead_ids:
        db.delete_push_subscription_by_id(d)
    return {"notification_id": notif_id, "sent": sum(1 for r in results if r["ok"]),
            "removed_expired": len(dead_ids), "results": results}


# ─── Hook helpers (called from the existing alert pipeline) ─────────────────

def notify_long_term_alert(user_id: str, ticker: str, alert_type: str,
                           message: str) -> None:
    try:
        notify_user(
            user_id,
            title=f"{ticker} · {alert_type.replace('_', ' ')}",
            body=message,
            url=f"/long-term#alerts",
            tag=f"alert-{alert_type}-{ticker}",
        )
    except Exception as e:
        log.warning("notify_long_term_alert failed: %s", e)


def notify_execution(user_id: str, ticker: str, action: str, reason: str,
                     usd_amount: float | None = None) -> None:
    if action not in ("placed", "blocked", "filled"):
        return
    amount_str = f"${usd_amount:.0f}" if usd_amount else ""
    try:
        notify_user(
            user_id,
            title=f"DCA {action} · {ticker} {amount_str}",
            body=reason,
            url="/long-term#execution",
            tag=f"exec-{ticker}",
        )
    except Exception as e:
        log.warning("notify_execution failed: %s", e)

"""One-click unsubscribe — signed tokens, no login required.

Each unsubscribe link carries a token that maps to a row in
`email_unsubscribes`. Hitting `/unsubscribe?token=...` records the
unsubscribe and shows a confirmation page. Resubscribe works via the
same endpoint.

AUDIT 2026-05-15 — ``_secret()`` previously fell back to the literal
string ``"narve-unsubscribe"`` when ``GATEWAY_COOKIE_SECRET`` was
unset. That fallback is a known constant any attacker can read from
this file, so it offered zero forgery protection in environments
that forgot to set the secret. The fallback is now removed in
production (``PRODUCTION=1`` or ``IS_PRODUCTION=1``); the dev-only
fallback is still permitted so the test suite + local runs work
without secrets in env.
"""

from __future__ import annotations

import hmac
import hashlib
import logging
import os
import secrets
import time
from typing import Optional

import db


log = logging.getLogger("email_system.unsubscribe")


# Dev fallback. NEVER used in production — the prod check raises.
_DEV_FALLBACK_SECRET = "dev-only-unsubscribe-secret-not-for-prod"


def _is_production() -> bool:
    return bool(
        os.environ.get("PRODUCTION") or os.environ.get("IS_PRODUCTION"),
    )


def _secret() -> bytes:
    """Return the HMAC key. Raises in production if not configured.

    The previous implementation silently fell back to the constant
    string ``"narve-unsubscribe"``. That defeats the whole point of
    signing — an attacker who reads this file can forge any
    unsubscribe token for any email. Production deploys MUST set
    ``GATEWAY_COOKIE_SECRET``; the gateway already refuses to boot
    without it (see server.py startup check), so reaching this branch
    in prod means env was tampered with mid-flight.
    """
    secret = os.environ.get("GATEWAY_COOKIE_SECRET") or ""
    if secret:
        return secret.encode()
    if _is_production():
        raise RuntimeError(
            "GATEWAY_COOKIE_SECRET is required in production — "
            "refusing to sign unsubscribe tokens with a dev-only constant."
        )
    log.warning(
        "unsubscribe: GATEWAY_COOKIE_SECRET unset, using dev-only fallback "
        "(this would refuse to sign in production)."
    )
    return _DEV_FALLBACK_SECRET.encode()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]


class UnsubscribeManager:
    """CRUD for one-click unsubscribes. Stateless — call class methods."""

    @staticmethod
    def generate_token(email: str, user_id: Optional[int], unsubscribed_from: str = "marketing") -> str:
        """Create or fetch an unsubscribe token for this email + scope."""
        with db.conn() as c:
            row = c.execute(
                "SELECT token FROM email_unsubscribes WHERE email = ? AND unsubscribed_from = ?",
                (email, unsubscribed_from),
            ).fetchone()
            if row:
                return row["token"]
            raw = secrets.token_urlsafe(24)
            token = f"{raw}.{_sign(raw + email + unsubscribed_from)}"
            c.execute(
                "INSERT INTO email_unsubscribes (user_id, email, unsubscribed_from, token, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, email, unsubscribed_from, token, int(time.time())),
            )
            return token

    @staticmethod
    def unsubscribe(token: str) -> Optional[dict]:
        """Mark an unsubscribe record as applied, returning the row as a dict."""
        if not token or "." not in token:
            return None
        raw, sig = token.rsplit(".", 1)
        with db.conn() as c:
            row = c.execute(
                "SELECT * FROM email_unsubscribes WHERE token = ?", (token,),
            ).fetchone()
            if not row:
                return None
            expected = _sign(raw + row["email"] + row["unsubscribed_from"])
            if not hmac.compare_digest(expected, sig):
                return None
            # Apply the unsubscribe to the user row.
            user_id = row["user_id"]
            if user_id:
                if row["unsubscribed_from"] == "marketing":
                    c.execute(
                        "UPDATE users SET email_marketing = 0, email_unsubscribed_at = ? WHERE id = ?",
                        (int(time.time()), user_id),
                    )
                elif row["unsubscribed_from"] == "digest":
                    c.execute(
                        "UPDATE users SET email_digest = 0, email_unsubscribed_at = ? WHERE id = ?",
                        (int(time.time()), user_id),
                    )
                elif row["unsubscribed_from"] == "all":
                    c.execute(
                        "UPDATE users SET email_marketing = 0, email_digest = 0, email_unsubscribed_at = ? WHERE id = ?",
                        (int(time.time()), user_id),
                    )
            return dict(row)

    @staticmethod
    def get_unsubscribe_url(user_id: Optional[int], email: str, scope: str = "marketing") -> str:
        token = UnsubscribeManager.generate_token(email, user_id, scope)
        base = os.environ.get("APP_URL", "https://narve.ai")
        return f"{base}/unsubscribe?token={token}&type={scope}"

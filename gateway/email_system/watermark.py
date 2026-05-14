"""Per-recipient watermarks for Pro-tier intelligence emails.

Three Pro emails carry watermarks today: the weekly signal digest, the
morning briefing, and the market-mover alert. Two surfaces ride in each
message:

  1. A *visible* 6-char hex fragment in the footer styled as a faint
     ``id:xxxxxx`` line. Small (9px) and low-contrast so it doesn't
     intrude on the design, but readable in a screenshot and survives
     copy-paste.

  2. An *invisible* zero-width-character run scattered through body
     text. The hex digits are unpacked to bits and emitted as U+200B
     (zero-width space = 0) and U+200C (zero-width non-joiner = 1) so
     a screenshot of the prose still carries the bits when the leaker
     retypes the content. The two code-points are both printable but
     invisible in every major email client and most code editors —
     they survive copy-paste verbatim.

The watermark is derived as the first 24 bits of HMAC-SHA256 over
``f"{user_id}:{email_id}"`` keyed with ``EMAIL_WATERMARK_KEY``. HMAC
(not raw SHA-256) prevents an attacker who has captured a few leaked
watermarks from extending the hash to forge new ones, and the keying
means a leaked watermark gives an outsider no way to map other
user-ids.

On send, the watermark is recorded in the ``email_watermarks`` table
(migration 175) keyed by the watermark itself, so reverse lookup is
O(1). The admin route ``GET /admin/trace-watermark?id=<wm>`` reads
this table behind the ``_require_admin_user`` guard.

Constraints (enforced by the test suite):

  * Same (user_id, email_id) input → same watermark (deterministic).
  * Different user_ids on the same email_id → different watermarks.
  * The watermark is NEVER added to the email subject — that would
    break threading and accidentally include forensic state in
    mailbox indices the leaker can grep.

If ``EMAIL_WATERMARK_KEY`` is unset, the helpers return empty strings
and ``record_watermark`` is a no-op. Templates render fine in that
case (the visible span just collapses) so dev environments don't have
to set the env var.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Optional


log = logging.getLogger("email.watermark")


# Zero-width code points used for steganographic encoding.
#   U+200B ZERO WIDTH SPACE          → bit 0
#   U+200C ZERO WIDTH NON-JOINER     → bit 1
# Both survive copy-paste in every modern client we've tested, and
# neither alters line layout. We deliberately avoid U+200D (joiner)
# because it changes shaping of adjacent emoji/Indic scripts.
_ZW_ZERO = "​"
_ZW_ONE = "‌"


def _key() -> bytes:
    return (os.environ.get("EMAIL_WATERMARK_KEY") or "").encode()


def watermark_for_user(user_id: int, email_id: str) -> str:
    """Return a 6-char hex watermark for this (user_id, email_id).

    Deterministic. Empty string if ``EMAIL_WATERMARK_KEY`` is unset so
    dev builds don't accidentally inject a fixed-fallback fingerprint.
    """
    key = _key()
    if not key:
        return ""
    msg = f"{int(user_id)}:{email_id}".encode()
    digest = hmac.new(key, msg, hashlib.sha256).hexdigest()
    return digest[:6]


def watermark_zw(watermark: str) -> str:
    """Encode a hex watermark as a run of zero-width characters.

    Each hex digit is unpacked to 4 bits and emitted big-endian. A 6-char
    watermark yields 24 zero-width chars — invisible inline, but copy-
    pasteable.
    """
    if not watermark:
        return ""
    out = []
    for ch in watermark:
        try:
            nibble = int(ch, 16)
        except ValueError:
            continue
        for shift in (3, 2, 1, 0):
            bit = (nibble >> shift) & 1
            out.append(_ZW_ONE if bit else _ZW_ZERO)
    return "".join(out)


def decode_zw(zw: str) -> str:
    """Inverse of :func:`watermark_zw` — recover the hex from a ZW run.

    Used by the admin forensic tooling when a screenshot has been OCR'd
    and the text reconstructed character-by-character.
    """
    bits = []
    for ch in zw:
        if ch == _ZW_ZERO:
            bits.append(0)
        elif ch == _ZW_ONE:
            bits.append(1)
    # Pack 4-bit nibbles back into hex.
    if not bits or len(bits) % 4:
        return ""
    out = []
    for i in range(0, len(bits), 4):
        nibble = (bits[i] << 3) | (bits[i + 1] << 2) | (bits[i + 2] << 1) | bits[i + 3]
        out.append(f"{nibble:x}")
    return "".join(out)


def record_watermark(
    user_id: int,
    email_id: str,
    watermark: str,
    template: Optional[str] = None,
) -> None:
    """Persist the watermark → user mapping for later forensic lookup.

    Best-effort; a failure here must not block the email send. The
    INSERT uses ``OR IGNORE`` so re-sends of the same (user, email_id)
    don't error — the deterministic hash means the row already exists
    and overwriting would just rewrite ``created_at`` to a confusing
    future timestamp.
    """
    if not watermark:
        return
    try:
        import db

        with db.conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO email_watermarks "
                "(watermark, user_id, email_id, template, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (watermark, int(user_id), email_id, template, int(time.time())),
            )
    except Exception as exc:  # pragma: no cover — exercised only when db is unavailable
        log.warning(
            "record_watermark failed user_id=%s email_id=%s: %s",
            user_id, email_id, exc,
        )


def trace_watermark(watermark: str) -> Optional[int]:
    """Reverse-lookup: watermark → user_id.

    Returns the user_id, or None if the watermark is unknown. The table
    is keyed on watermark so this is a primary-key index hit.
    """
    if not watermark:
        return None
    try:
        import db

        with db.conn() as c:
            row = c.execute(
                "SELECT user_id FROM email_watermarks WHERE watermark = ?",
                (watermark.strip().lower(),),
            ).fetchone()
    except Exception as exc:  # pragma: no cover
        log.warning("trace_watermark failed: %s", exc)
        return None
    return int(row["user_id"]) if row else None


def trace_watermark_detail(watermark: str) -> Optional[dict]:
    """Like :func:`trace_watermark` but returns the full row.

    Surfaces email_id + template + created_at to the admin UI so the
    incident responder can correlate the leak with a specific send.
    """
    if not watermark:
        return None
    try:
        import db

        with db.conn() as c:
            row = c.execute(
                "SELECT watermark, user_id, email_id, template, created_at "
                "FROM email_watermarks WHERE watermark = ?",
                (watermark.strip().lower(),),
            ).fetchone()
    except Exception as exc:  # pragma: no cover
        log.warning("trace_watermark_detail failed: %s", exc)
        return None
    if not row:
        return None
    return {
        "watermark": row["watermark"],
        "user_id": int(row["user_id"]),
        "email_id": row["email_id"],
        "template": row["template"],
        "created_at": int(row["created_at"]),
    }


def email_id(template: str, user_id: int, batch_ts: Optional[int] = None) -> str:
    """Build a stable per-send identifier.

    Combines template + user + timestamp bucket so the same daily
    morning briefing for the same user always hashes to the same
    watermark within a 24-hour window (re-sends from retry queues are
    idempotent), but two consecutive days' briefings get different
    watermarks.
    """
    ts = batch_ts if batch_ts is not None else int(time.time())
    # Day-resolution bucket — re-renders within a day are the "same email".
    day = ts // 86400
    return f"{template}:{int(user_id)}:{day}"


def annotate_context(
    context: dict,
    user_id: int,
    template: str,
    *,
    batch_ts: Optional[int] = None,
) -> dict:
    """Mutate `context` in place to add `watermark` + `watermark_zw` keys.

    Convenience helper for the email enqueue sites — keeps the per-send
    boilerplate to a single call. Also records the mapping in the DB so
    the admin trace endpoint can resolve it later.
    """
    eid = email_id(template, user_id, batch_ts=batch_ts)
    wm = watermark_for_user(user_id, eid)
    context["watermark"] = wm
    context["watermark_zw"] = watermark_zw(wm)
    if wm:
        record_watermark(user_id, eid, wm, template=template)
    return context

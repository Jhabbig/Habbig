"""Analytics event hardening helpers.

The actual write is still ``db.record_analytics_event`` (defined in
queries/admin.py to avoid a churny rename). This module owns the
*server-side hardening* that runs before the write:

* PII scrub — strip / redact obvious personal data from a properties
  dict before it ever touches SQLite. Frontend analytics.js should never
  send email or phone, but a malicious client (or a buggy one) might —
  defence-in-depth says we drop it server-side.
* Properties size cap — ``/api/analytics/event`` already caps the raw
  body at 4096 bytes, but we also cap the *post-parse* size of the
  properties dict at 4096 chars of key+value content so a single highly
  compressible blob can't sneak past the body cap.

These helpers return *new* sanitised values rather than mutating in
place, which keeps the server handler easy to reason about.
"""

from __future__ import annotations

import re
from typing import Any, Optional


# Keys whose values are dropped entirely on the way in. Matches the
# ``properties`` field names a curious frontend dev is most likely to
# accidentally log. Lowercased for case-insensitive match.
_PII_KEYS = frozenset({
    "email",
    "email_address",
    "mail",
    "phone",
    "phone_number",
    "mobile",
    "tel",
    "telephone",
    "ssn",
    "social_security",
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "credit_card",
    "card_number",
    "cc",
    "cvv",
})

# Loose regexes that catch obvious PII in *values*. We don't try to be
# perfect — these are belt-and-braces filters for cases where someone
# logs an email under a non-obvious key like "user_info".
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# US-style or international phone (7+ digits with optional separators).
_PHONE_RE = re.compile(r"(?:\+?\d[\s\-.]?){7,}\d")

# Hard cap on the *serialised* size of properties (sum of key + str(val)
# chars). 2048 chars is generous: a clean analytics payload is < 200.
# Intentionally smaller than the 4 KB raw-body cap in the route handler
# so this check is independently reachable — otherwise a payload whose
# *properties* are too big would already have tripped the body cap and
# we'd never see a 422 in practice.
PROPERTIES_MAX_CHARS = 2048

# Max length of any single property value after coercion to str. Stops
# a single 2 KB blob from filling the table column.
PROPERTY_VALUE_MAX = 512


def _redact_value(value: Any) -> Any:
    """Return *value* with any embedded email / phone redacted.

    Non-str values are coerced to str for the scan, then either returned
    unchanged (if no PII pattern matched) or returned as the redacted
    str. The caller decides how to coerce back.
    """
    s = value if isinstance(value, str) else str(value)
    redacted = _EMAIL_RE.sub("[redacted-email]", s)
    redacted = _PHONE_RE.sub("[redacted-phone]", redacted)
    return redacted


def scrub_properties(props: Optional[dict]) -> Optional[dict]:
    """Return a new dict with PII keys removed and PII patterns redacted.

    * Drops any top-level key in :data:`_PII_KEYS` (case-insensitive).
    * Truncates each value to :data:`PROPERTY_VALUE_MAX` chars.
    * Redacts email / phone patterns in remaining values.
    * Coerces all values to str so the SQLite column stays predictable.

    Returns ``None`` if *props* is None or empty after scrubbing.
    """
    if not props or not isinstance(props, dict):
        return None
    out: dict[str, str] = {}
    for raw_key, raw_val in props.items():
        if not isinstance(raw_key, str):
            # Drop non-string keys outright — analytics.js never sends
            # those; anyone who does is probing.
            continue
        key = raw_key[:64]
        if key.lower() in _PII_KEYS:
            # Drop the whole entry; we don't want a redacted marker
            # because that still leaks "user tried to send email".
            continue
        coerced = _redact_value(raw_val)
        if not isinstance(coerced, str):
            coerced = str(coerced)
        if len(coerced) > PROPERTY_VALUE_MAX:
            coerced = coerced[:PROPERTY_VALUE_MAX]
        out[key] = coerced
    return out or None


def properties_too_large(props: Optional[dict]) -> bool:
    """Return True if *props* exceeds :data:`PROPERTIES_MAX_CHARS`.

    Counts the sum of ``len(k) + len(str(v))`` across all entries. Run
    AFTER :func:`scrub_properties` so per-value truncation has applied.
    """
    if not props:
        return False
    total = 0
    for k, v in props.items():
        total += len(k) + len(str(v))
        if total > PROPERTIES_MAX_CHARS:
            return True
    return False

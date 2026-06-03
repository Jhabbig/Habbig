"""Find email addresses in free text — including the common obfuscations
people use when they want to be reachable but not scraped.

Returns the first plausible address or None. Conservative: rather miss an
edge case than email a malformed address and burn sender reputation.
"""

from __future__ import annotations

import re
from typing import Optional

# Plain RFC-ish: user + @ + domain.tld
_PLAIN_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}"
)

# Common obfuscations: "foo (at) bar (dot) com", "foo [at] bar [dot] com",
# "foo AT bar DOT com". Whitespace and bracket-style separators only —
# we deliberately don't try to defeat aggressive ROT13 / image-only
# obfuscation, that's a false-positive minefield.
_OBFUSC_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)"
    r"\s*[\[\(\{]?\s*(?:at|@)\s*[\]\)\}]?\s*"
    r"([A-Za-z0-9.\-]+)"
    r"\s*[\[\(\{]?\s*(?:dot|\.)\s*[\]\)\}]?\s*"
    r"([A-Za-z]{2,24})",
    re.IGNORECASE,
)

# Addresses that look real but shouldn't be contacted: bot/admin commons,
# automated reply-to addresses, etc.
_BLOCKED_LOCALS = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "support", "admin", "postmaster", "abuse", "webmaster",
    "info", "contact", "hello", "team", "billing", "sales",
    "automated", "notifications", "notification",
}


def extract_email(text: str) -> Optional[str]:
    """Return the first plausible reachable email in `text`, or None."""
    if not text:
        return None

    # 1. Plain pattern first.
    m = _PLAIN_RE.search(text)
    if m:
        addr = m.group(0).lower().rstrip(".")
        if _is_usable(addr):
            return addr

    # 2. Obfuscated form.
    m = _OBFUSC_RE.search(text)
    if m:
        addr = f"{m.group(1)}@{m.group(2)}.{m.group(3)}".lower()
        if _is_usable(addr):
            return addr

    return None


def _is_usable(addr: str) -> bool:
    if "@" not in addr:
        return False
    local, _, domain = addr.partition("@")
    if not local or not domain or "." not in domain:
        return False
    if local in _BLOCKED_LOCALS:
        return False
    # Obvious throwaway / temp providers — these don't drive sign-ups and
    # bounce often enough to hurt deliverability.
    bad_domains = {
        "example.com", "test.com", "localhost",
        "mailinator.com", "guerrillamail.com", "10minutemail.com",
        "tempmail.com", "throwawaymail.com",
    }
    if domain in bad_domains:
        return False
    return True

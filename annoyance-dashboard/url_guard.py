"""
URL allowlist for market-routing and email links.

P8.2: ``entity_markets.json`` entries and POST /api/market-suggestions are
both user-submittable surfaces whose URLs eventually land as ``<a href="...">``
in the dashboard and in outbound spike-alert emails. Without validation, an
attacker could submit a suggestion (or get a curator to paste one) pointing at
evil.example.com and we'd happily render it — classic open redirect.

We constrain to three apex domains: narve.ai (our own), polymarket.com, and
kalshi.com. Subdomains of those apexes are allowed (staging.narve.ai,
api.polymarket.com, etc.). Anything else is rejected at submission time and
stripped at render time.

Kept intentionally tiny: one allowlist, one predicate, one sanitizer. No
regex, no SSRF guard (that's a different concern — this module only governs
what we will SHOW to a user, not what we will FETCH on their behalf).
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse


ALLOWED_APEXES: frozenset[str] = frozenset({
    "narve.ai",
    "polymarket.com",
    "kalshi.com",
})


def is_allowed_url(url: Optional[str]) -> bool:
    """True iff ``url`` is http/https and its host matches (or is a subdomain
    of) one of ALLOWED_APEXES. False for anything empty, malformed, non-HTTP,
    or pointing outside the allowlist. Never raises."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower().strip()
    if not host:
        return False
    for apex in ALLOWED_APEXES:
        if host == apex or host.endswith("." + apex):
            return True
    return False


def safe_url_or_none(url: Optional[str]) -> Optional[str]:
    """Return the URL unchanged if it passes ``is_allowed_url``, else None.
    Use at render time to short-circuit bad entries rather than crash."""
    return url if is_allowed_url(url) else None

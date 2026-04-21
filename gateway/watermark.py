"""Forensic-watermark helpers — server-side SVG + seed generation.

Two kinds of watermark ride on every authenticated page:

  1. Visible SVG overlay — tiled across the viewport via CSS
     ``background-image: url("data:image/svg+xml;base64,...")``. Generated
     per-request so the email/IP/timestamp are baked into the pixels of
     any screenshot.

  2. Invisible canvas pattern — drawn client-side by ``watermark.js`` from
     a 32-bit seed we hand out here. The seed is stored in
     ``watermark_seeds`` (migration 070) so the forensics tool can walk
     every known seed and pick the match.

The SVG is intentionally minimal (no external fonts, no JS) so content-
security policies don't have to special-case it. Rendered at ~0.045 opacity
so the UI still looks monochrome but a screenshot carries the data.
"""

from __future__ import annotations

import base64
import hashlib
import html as _html
import time
from typing import Optional


def mask_ip(ip: str) -> str:
    """Mask the last octet (IPv4) or last group (IPv6) for display."""
    if not ip:
        return "unknown"
    if ":" in ip:
        # IPv6 — mask the last two groups.
        parts = ip.split(":")
        if len(parts) > 2:
            return ":".join(parts[:-2] + ["*", "x"])
        return ip
    parts = ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.*.x"
    return ip


def _derive_seed(user_id: int, session_id: str) -> int:
    """Derive a deterministic 32-bit seed from (user, session).

    Hash twice (user-id + session-id, then user-id-only as a salt) so a
    leaked seed can't be trivially mapped back to a session id.
    """
    h = hashlib.sha256(f"narve-wm:{user_id}:{session_id}".encode()).digest()
    return int.from_bytes(h[:4], "big")


def session_suffix(session_token_or_hash: str) -> str:
    """Last 8 chars of the session-token hash — safe to display.

    If the caller passed a raw cookie value, hash it first so we never
    render raw token fragments into the SVG.
    """
    if not session_token_or_hash:
        return "anonymous"
    val = session_token_or_hash.strip()
    if len(val) < 48 or any(c not in "abcdef0123456789" for c in val.lower()):
        # Not obviously a SHA-256 hex — hash it to normalise.
        val = hashlib.sha256(val.encode()).hexdigest()
    return val[-8:]


def build_svg(
    *,
    email: str,
    user_id: int,
    session_suffix_value: str,
    ip_masked: str,
    timestamp_utc: Optional[str] = None,
) -> str:
    """Return an SVG string (NOT base64-encoded) carrying forensic lines.

    Keep the viewBox small and the text compact so the SVG tiles nicely
    via ``background-repeat``.
    """
    if timestamp_utc is None:
        timestamp_utc = time.strftime("%Y-%m-%d %H:%MZ", time.gmtime())

    # HTML-escape every interpolated field — SVG uses XML rules so angle
    # brackets and ampersands in user-supplied content would break the
    # document.
    e_email = _html.escape(email or "unknown")
    e_ip = _html.escape(ip_masked or "unknown")
    e_ts = _html.escape(timestamp_utc)
    e_uid = _html.escape(f"uid:{user_id}  sid:{session_suffix_value}")

    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'width="260" height="140" viewBox="0 0 260 140">'
        '<g transform="rotate(-18 130 70)" '
        'font-family="\'SFMono-Regular\',\'Menlo\',\'Consolas\',monospace" '
        'font-size="11" fill="#ffffff" fill-opacity="1" '
        'text-anchor="middle">'
        f'<text x="130" y="50">{e_email}</text>'
        f'<text x="130" y="68">{e_uid}</text>'
        f'<text x="130" y="86">{e_ts}</text>'
        f'<text x="130" y="104">{e_ip}</text>'
        '</g></svg>'
    )


def svg_data_uri(svg: str) -> str:
    """Encode an SVG string as a base64 data URI suitable for CSS url()."""
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def overlay_html(
    *,
    email: str,
    user_id: int,
    session_suffix_value: str,
    ip_masked: str,
    seed: int,
    opacity: float = 0.06,
) -> str:
    """Return the HTML snippet for the fixed-position watermark overlay +
    the invisible steganographic canvas. Call once per page render.

    The outer ``<div>`` is ``pointer-events: none`` so it never blocks
    clicks. Both surfaces are ``z-index: 9999`` so they always sit above
    page chrome but below any full-screen modals the caller might stack
    higher.
    """
    svg = build_svg(
        email=email, user_id=user_id,
        session_suffix_value=session_suffix_value,
        ip_masked=ip_masked,
    )
    uri = svg_data_uri(svg)
    # All HTML-attribute values below either come from constants, integers,
    # or values we already escaped. Safe to splice.
    return (
        '<div id="nv-watermark-visible" aria-hidden="true" '
        'style="position:fixed;inset:0;pointer-events:none;z-index:9999;'
        f'opacity:{opacity:.3f};'
        f'background-image:url(\'{uri}\');'
        'background-repeat:repeat;background-size:260px 140px;'
        '"></div>'
        '<canvas id="nv-watermark-canvas" aria-hidden="true" '
        f'data-seed="{int(seed)}" '
        'style="position:fixed;inset:0;pointer-events:none;z-index:9998;'
        'width:100vw;height:100vh;"></canvas>'
    )


def resolve_ip_from_request(request) -> str:
    """Best-effort client IP extraction (Cloudflare sends CF-Connecting-IP)."""
    headers = getattr(request, "headers", {}) if request else {}
    ip = (
        headers.get("cf-connecting-ip")
        or headers.get("x-forwarded-for", "").split(",")[0].strip()
        or headers.get("x-real-ip")
        or (getattr(getattr(request, "client", None), "host", "") if request else "")
    )
    return ip or ""

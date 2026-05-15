"""HTML allowlist sanitizer for admin-authored newsletter bodies.

Rationale
---------

``email_system/templates/newsletter_blast.html`` renders the admin's
composed body via the renderer's ``raw_`` convention, which skips
HTML-escape (see ``email_system/renderer.py:107-117``). Admins
*legitimately* need rich HTML — links, paragraphs, headings, bold/italic,
lists, the occasional inline image — so escaping the whole thing isn't
viable.

The risk that the security audit flagged: a compromised admin (stolen
session, leaked OTP, internal threat) becomes a mass-phishing surface to
every subscriber. The mitigation is a server-side allowlist sanitizer
that runs *before* the body reaches the renderer, so even pristine input
that contains malicious tags / attributes / URIs is reduced to the
allowlisted subset before storage and send.

Allowlist
---------

Tags kept:
    ``p``, ``a``, ``strong``, ``em``, ``ul``, ``ol``, ``li``, ``br``,
    ``h2``, ``h3``, ``img``

Attributes kept:
    * ``a[href]``  — only when the URL starts with ``http://``,
                     ``https://``, or ``mailto:``.
    * ``img[src]`` — only when the URL starts with ``https://``
                     (per audit: http img surface = mixed-content
                     phishing vector, drop it).
    * ``img[alt]`` — accessibility, treated as inert text.

Everything else is dropped:
    * Disallowed tags (``<script>``, ``<iframe>``, ``<object>``,
      ``<embed>``, ``<style>``, ``<form>``, ``<svg>``, etc.) — tag
      stripped, inner *text* preserved (so ``<script>foo</script>``
      becomes the literal text ``foo``, not silently broken markup).
    * ``on*`` event handlers (``onclick``, ``onerror``, ``onload`` …) —
      attribute dropped.
    * ``javascript:``, ``data:``, ``vbscript:``, ``file:`` URIs in
      href/src — attribute dropped.
    * All other attributes on allowed tags (``style``, ``class``,
      ``id``, ``target``, ``onclick``, …) — dropped. The renderer
      injects its own inline styles on the wrapper, so per-tag styling
      isn't needed.

Implementation
--------------

Hand-rolled on top of the stdlib ``html.parser.HTMLParser``. We don't
pull in ``bleach`` because the gateway already runs without that
dependency and the allowlist is small enough that a custom parser is
auditable in <200 lines. The parser walks the input once, emits only
allowed tags with allowed attributes, and HTML-escapes text data so
content embedded between tags can never break out of the document
structure.
"""

from __future__ import annotations

import html
from html.parser import HTMLParser


# Tags retained verbatim in the output. Anything not in this set has
# its tag-stripping applied; the *text* inside is still emitted so a
# stray ``<div>hello</div>`` becomes the literal text ``hello`` rather
# than vanishing entirely. Void/self-closing tags ``br`` and ``img``
# emit without a closing tag.
_ALLOWED_TAGS = frozenset({
    "p", "a", "strong", "em", "ul", "ol", "li", "br", "h2", "h3", "img",
})

# Void tags — no closing tag emitted.
_VOID_TAGS = frozenset({"br", "img"})

# Attribute allowlist, keyed by tag. Each entry is the set of attribute
# names that survive sanitization for that tag. Anything else is
# dropped, including ``style``, ``class``, ``id``, ``target``, and any
# event handler.
_ALLOWED_ATTRS: dict[str, frozenset[str]] = {
    "a":   frozenset({"href"}),
    "img": frozenset({"src", "alt"}),
}

# URI schemes accepted on ``a[href]``. Anything else (``javascript:``,
# ``data:``, ``vbscript:``, ``file:``, scheme-relative ``//foo``, …)
# gets the attribute dropped entirely so the renderer emits a bare
# ``<a>`` rather than an attacker-controlled href.
_SAFE_HREF_PREFIXES = ("http://", "https://", "mailto:")

# Only HTTPS for inline images — http leaks to mixed-content warnings
# in many clients and gives the attacker a passive-tracking surface in
# the rest. ``data:`` is also outright banned to deny base64-embedded
# phishing imagery.
_SAFE_IMG_SRC_PREFIXES = ("https://",)


def _safe_href(value: str | None) -> str | None:
    """Return ``value`` if it looks like a safe link target, else None.

    Strips surrounding whitespace and lowercases the scheme for the
    comparison so ``  HTTPS://example.com  `` survives the check while
    ``  JavaScript:alert(1)  `` does not.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    lowered = stripped.lower()
    if not lowered.startswith(_SAFE_HREF_PREFIXES):
        return None
    return stripped


def _safe_img_src(value: str | None) -> str | None:
    """Return ``value`` if it's an https:// URL, else None."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    lowered = stripped.lower()
    if not lowered.startswith(_SAFE_IMG_SRC_PREFIXES):
        return None
    return stripped


def _filter_attrs(
    tag: str, attrs: list[tuple[str, str | None]],
) -> list[tuple[str, str]]:
    """Drop disallowed attributes; validate values on allowed ones.

    Always drops ``on*`` event handlers, even on allowed tags. For
    ``a[href]`` and ``img[src]`` the value is run through the scheme
    allowlist; if the value fails the check, the attribute is dropped
    (the tag itself stays so users still see anchor text / alt text).
    """
    allowed = _ALLOWED_ATTRS.get(tag, frozenset())
    out: list[tuple[str, str]] = []
    for name, value in attrs:
        lname = name.lower()
        # Defence-in-depth: ``on*`` handlers can never appear, even on
        # tags whose allowlist would otherwise permit them.
        if lname.startswith("on"):
            continue
        if lname not in allowed:
            continue
        if value is None:
            # HTML5 boolean attribute; none of the surviving
            # allowlisted attributes (href/src/alt) are boolean so
            # we treat the missing value as a drop.
            continue
        if tag == "a" and lname == "href":
            safe = _safe_href(value)
            if safe is None:
                continue
            out.append(("href", safe))
        elif tag == "img" and lname == "src":
            safe = _safe_img_src(value)
            if safe is None:
                continue
            out.append(("src", safe))
        elif tag == "img" and lname == "alt":
            out.append(("alt", value))
        else:
            # Shouldn't reach here given the allowlist, but be explicit:
            # drop anything we didn't whitelist explicitly above.
            continue
    return out


def _format_attrs(attrs: list[tuple[str, str]]) -> str:
    """Render an attribute list as ``key="value"`` pairs, escaped."""
    if not attrs:
        return ""
    parts = []
    for name, value in attrs:
        # ``html.escape(quote=True)`` covers ``"``, ``<``, ``>``, ``&``,
        # ``'`` — sufficient for a double-quoted attribute value.
        parts.append(f'{name}="{html.escape(value, quote=True)}"')
    return " " + " ".join(parts)


class _Sanitizer(HTMLParser):
    """Walks input once, emits only allowed tags + safe text.

    The stack tracks unclosed *allowed* tags so we can rebalance if the
    admin's source has un-nested or missing close tags (the parser
    still gets the structure right via tokenization, but we want our
    output to be balanced even when their input wasn't).
    """

    def __init__(self) -> None:
        # ``convert_charrefs=True`` makes the parser pre-resolve
        # ``&amp;`` etc. into their characters before calling
        # ``handle_data``. We re-escape on emit so the round-trip
        # remains lossless and untrusted.
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._stack: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.lower()
        if tag not in _ALLOWED_TAGS:
            # Drop the tag entirely; subsequent ``handle_data`` calls
            # for content inside still fire so the text survives.
            return
        safe_attrs = _filter_attrs(tag, attrs)
        if tag in _VOID_TAGS:
            self._out.append(f"<{tag}{_format_attrs(safe_attrs)}/>")
        else:
            self._out.append(f"<{tag}{_format_attrs(safe_attrs)}>")
            self._stack.append(tag)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]],
    ) -> None:
        # Self-closing form like ``<br/>``. Always emit as void.
        tag = tag.lower()
        if tag not in _ALLOWED_TAGS:
            return
        safe_attrs = _filter_attrs(tag, attrs)
        self._out.append(f"<{tag}{_format_attrs(safe_attrs)}/>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag not in _ALLOWED_TAGS:
            return
        if tag in _VOID_TAGS:
            return
        # Pop until we find the matching tag, closing any allowed tags
        # we encounter along the way so the output stays balanced.
        if tag not in self._stack:
            return
        while self._stack:
            top = self._stack.pop()
            self._out.append(f"</{top}>")
            if top == tag:
                break

    def handle_data(self, data: str) -> None:
        # Text between tags — always HTML-escape so an attacker can't
        # smuggle ``<script>`` through ``handle_data`` (would only
        # matter if we ever flipped ``convert_charrefs``, but
        # escape-on-emit is cheap and removes a footgun).
        if data:
            self._out.append(html.escape(data, quote=False))

    def handle_comment(self, data: str) -> None:
        # IE conditional comments + comment-smuggled payloads are a
        # known XSS surface in some email clients. Drop all comments.
        return

    def handle_decl(self, decl: str) -> None:
        # No ``<!DOCTYPE …>`` either — newsletter body is a fragment.
        return

    def handle_pi(self, data: str) -> None:
        # ``<?xml …?>`` processing instructions — drop.
        return

    def unknown_decl(self, data: str) -> None:
        # ``<![CDATA[…]]>`` etc. — drop.
        return

    def close(self) -> str:  # type: ignore[override]
        super().close()
        # Close any tags the admin left open.
        while self._stack:
            top = self._stack.pop()
            self._out.append(f"</{top}>")
        return "".join(self._out)


def sanitize_newsletter_html(raw: str) -> str:
    """Apply the allowlist sanitizer to ``raw`` and return the result.

    Always returns a string. ``None`` and empty inputs short-circuit to
    ``""`` so callers can pass form values straight through without
    a None-guard at every callsite. Non-string inputs are coerced via
    ``str()`` for the same reason.
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raw = str(raw)
    if not raw:
        return ""
    parser = _Sanitizer()
    parser.feed(raw)
    return parser.close()


__all__ = ["sanitize_newsletter_html"]

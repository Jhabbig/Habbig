"""Input-sanitation helpers shared across every form / API handler.

Two kinds of protection:

1. **Syntactic** — strip, length-cap, reject control / null bytes. The
   13-item edge-case matrix in EDGE_CASES.md is the acceptance test.
2. **Semantic** — coerce "empty string" to None, trim trailing spaces,
   normalise unicode so two visually-identical inputs hash the same.

None of this is auth or escaping. SQL injection is defended by
parameterised queries in db.py; HTML escaping happens at the template
boundary; session / CSRF checks live elsewhere. These helpers exist so
1) handlers can't accidentally send an invalid-shaped string *past*
those defences, and 2) weird user input surfaces a useful 400, not a
500 from a downstream library.

All functions are pure; no DB, no I/O.

Usage pattern:

    from security.input_hygiene import clean_text, clean_int

    @app.post("/api/feedback")
    async def feedback(request: Request):
        body = await request.json()
        message = clean_text(body.get("message"), max_len=2000, required=True)
        score   = clean_int(body.get("score"), lo=1, hi=5)
        ...
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Optional

from fastapi import HTTPException


# ── Tunables ───────────────────────────────────────────────────────────────

# Max string length we'll ever accept from the outside. Individual
# callers lower this further via `max_len=`.
_HARD_MAX_LEN = 10_000

# Max length after which *any* length check trips — protects the DB
# from "oops I sent a 10 MB request body as a name" scenarios where
# the caller forgot to set max_len.
_ABSOLUTE_MAX_LEN = 1_000_000

# Control-character ranges: C0 controls (0x00-0x1F) minus the three
# we keep for legitimate whitespace (\t \n \r), plus C1 controls
# (0x80-0x9F), plus null byte explicitly. Zero-width characters are
# treated separately via unicode normalisation.
_CTRL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")

# Zero-width + bidi control + other "invisible" glyphs that attackers
# use to slip past length limits or pretend two usernames are the
# same. Keep ordinary whitespace (space, tab, newline).
_INVISIBLE_RE = re.compile(
    "["
    "\u200b"   # zero-width space
    "\u200c"   # zero-width non-joiner
    "\u200d"   # zero-width joiner
    "\u2060"   # word joiner
    "\ufeff"   # byte-order mark
    "\u202a-\u202e"  # LRE/RLE/PDF/LRO/RLO (bidi control)
    "\u2066-\u2069"  # LRI/RLI/FSI/PDI     (bidi isolate)
    "]+"
)


# ── Exceptions ─────────────────────────────────────────────────────────────


def _bad(msg: str, *, field: Optional[str] = None) -> HTTPException:
    """Shared 400 factory. Keeps every input-reject response flat.

    `field` names the offending property — not required, but handy
    for JSON-RPC-ish callers. The body is a JSON-ready dict so the
    caller's JSONResponse wrapper does the right thing.
    """
    detail = {"error": msg}
    if field:
        detail["field"] = field
    return HTTPException(status_code=400, detail=detail)


# ── Text ───────────────────────────────────────────────────────────────────


def clean_text(
    raw: Any,
    *,
    max_len: int = _HARD_MAX_LEN,
    min_len: int = 0,
    required: bool = False,
    strip: bool = True,
    allow_empty: bool = False,
    field: Optional[str] = None,
    normalise: bool = True,
) -> Optional[str]:
    """Normalise a free-form text input.

    * Rejects non-str inputs with 400 (dicts, lists, integers cast to
      str would all mask bugs — caller should convert first).
    * Strips whitespace unless `strip=False`.
    * Strips zero-width, bidi, BOM, and C1 control characters.
    * Rejects any remaining C0 control / null byte.
    * Coerces "" to None unless `allow_empty=True`.
    * Enforces `min_len` / `max_len` in *code points*, not bytes.
    * If `required=True` and the result is None, raises 400.

    Returns the cleaned string or None (if empty and allowed).
    """
    if raw is None:
        if required:
            raise _bad("field required", field=field)
        return None

    if not isinstance(raw, str):
        # Pydantic typically converts non-strings before handlers see
        # them, but raw dict access from request.json() can drop
        # anything here. Fail fast rather than str() over surprising
        # input.
        raise _bad("must be a string", field=field)

    # Kill the bigger-than-a-book class of input before anything else
    # does linear work on it.
    if len(raw) > _ABSOLUTE_MAX_LEN:
        raise _bad("input too large", field=field)

    if normalise:
        # NFC is the form browsers emit; NFKC would also collapse
        # compatibility sequences (pretending fullwidth 'ａ' == 'a'),
        # which is good for usernames but wrong for free text. NFC
        # covers the "é as U+00E9 vs 'e' + combining U+0301" case.
        raw = unicodedata.normalize("NFC", raw)

    # Kill invisible / bidi-hostile characters before measuring length —
    # so an attacker can't stuff a 10k-char zalgo stream under a 200-char
    # limit by having the invisible bytes not "count".
    raw = _INVISIBLE_RE.sub("", raw)

    # Any C0 control (except \t \n \r) or null byte is a hard reject,
    # not a strip — they're either crash-inducing downstream or a
    # sign of weird input.
    if _CTRL_CHAR_RE.search(raw):
        raise _bad("contains control characters", field=field)

    if strip:
        raw = raw.strip()

    # Empty-string handling: if allow_empty is False (default), collapse
    # "" to None. Required-ness is then evaluated on the cleaned value.
    if raw == "" and not allow_empty:
        if required:
            raise _bad("field required", field=field)
        return None

    if max_len < _HARD_MAX_LEN:
        cap = max_len
    else:
        cap = _HARD_MAX_LEN
    if len(raw) > cap:
        raise _bad(
            f"must be {cap} characters or fewer",
            field=field,
        )

    if min_len > 0 and len(raw) < min_len:
        raise _bad(
            f"must be at least {min_len} characters",
            field=field,
        )

    return raw


# ── Numbers ────────────────────────────────────────────────────────────────


def clean_int(
    raw: Any,
    *,
    lo: Optional[int] = None,
    hi: Optional[int] = None,
    required: bool = False,
    default: Optional[int] = None,
    field: Optional[str] = None,
) -> Optional[int]:
    """Coerce to int with range check.

    Rejects:
      * NaN / Infinity (float('inf') fails the int() branch)
      * Scientific notation (`"1e100"` is a float, not int syntax)
      * Floats with non-zero fractional part
      * Booleans (Python's int subclass — surprising)

    Returns `default` for None/empty unless `required=True`.
    """
    if raw is None or raw == "":
        if required:
            raise _bad("field required", field=field)
        return default

    # Bools pass `isinstance(raw, int)` in Python; catch them first so
    # `True`/`False` don't silently coerce to `1`/`0`.
    if isinstance(raw, bool):
        raise _bad("must be an integer", field=field)

    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        if math.isnan(raw) or math.isinf(raw):
            raise _bad("must be a finite integer", field=field)
        if raw != int(raw):
            raise _bad("must be an integer", field=field)
        value = int(raw)
    elif isinstance(raw, str):
        s = raw.strip()
        # Reject scientific / decimal notation explicitly — permissive
        # str→int would raise ValueError, but we want the caller to
        # hit our 400, not a 500 from uncaught ValueError.
        if not re.fullmatch(r"-?\d+", s):
            raise _bad("must be an integer", field=field)
        try:
            value = int(s)
        except ValueError:
            raise _bad("must be an integer", field=field)
    else:
        raise _bad("must be an integer", field=field)

    if lo is not None and value < lo:
        raise _bad(f"must be ≥ {lo}", field=field)
    if hi is not None and value > hi:
        raise _bad(f"must be ≤ {hi}", field=field)
    return value


def clean_float(
    raw: Any,
    *,
    lo: Optional[float] = None,
    hi: Optional[float] = None,
    required: bool = False,
    default: Optional[float] = None,
    field: Optional[str] = None,
) -> Optional[float]:
    """Coerce to finite float with range check. Rejects NaN, ±Infinity."""
    if raw is None or raw == "":
        if required:
            raise _bad("field required", field=field)
        return default
    if isinstance(raw, bool):
        raise _bad("must be a number", field=field)
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        s = raw.strip()
        try:
            value = float(s)
        except ValueError:
            raise _bad("must be a number", field=field)
    else:
        raise _bad("must be a number", field=field)
    if math.isnan(value) or math.isinf(value):
        raise _bad("must be a finite number", field=field)
    if lo is not None and value < lo:
        raise _bad(f"must be ≥ {lo}", field=field)
    if hi is not None and value > hi:
        raise _bad(f"must be ≤ {hi}", field=field)
    return value


# ── Pagination ─────────────────────────────────────────────────────────────


def clean_page(
    raw: Any,
    *,
    default: int = 1,
    max_page: int = 10_000,
    field: str = "page",
) -> int:
    """Coerce a page number. Zero / negative collapse to 1; huge values
    clamp to `max_page`. Callers that want a 400 on out-of-range can
    call `clean_int` directly instead."""
    try:
        n = clean_int(raw, required=False, default=default, field=field)
    except HTTPException:
        return default
    if n is None or n < 1:
        return 1
    if n > max_page:
        return max_page
    return n


def clean_per_page(
    raw: Any,
    *,
    default: int = 20,
    max_per_page: int = 100,
    field: str = "per_page",
) -> int:
    """Same shape as `clean_page` but for per_page / limit arguments.

    Zero or negative collapse to `default`; values above `max_per_page`
    clamp to `max_per_page` (NOT 400). This matches the API convention:
    callers that send a huge `per_page` get back the cap, not an error,
    so a mobile app that hasn't heard about the limit still works.
    """
    try:
        n = clean_int(raw, required=False, default=default, field=field)
    except HTTPException:
        return default
    if n is None or n < 1:
        return default
    if n > max_per_page:
        return max_per_page
    return n


# ── Email (light touch) ────────────────────────────────────────────────────


# Deliberately permissive — accepting "alice+narve@example.com" is the
# point. RFC-perfect regexes are 300 chars long and still miss edge
# cases. This one catches the "obviously not an email" class (no @,
# whitespace, empty local/domain, multiple @, control chars).
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def clean_email(
    raw: Any,
    *,
    required: bool = False,
    field: str = "email",
) -> Optional[str]:
    """Basic email cleanup. Lowercases the *whole* address — lossy for
    rare case-sensitive local parts, but every consumer email provider
    treats local parts case-insensitively, and this kills the
    "alice@example.com and ALICE@example.com look like two users"
    class of bug."""
    s = clean_text(raw, max_len=254, required=required, field=field)
    if s is None:
        return None
    s = s.lower()
    if not _EMAIL_RE.match(s):
        raise _bad("not a valid email address", field=field)
    return s


# ── Slug / handle ──────────────────────────────────────────────────────────


_HANDLE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")


def clean_handle(
    raw: Any,
    *,
    required: bool = False,
    field: str = "handle",
) -> Optional[str]:
    """Narrow charset for source handles + usernames. Alphanum + `_.-`,
    max 64 chars, must not start with a dot or dash — path-traversal
    strings like `../etc` are rejected automatically."""
    s = clean_text(raw, max_len=64, required=required, field=field)
    if s is None:
        return None
    if not _HANDLE_RE.match(s):
        raise _bad("handle contains invalid characters", field=field)
    return s

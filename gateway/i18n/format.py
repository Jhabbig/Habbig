"""Locale-aware number / currency / date / percent formatting.

Mirrors the client-side helpers in gateway/static/i18n-client.js so a
value formatted server-side and then re-formatted client-side (after a
locale switch) looks the same. Powered by Python's standard ``locale``
module where possible; falls back to manual separator swaps when the
host OS doesn't have the locale installed (alpine/slim containers).

Use:
    from gateway.i18n.format import format_number, format_currency,
                                   format_percent, format_date
    format_number(0.8123, "es")        → "0,81"
    format_currency(14.99, "de", "EUR")→ "14,99 €"
    format_percent(0.8123, "pt-br", 1) → "81,2 %"
    format_date(1735689600, "en")      → "1 Jan 2026"

None / empty inputs return "" — callers can drop the result straight
into a template without null-checking.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import locale as _locale
from typing import Any, Optional


# BCP-47 → POSIX locale hint. We try a couple of variants before giving
# up and falling back to manual formatting. Always UTF-8.
_POSIX_HINTS: dict[str, tuple[str, ...]] = {
    "en":    ("en_US.UTF-8", "en_GB.UTF-8", "C.UTF-8"),
    "es":    ("es_ES.UTF-8", "es_MX.UTF-8", "es_AR.UTF-8"),
    "de":    ("de_DE.UTF-8", "de_AT.UTF-8", "de_CH.UTF-8"),
    "pt-br": ("pt_BR.UTF-8", "pt_PT.UTF-8"),
}

# Manual fallback — (thousands-sep, decimal-sep, percent-spacer).
# Used when the host OS can't set the locale. Covers the four
# languages we ship; falls back to English rules for anything else.
_MANUAL_RULES: dict[str, tuple[str, str, str]] = {
    "en":    (",", ".", ""),
    "es":    (".", ",", " "),
    "de":    (".", ",", " "),
    "pt-br": (".", ",", " "),
}


@contextlib.contextmanager
def _use_locale(lang: str):
    """Set LC_ALL to the best POSIX match for *lang* for the duration of
    the block, then restore. Silently no-ops when no match available —
    callers fall back to the manual rules."""
    lang = (lang or "en").lower()
    hints = _POSIX_HINTS.get(lang) or _POSIX_HINTS.get(lang.split("-", 1)[0], ())
    original = _locale.setlocale(_locale.LC_ALL, None)
    applied = False
    for hint in hints:
        try:
            _locale.setlocale(_locale.LC_ALL, hint)
            applied = True
            break
        except _locale.Error:
            continue
    try:
        yield applied
    finally:
        if applied:
            try:
                _locale.setlocale(_locale.LC_ALL, original)
            except _locale.Error:
                pass


def _rules_for(lang: str) -> tuple[str, str, str]:
    lang = (lang or "en").lower()
    if lang in _MANUAL_RULES:
        return _MANUAL_RULES[lang]
    primary = lang.split("-", 1)[0]
    return _MANUAL_RULES.get(primary, _MANUAL_RULES["en"])


def _manual_number(n: float, lang: str, *, max_frac: int) -> str:
    thou, dec, _ = _rules_for(lang)
    # Let Python render the number with the requested precision first.
    rounded = round(n, max_frac)
    if max_frac == 0:
        formatted = f"{int(rounded):,}"
    else:
        formatted = f"{rounded:,.{max_frac}f}"
    # Swap the default , and . for locale separators.
    return (
        formatted.replace(",", "\x00").replace(".", dec).replace("\x00", thou)
    )


def format_number(
    value: Any,
    lang: str = "en",
    *,
    max_fraction_digits: int = 2,
    min_fraction_digits: Optional[int] = None,
) -> str:
    """Locale-aware decimal. Returns "" for None / blank / non-numeric."""
    if value is None or value == "":
        return ""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return ""
    min_frac = min_fraction_digits if min_fraction_digits is not None else 0
    with _use_locale(lang) as applied:
        if applied:
            try:
                return _locale.format_string(
                    f"%.{max_fraction_digits}f",
                    n,
                    grouping=True,
                )
            except Exception:
                pass
    return _manual_number(n, lang, max_frac=max_fraction_digits)


def format_currency(
    value: Any,
    lang: str = "en",
    currency: str = "USD",
) -> str:
    """Locale-aware currency. `currency` is ISO-4217 (``"USD"``, ``"GBP"``,
    ``"EUR"``, ``"BRL"``). Symbol placement and separators match the
    locale; if the host OS doesn't have the locale installed we fall
    back to a compact ``"<symbol><number>"`` or ``"<number> <symbol>"``
    shape depending on the language.
    """
    if value is None or value == "":
        return ""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return ""

    symbol = {
        "USD": "$", "GBP": "£", "EUR": "€", "BRL": "R$",
    }.get(currency.upper(), currency.upper())

    lang_l = (lang or "en").lower()
    number = format_number(n, lang_l, max_fraction_digits=2, min_fraction_digits=2)

    # Anglophone: "$12.34". Continental Europe + LatAm: "12,34 €".
    if lang_l == "en":
        return f"{symbol}{number}"
    return f"{number} {symbol}"


def format_percent(
    value: Any,
    lang: str = "en",
    precision: int = 0,
) -> str:
    """Display *value* (0..1) as a percentage in the locale's style.

    Falls through to formatting the number × 100 with a `%` suffix —
    Spanish / German / Portuguese get the space before % that matches
    their typographic norm.
    """
    if value is None or value == "":
        return ""
    try:
        n = float(value) * 100
    except (TypeError, ValueError):
        return ""
    number = format_number(n, lang, max_fraction_digits=precision, min_fraction_digits=precision)
    _, _, spacer = _rules_for(lang)
    return f"{number}{spacer}%"


def format_date(
    value: Any,
    lang: str = "en",
    *,
    style: str = "short",
) -> str:
    """Locale-aware date. ``value`` may be a datetime, unix ts (s or ms),
    ISO string, or None. ``style`` is ``"short"``, ``"medium"``, or
    ``"long"`` to pick progressively more verbose formats.
    """
    if value is None or value == "":
        return ""
    dt = _coerce_datetime(value)
    if dt is None:
        return ""
    fmt_en = {
        "short": "%Y-%m-%d",
        "medium": "%d %b %Y",
        "long": "%A, %d %B %Y",
    }.get(style, "%Y-%m-%d")
    with _use_locale(lang) as applied:
        if applied:
            try:
                return dt.strftime(fmt_en)
            except Exception:
                pass
    return dt.strftime(fmt_en)


def _coerce_datetime(value: Any) -> Optional[_dt.datetime]:
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime.combine(value, _dt.time.min)
    if isinstance(value, (int, float)):
        ts = float(value)
        # Heuristic: unix seconds → ms.
        if ts > 1e12:
            ts /= 1000.0
        try:
            return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None

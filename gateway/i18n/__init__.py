"""i18n package for the narve.ai gateway.

Public API:

    from gateway.i18n import t, detect_language, SUPPORTED, DEFAULT

    lang = detect_language(request)      # "en" | "es" | "de" | "pt-br"
    greeting = t("nav.dashboards", lang) # "Dashboards" / "Paneles" / …

Locale files live in ``gateway/i18n/locales/<lang>.json`` as flat
``{"key": "string"}`` dicts. Missing keys fall back to the English file,
and missing English falls back to the key itself so the UI never 500s.
"""

from __future__ import annotations

from .translator import DEFAULT, SUPPORTED, clear_cache, load_locale, t
from .detector import (
    LANG_COOKIE_NAME,
    detect_language,
    normalise_lang,
    parse_accept_language,
)
from .format import (
    format_currency,
    format_date,
    format_number,
    format_percent,
)

__all__ = [
    "DEFAULT",
    "SUPPORTED",
    "LANG_COOKIE_NAME",
    "t",
    "load_locale",
    "clear_cache",
    "detect_language",
    "normalise_lang",
    "parse_accept_language",
    "format_currency",
    "format_date",
    "format_number",
    "format_percent",
]

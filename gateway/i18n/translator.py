"""Core translator — `t(key, lang, **kwargs)`.

Design:
  * Flat JSON: one file per language, keyed by dotted semantic strings
    ("nav.billing", "error.rate_limit"). No nested objects so lookup is
    a single dict access.
  * Cache at module level — locale files are small (few hundred KB at
    most), never invalidated in-process. Tests can clear via
    ``clear_cache()``.
  * Fallback chain: requested lang → DEFAULT → key. Never raises.
  * str.format substitution for interpolation — callers pass kwargs
    matching the placeholders in the template.

The auto-translation flag ``_machine`` on individual entries is parsed
transparently: if a value is a dict ``{"text": "…", "_machine": true}``,
we pull out ``text``. Human-reviewed strings are plain strings.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any


log = logging.getLogger("i18n")

LOCALES_DIR = Path(__file__).parent / "locales"
SUPPORTED: list[str] = ["en", "es", "de", "pt-br"]
DEFAULT: str = "en"

_cache: dict[str, dict[str, Any]] = {}


def _locale_path(lang: str) -> Path:
    return LOCALES_DIR / f"{lang}.json"


def load_locale(lang: str) -> dict[str, Any]:
    """Load and cache a locale file. Missing file → empty dict (caller
    falls back to English). Malformed JSON is logged and treated as
    empty so a typo in a community translation never takes the site
    down."""
    if lang in _cache:
        return _cache[lang]
    path = _locale_path(lang)
    if not path.exists():
        _cache[lang] = {}
        return _cache[lang]
    try:
        _cache[lang] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("i18n: locale %s unreadable: %s", lang, e)
        _cache[lang] = {}
    return _cache[lang]


def clear_cache() -> None:
    """Drop all cached locales. Used in tests when a locale file changes
    between cases, and by a future admin-panel "reload translations"
    button."""
    _cache.clear()


def _resolve(entry: Any) -> str | None:
    """Extract the display string from a locale entry.

    Plain-string entries are returned as-is. Dict entries shaped like
    ``{"text": "...", "_machine": true}`` (emitted by the auto-translate
    pipeline) get unwrapped to the text.

    Anything else → None so the caller falls through to the next lang.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        txt = entry.get("text")
        if isinstance(txt, str):
            return txt
    return None


def t(key: str, lang: str = DEFAULT, **kwargs: Any) -> str:
    """Translate *key* into *lang*. Returns the key itself if no
    translation exists in either the requested locale or the default.

    Substitution:
      t("greeting", "es", name="Alice") → template.format(name="Alice")
    """
    lang = (lang or DEFAULT).lower()

    primary = load_locale(lang)
    resolved = _resolve(primary.get(key))

    if resolved is None and lang != DEFAULT:
        fallback = load_locale(DEFAULT)
        resolved = _resolve(fallback.get(key))

    if resolved is None:
        resolved = key

    if not kwargs:
        return resolved
    try:
        return resolved.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        # Missing placeholder or malformed {} in the template → return
        # the raw template rather than raising. Better to show a {name}
        # than to 500 the page.
        return resolved

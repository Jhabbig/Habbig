#!/usr/bin/env python3
"""Scan the gateway for user-facing strings → emit candidate i18n keys.

Scope:
  * Every gateway/static/*.html file — <title>, button text, label text,
    static copy in <h1>..<h4>, <p>, <span>, <a>, placeholder=, alt=, etc.
  * Every Python module under gateway/ that calls render_page(...) — the
    `raw_*` string kwargs often carry translatable blurbs.

The script NEVER overwrites existing keys in en.json. It emits a
``candidates.json`` alongside the locale files that an engineer reviews,
trims, and merges by hand. This keeps noise out of the canonical locale
while letting us mechanically spot brand-new untranslated strings.

Usage:
    python3 gateway/scripts/extract_strings.py
    python3 gateway/scripts/extract_strings.py --diff       # print only new keys
    python3 gateway/scripts/extract_strings.py --stats      # just counts
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent          # gateway/
STATIC_DIR = ROOT / "static"
EMAIL_TEMPLATES_DIR = ROOT / "email_system" / "templates"
LOCALES_DIR = ROOT / "i18n" / "locales"
CANDIDATES_OUT = LOCALES_DIR / "candidates.json"


# Tags whose inner text is user-facing. Other tags (script/style) are skipped
# elsewhere before we ever look at their children.
TEXT_TAGS = {
    "title", "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "li", "a", "button", "label", "span", "strong", "em",
    "small", "figcaption", "summary", "option",
}

# Attributes that carry user-visible text. "value" is for submit buttons.
TEXT_ATTRS = {"placeholder", "alt", "title", "aria-label", "value"}

# Strings we never want in the locale. Most of these are CSS / JSON fragments
# that leak into HTML via <script> or inline style attrs.
NOISE_RE = re.compile(
    r"^(https?://|/|#|[{}\[\];:]|&[a-z]+;|\s*$|-?\d+(\.\d+)?(%|px|em|rem|vh|vw)?)"
)

# Strip leading / trailing whitespace, collapse runs of internal whitespace.
WS_RE = re.compile(r"\s+")


def _clean(s: str) -> str:
    return WS_RE.sub(" ", s.strip())


def _is_translatable(s: str) -> bool:
    if not s or len(s) < 2:
        return False
    if len(s) > 500:
        # Likely a paragraph of static body copy — translate in chunks.
        return False
    if s.count(" ") == 0 and len(s) <= 3:
        # Single short words are likely icons / separators.
        return False
    if NOISE_RE.match(s):
        return False
    if not any(c.isalpha() for c in s):
        return False
    # Jinja-ish {{ ... }} placeholders — skip, the inner key is the real hit.
    if "{{" in s and "}}" in s:
        return False
    return True


def _semantic_key(text: str, source_hint: str) -> str:
    """Derive a stable dotted key for *text*. Uses the filename as the
    namespace so duplicate strings across pages don't collide."""
    ns = Path(source_hint).stem.replace("-", "_")
    # Short slug from the first 40 chars, lowercase, alnum-plus-underscore.
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower())[:40].strip("_")
    if not slug:
        # Fall back to a content hash so we still emit something.
        slug = hashlib.sha1(text.encode()).hexdigest()[:10]
    return f"{ns}.{slug}"


# ── HTML extraction ─────────────────────────────────────────────────────────


TAG_RE = re.compile(r"<(\w+)([^>]*)>(.*?)</\1>", re.DOTALL)
SELF_CLOSING_ATTR_RE = re.compile(
    r'(' + "|".join(sorted(TEXT_ATTRS, key=len, reverse=True)) + r')\s*=\s*"([^"]+)"',
    re.IGNORECASE,
)


def extract_from_html(path: Path) -> list[tuple[str, str]]:
    """Return [(key, text), ...] for *path*."""
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return []

    # Drop <script> and <style> blocks up front.
    content = re.sub(r"<script[^>]*>.*?</script>", "", content, flags=re.DOTALL | re.IGNORECASE)
    content = re.sub(r"<style[^>]*>.*?</style>", "", content, flags=re.DOTALL | re.IGNORECASE)

    hits: list[tuple[str, str]] = []

    # Tag inner text.
    for m in TAG_RE.finditer(content):
        tag = m.group(1).lower()
        if tag not in TEXT_TAGS:
            continue
        inner = _clean(re.sub(r"<[^>]+>", " ", m.group(3)))
        if _is_translatable(inner):
            hits.append((_semantic_key(inner, str(path)), inner))

    # Attribute values.
    for m in SELF_CLOSING_ATTR_RE.finditer(content):
        value = _clean(m.group(2))
        if _is_translatable(value):
            hits.append((_semantic_key(value, str(path)), value))

    return hits


# ── Python extraction (render_page kwargs + common JSONResponse message) ───


RENDER_PAGE_RE = re.compile(
    r'render_page\s*\([^)]*?(?P<body>\{[\s\S]*?\}|[^)]*)\)'
)
RAW_KW_RE = re.compile(
    r'(raw_[a-z_]+|[a-z_]+)\s*=\s*([fr]?"((?:[^"\\]|\\.)*)"|[fr]?\'((?:[^\'\\]|\\.)*)\')',
    re.IGNORECASE,
)


def extract_from_python(path: Path) -> list[tuple[str, str]]:
    try:
        src = path.read_text(encoding="utf-8")
    except Exception:
        return []

    hits: list[tuple[str, str]] = []
    # Look only inside render_page(...) call sites to avoid grabbing every
    # docstring in the file.
    for m in RENDER_PAGE_RE.finditer(src):
        for kw in RAW_KW_RE.finditer(m.group("body") if m.group("body") else m.group(0)):
            _name, _, dbl, sgl = kw.groups()
            value = _clean(dbl or sgl or "")
            if _is_translatable(value):
                hits.append((_semantic_key(value, str(path)), value))
    return hits


# ── JS extraction — pull string literals passed to user-facing DOM APIs ────

# Narrow, high-signal patterns: we don't want every string literal in
# every JS file (that would include API paths, event names, CSS class
# names). Instead we grab literals passed to functions that touch the
# DOM text layer or render banners / toasts / alerts.
_JS_TEXT_CALLS = (
    "textContent", "innerText", "placeholder", "alt", "title",
    "alert", "confirm", "prompt",
    "narveSkel.error", "narveToast", "showToast", "showError",
)
_JS_CALL_RE = re.compile(
    r'(?:' + "|".join(re.escape(c) for c in _JS_TEXT_CALLS) + r')'
    r'\s*[=(]\s*([`"\'])((?:\\.|(?!\1).)*)\1',
)
# Also grab the common `t("key")` shape — flags strings that the author
# already intends to translate; useful to audit that their keys exist.
_JS_T_CALL_RE = re.compile(
    r"""\bt\(\s*['"]([^'"]+)['"]\s*[,)]""",
)


def extract_from_js(path: Path) -> list[tuple[str, str]]:
    try:
        src = path.read_text(encoding="utf-8")
    except Exception:
        return []
    hits: list[tuple[str, str]] = []
    for m in _JS_CALL_RE.finditer(src):
        value = _clean(m.group(2))
        if _is_translatable(value):
            hits.append((_semantic_key(value, str(path)), value))
    # `t("some.key")` calls — record the key itself so the auditor can
    # cross-check against en.json. No translation needed since the key
    # IS the locale reference.
    for m in _JS_T_CALL_RE.finditer(src):
        key = m.group(1).strip()
        if key:
            hits.append(("__refs__." + key, "(referenced by JS)"))
    return hits


# ── Driver ──────────────────────────────────────────────────────────────────


def load_existing_keys() -> set[str]:
    keys: set[str] = set()
    en = LOCALES_DIR / "en.json"
    if en.exists():
        try:
            keys.update(json.loads(en.read_text(encoding="utf-8")).keys())
        except Exception:
            pass
    return keys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--diff", action="store_true", help="only print new keys")
    ap.add_argument("--stats", action="store_true", help="just counts")
    args = ap.parse_args()

    existing = load_existing_keys()

    discovered: dict[str, str] = {}

    for html_path in sorted(STATIC_DIR.glob("*.html")):
        for key, text in extract_from_html(html_path):
            discovered.setdefault(key, text)

    # Email templates live outside gateway/static/ — their copy is just
    # as user-facing (often more so; transactional emails can't fall
    # back to English without embarrassment).
    if EMAIL_TEMPLATES_DIR.exists():
        for html_path in sorted(EMAIL_TEMPLATES_DIR.glob("*.html")):
            for key, text in extract_from_html(html_path):
                discovered.setdefault(key, text)

    for py_path in sorted(ROOT.glob("*.py")):
        for key, text in extract_from_python(py_path):
            discovered.setdefault(key, text)

    # Client JS — toasts, dynamic row text, error banners. High-signal
    # surface that the HTML walker misses because the copy lives in JS.
    for js_path in sorted(STATIC_DIR.glob("*.js")):
        # Skip vendored / minified blobs.
        if ".min." in js_path.name or "vendor" in js_path.name.lower():
            continue
        for key, text in extract_from_js(js_path):
            discovered.setdefault(key, text)

    new_keys = {k: v for k, v in discovered.items() if k not in existing}

    if args.stats:
        print(f"existing keys: {len(existing)}")
        print(f"discovered:    {len(discovered)}")
        print(f"new:           {len(new_keys)}")
        return 0

    CANDIDATES_OUT.write_text(
        json.dumps(new_keys if args.diff else discovered,
                   indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    print(f"Wrote {len(discovered)} candidates to {CANDIDATES_OUT}")
    print(f"  new:     {len(new_keys)}")
    print(f"  exists:  {len(discovered) - len(new_keys)}")
    if args.diff:
        for k in sorted(new_keys)[:20]:
            print(f"  + {k}  →  {new_keys[k][:60]!r}")
        if len(new_keys) > 20:
            print(f"  (+{len(new_keys) - 20} more)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

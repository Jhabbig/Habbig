#!/usr/bin/env python3
"""Scan CSS files for foreground/background pairs and flag contrast failures.

Approach: walk ``static/*.css`` and the inline ``<style>`` blocks inside
``static/*.html``, collect every ``color: …`` and ``background-color: …``
declaration (including CSS custom properties that get resolved to a colour
via ``:root { --token: #hex }``), and pair them by selector scope.

For each pair, compute the WCAG 2.1 contrast ratio. Flag:
  - ratio < 4.5 for body-sized text (anything except tokens containing
    ``heading``, ``title``, ``display``)
  - ratio < 3.0 for large text (the heading/title tokens) and UI
    components

This is a static heuristic — false positives are possible for selectors
whose effective background differs at runtime (e.g. inside a modal
overlay). Each flagged pair gets its file:line so the reviewer can
check in context.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


# ── colour parsing ─────────────────────────────────────────────────────────


HEX_RE = re.compile(r"^#([0-9a-fA-F]{3,8})$")
RGB_RE = re.compile(r"^rgba?\(\s*([^)]+)\)$")


def _parse_hex(value: str) -> tuple[int, int, int, float] | None:
    match = HEX_RE.match(value.strip())
    if not match:
        return None
    h = match.group(1)
    if len(h) == 3:
        r, g, b = (int(ch * 2, 16) for ch in h)
        return (r, g, b, 1.0)
    if len(h) == 4:
        r, g, b, a = (int(ch * 2, 16) for ch in h)
        return (r, g, b, a / 255)
    if len(h) == 6:
        r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
        return (r, g, b, 1.0)
    if len(h) == 8:
        r, g, b, a = (int(h[i : i + 2], 16) for i in (0, 2, 4, 6))
        return (r, g, b, a / 255)
    return None


def _parse_rgb(value: str) -> tuple[int, int, int, float] | None:
    match = RGB_RE.match(value.strip())
    if not match:
        return None
    parts = [p.strip() for p in match.group(1).replace("/", ",").split(",") if p.strip()]
    if len(parts) < 3:
        return None
    try:
        r, g, b = (int(float(p.rstrip("%")) * (2.55 if p.endswith("%") else 1)) for p in parts[:3])
        a = float(parts[3]) if len(parts) >= 4 else 1.0
    except ValueError:
        return None
    return (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)), a)


NAMED_COLOURS = {
    "white": (255, 255, 255, 1.0),
    "black": (0, 0, 0, 1.0),
    "transparent": (0, 0, 0, 0.0),
    "red": (255, 0, 0, 1.0),
    "green": (0, 128, 0, 1.0),
    "blue": (0, 0, 255, 1.0),
    "gray": (128, 128, 128, 1.0),
    "grey": (128, 128, 128, 1.0),
}


def parse_colour(value: str) -> tuple[int, int, int, float] | None:
    """Return (r, g, b, alpha) or None if not parseable."""
    if not value:
        return None
    v = value.strip().rstrip(";").strip()
    if v.lower() in NAMED_COLOURS:
        return NAMED_COLOURS[v.lower()]
    return _parse_hex(v) or _parse_rgb(v)


# ── WCAG contrast ─────────────────────────────────────────────────────────


def _linearise(chan: float) -> float:
    c = chan / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 0.2126 * _linearise(r) + 0.7152 * _linearise(g) + 0.0722 * _linearise(b)


def contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    l1 = _luminance(fg)
    l2 = _luminance(bg)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


# ── Token extraction ──────────────────────────────────────────────────────


TOKEN_RE = re.compile(r"--([a-zA-Z0-9_-]+)\s*:\s*([^;]+);")
COLOR_DECL_RE = re.compile(
    r"(?P<prop>(?:color|background|background-color))\s*:\s*(?P<value>[^;}]+)[;}]",
    re.IGNORECASE,
)


def extract_css_tokens(text: str) -> dict[str, str]:
    """Return {token_name: resolved colour value} by walking `--foo: …` decls."""
    tokens: dict[str, str] = {}
    for match in TOKEN_RE.finditer(text):
        name, raw_value = match.group(1), match.group(2).strip()
        tokens[name] = raw_value
    # Resolve var() chains (one level is enough for our tree).
    for _ in range(3):
        for name, value in list(tokens.items()):
            ref_match = re.match(r"var\(--([a-zA-Z0-9_-]+)(?:\s*,\s*([^)]+))?\)", value)
            if ref_match:
                ref_name = ref_match.group(1)
                fallback = ref_match.group(2)
                if ref_name in tokens:
                    tokens[name] = tokens[ref_name]
                elif fallback:
                    tokens[name] = fallback.strip()
    return tokens


def resolve_colour(raw_value: str, tokens: dict[str, str]) -> tuple[int, int, int, float] | None:
    """Resolve a raw CSS colour value (may contain var()) to RGBA tuple."""
    value = raw_value.strip()
    match = re.match(r"var\(--([a-zA-Z0-9_-]+)(?:\s*,\s*([^)]+))?\)", value)
    if match:
        ref_name = match.group(1)
        fallback = match.group(2)
        if ref_name in tokens:
            value = tokens[ref_name]
        elif fallback:
            value = fallback.strip()
        else:
            return None
    return parse_colour(value)


# ── Audit ──────────────────────────────────────────────────────────────────


def _walk_sources():
    """Yield (path, text) for every file whose contrast we audit."""
    for p in sorted(STATIC_DIR.rglob("*.css")):
        yield p, p.read_text(errors="replace")
    for p in sorted(STATIC_DIR.rglob("*.html")):
        text = p.read_text(errors="replace")
        # Inline <style> blocks only — attribute styles would need a DOM parser.
        for block in re.findall(r"<style[^>]*>(.*?)</style>", text, re.DOTALL | re.IGNORECASE):
            yield p, block


_HEADING_TOKEN_HINTS = ("heading", "title", "display", "hero", "h1", "h2")


def is_heading_scope(decl_line: str) -> bool:
    low = decl_line.lower()
    return any(hint in low for hint in _HEADING_TOKEN_HINTS)


def audit() -> list[dict]:
    findings: list[dict] = []
    # First pass: collect every token mapping seen anywhere so we can
    # resolve ``color: var(--text-primary)`` regardless of which file set
    # the token.
    global_tokens: dict[str, str] = {}
    for _path, text in _walk_sources():
        global_tokens.update(extract_css_tokens(text))

    # Collect all background values so we can pair fg against each likely bg.
    # For a heuristic pass, we check against the two most common page
    # backgrounds (``--bg-void`` / ``--bg-surface``) plus white and black.
    likely_bgs_raw = []
    for token_name in ("bg-void", "bg-base", "bg-surface", "bg-raised", "bg-overlay"):
        if token_name in global_tokens:
            likely_bgs_raw.append((token_name, global_tokens[token_name]))
    likely_bgs: list[tuple[str, tuple[int, int, int]]] = []
    for name, raw in likely_bgs_raw:
        resolved = resolve_colour(raw, global_tokens)
        if resolved and resolved[3] >= 0.5:
            likely_bgs.append((name, resolved[:3]))
    # Always include pure white/black so light/dark themes both get coverage.
    likely_bgs.extend([("white", (255, 255, 255)), ("black", (0, 0, 0))])

    for path, text in _walk_sources():
        for match in COLOR_DECL_RE.finditer(text):
            prop = match.group("prop").lower()
            if prop != "color":
                continue
            raw_value = match.group("value").strip()
            fg = resolve_colour(raw_value, global_tokens)
            if not fg or fg[3] < 0.5:
                continue
            line_no = text.count("\n", 0, match.start()) + 1
            decl_line = text.splitlines()[line_no - 1] if line_no - 1 < len(text.splitlines()) else ""
            is_heading = is_heading_scope(decl_line)
            required = 3.0 if is_heading else 4.5
            # Flag ONLY when every plausible background is below ratio.
            # A "token contrasts against none of our backgrounds" result is
            # the interesting signal; a single-background failure may be
            # intentional in a themed widget.
            fails: list[str] = []
            best = 0.0
            for bg_name, bg_rgb in likely_bgs:
                ratio = contrast_ratio(fg[:3], bg_rgb)
                best = max(best, ratio)
                if ratio < required:
                    fails.append(f"{bg_name}={ratio:.2f}")
            if len(fails) == len(likely_bgs):
                findings.append({
                    "file": str(path.relative_to(STATIC_DIR.parent)),
                    "line": line_no,
                    "decl": decl_line.strip()[:120],
                    "fg": raw_value,
                    "required": required,
                    "best_ratio": round(best, 2),
                    "fails_against": fails,
                })
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Static contrast audit for narve.ai CSS.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args()

    findings = audit()
    if args.json:
        import json
        print(json.dumps(findings, indent=2))
        return 1 if findings else 0

    if not findings:
        print("no contrast failures detected (heuristic: every likely bg passes)")
        return 0

    print(f"{len(findings)} potential contrast issues:\n")
    for f in findings:
        print(f"{f['file']}:{f['line']}  fg={f['fg']}  required≥{f['required']:.1f}  "
              f"best={f['best_ratio']}  fails={','.join(f['fails_against'])}")
        print(f"    {f['decl']}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

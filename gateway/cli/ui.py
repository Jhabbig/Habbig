"""Terminal translation of the narve.ai design system (gateway/static/tokens.css).

The web system is monochrome, typography-forward and information-dense; colour
never carries information. In a terminal the theme (light or dark background)
belongs to the user, so instead of fixed greys this module maps the token
scales onto SGR *attributes*, which invert correctly on both themes — the same
role the light/dark token tables play on the web:

    CSS token                  Terminal rendering
    ─────────────────────────  ────────────────────────────────────────────
    --text-primary             bold, default foreground
    --text-secondary           default foreground
    --text-tertiary            dim (meta, captions, table headers)
    --text-quaternary          dim (decorative separators only — never data)
    --rank-1 (strongest)       reverse video (badge fill), bold elsewhere
    --rank-2                   bold
    --rank-3                   default
    --rank-4 (minimal)         dim
    --border-ghost/subtle      light box-drawing rules '─', dim
    --error-border             heavy rule '━' — border *weight*, not hue
    --interactive-bg           reverse video (the "primary button" fill)
    --font-mono                the terminal itself; numerics right-aligned
    card-meta style            dim + UPPERCASE (+ letterspacing on wordmark)

Rules carried over verbatim from the design system:
  * No colour for hierarchy — rank tiers differ by weight, not hue.
  * Error surfaces differ by border weight, not colour.
  * Destructive actions are guarded by confirmation, not styling.
  * --text-quaternary is decorative only and never renders data.

ANSI output is suppressed when stdout is not a TTY, when $NO_COLOR is set, or
when $TERM is "dumb", so piped output stays clean for grep/awk.
"""

from __future__ import annotations

import os
import shutil
import sys
import time

# ── Capability detection ──────────────────────────────────────────────────────

def _ansi_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    return sys.stdout.isatty()


ANSI = _ansi_enabled()


def disable_ansi() -> None:
    """Force plain output (used by --plain / --json)."""
    global ANSI
    ANSI = False


def _sgr(code: str, text: str) -> str:
    if not ANSI or not text:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


# ── Text scale (tokens → attributes) ─────────────────────────────────────────

def primary(text: str) -> str:      # --text-primary
    return _sgr("1", text)


def secondary(text: str) -> str:    # --text-secondary
    return text


def tertiary(text: str) -> str:     # --text-tertiary
    return _sgr("2", text)


def quaternary(text: str) -> str:   # --text-quaternary — decorative only
    return _sgr("2", text)


def inverse(text: str) -> str:      # --interactive-bg / rank-1 fill
    return _sgr("7", text)


# ── Rank scale (monochrome tiers — darker = more emphasis) ───────────────────

RANKS = {1: lambda s: _sgr("1;7", s), 2: primary, 3: secondary, 4: tertiary}


def badge(label: str, rank: int = 3) -> str:
    """Monochrome badge. Tier is carried by weight/fill plus the text label
    itself — exactly the web .badge-* pattern, never by hue."""
    return RANKS.get(rank, secondary)(f" {label.upper()} ")


# ── Rules and borders ────────────────────────────────────────────────────────

def term_width(default: int = 100) -> int:
    return shutil.get_terminal_size((default, 24)).columns


def rule(weight: str = "ghost", width: int | None = None) -> str:
    """--border-ghost / --border-subtle → light line; error/strong → heavy."""
    w = width or min(term_width(), 100)
    ch = "━" if weight in ("strong", "error") else "─"
    line = ch * w
    return line if weight in ("strong", "error") else quaternary(line)


# ── Typography components ────────────────────────────────────────────────────

def wordmark() -> str:
    """--font-display equivalent: the letterspaced wordmark."""
    return primary("N A R V E") + "  " + tertiary("gateway engine cli")


def heading(text: str) -> str:
    """Section heading (--text-2xl / semibold)."""
    return primary(text)


def meta(text: str) -> str:
    """card-meta: uppercase, tracked, tertiary."""
    return tertiary(text.upper())


# ── Data components ──────────────────────────────────────────────────────────

def table(headers: list[str], rows: list[list[str]],
          aligns: list[str] | None = None) -> str:
    """.data-table — uppercase tertiary header, hairline under the header row,
    tabular numerics right-aligned via `aligns` ('l' or 'r' per column)."""
    aligns = aligns or ["l"] * len(headers)
    # Width computation must ignore ANSI codes.
    import re
    strip = lambda s: re.sub(r"\x1b\[[0-9;]*m", "", s)
    cols = len(headers)
    widths = [len(strip(h)) for h in headers]
    for row in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(strip(row[i])))

    def fmt(cell: str, i: int) -> str:
        pad = widths[i] - len(strip(cell))
        return (" " * pad + cell) if aligns[i] == "r" else (cell + " " * pad)

    gap = "  "
    out = [gap.join(fmt(meta(h), i) for i, h in enumerate(headers)).rstrip()]
    out.append(quaternary("─" * min(sum(widths) + 2 * (cols - 1), term_width())))
    for row in rows:
        out.append(gap.join(fmt(row[i], i) for i in range(cols)).rstrip())
    return "\n".join(out)


def kv(pairs: list[tuple[str, str]]) -> str:
    """Definition list: tertiary uppercase label column, primary values."""
    width = max((len(k) for k, _ in pairs), default=0)
    return "\n".join(f"{meta(k.ljust(width))}  {v}" for k, v in pairs)


def banner(text: str, kind: str = "info") -> str:
    """.banner — info/warning render in rank tiers; error is set apart by
    border *weight* (heavy rule), never by colour."""
    if kind == "error":
        bar = "━" * min(term_width(), 100)
        return f"{bar}\n{primary(text)}\n{bar}"
    body = primary(text) if kind == "warning" else secondary(text)
    line = rule("ghost")
    return f"{line}\n{body}\n{line}"


def empty_state(title: str, hint: str = "") -> str:
    """.empty-state — centred-feel, tertiary."""
    out = [f"  {secondary(title)}"]
    if hint:
        out.append(f"  {tertiary(hint)}")
    return "\n" + "\n".join(out) + "\n"


# ── Formatting helpers (--font-mono, tabular-nums territory) ─────────────────

def money(cents: float) -> str:
    return f"${cents / 100:,.2f}"


def ts(epoch: int | float | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not epoch:
        return tertiary("—")
    return time.strftime(fmt, time.localtime(epoch))


def dot() -> str:
    """Decorative separator — the one legitimate --text-quaternary use."""
    return quaternary(" · ")


# ── Destructive-action guard ─────────────────────────────────────────────────

def confirm(prompt: str, assume_yes: bool = False) -> bool:
    """btn-destructive: monochrome, differentiated by a confirmation dialog.
    Non-interactive runs must pass --yes explicitly."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(banner("Refusing destructive action without --yes in a "
                     "non-interactive session.", "error"))
        return False
    reply = input(f"{primary(prompt)} {tertiary('[y/N]')} ").strip().lower()
    return reply in ("y", "yes")

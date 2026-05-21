"""Shareable Open Graph cards.

Each card is a 1200x630 PNG (the OG / Twitter standard) rendered with
Pillow. Keeping the renderer entirely server-side means no headless
Chromium dependency in the container, and the PNGs cache well at the
gateway / CDN layer.

Three card kinds:

  mood       : the national mood gauge + verbal label
  country    : per-country (Clark-Fisher) profile
  backtest   : the election-backtest headline accuracy number

The same module exposes `html_preview(...)` for each kind — a minimal
HTML page with the OG meta tags pointing at the PNG, so a link unfurled
on Twitter / Slack / etc. shows the card.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from io import BytesIO

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

# Colors — match the dashboard palette
BG       = "#0e1117"
PANEL    = "#161b22"
FG       = "#e6edf3"
MUTED    = "#8b949e"
ACCENT   = "#ec4899"
GOOD     = "#56d364"
WARN     = "#d29922"
BAD      = "#f85149"
GRID     = "#30363d"

W, H = 1200, 630

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]


def _font(size: int, bold: bool = False):
    paths = list(_FONT_CANDIDATES)
    if not bold:
        paths = [p for p in paths if "Bold" not in p] + paths
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _color_for_mood(v: float | None) -> str:
    if v is None:
        return MUTED
    if v >= 70: return GOOD
    if v >= 55: return WARN
    if v >= 40: return WARN
    return BAD


def _chrome(d: "ImageDraw.ImageDraw") -> None:
    """Common branding — accent bar + header + URL footer."""
    d.rectangle([(0, 0), (W, 8)], fill=ACCENT)
    d.text((48, 32), "VOTER PULSE", fill=ACCENT, font=_font(22, bold=True))
    d.text((48, 64), "How voters feel and how their lives are going.", fill=MUTED, font=_font(20))
    d.text((W - 48, H - 36), "pulse.narve.ai", fill=MUTED, font=_font(20), anchor="rs")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    d.text((48, H - 36), f"snapshot {ts}", fill=MUTED, font=_font(16), anchor="ls")


def _png_bytes(img: "Image.Image") -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _placeholder(message: str) -> bytes:
    """Always-available fallback if Pillow isn't installed."""
    return (
        "<svg xmlns='http://www.w3.org/2000/svg' width='1200' height='630' "
        "viewBox='0 0 1200 630'><rect width='1200' height='630' fill='#0e1117'/>"
        "<text x='600' y='315' fill='#e6edf3' text-anchor='middle' "
        "font-family='sans-serif' font-size='36'>" + message + "</text></svg>"
    ).encode("utf-8")


# ── Cards ────────────────────────────────────────────────────────────────────

def render_mood_card(mood: dict) -> tuple[bytes, str]:
    """Return (png_bytes, content_type)."""
    if not _PIL_AVAILABLE:
        return _placeholder("voter pulse — install Pillow to render cards"), "image/svg+xml"

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _chrome(d)

    overall = mood.get("overall")
    label = mood.get("label") or "—"
    color = _color_for_mood(overall)

    # Big number
    big = f"{round(overall)}" if overall is not None else "—"
    d.text((W // 2, H // 2 - 30), big, fill=color, font=_font(280, bold=True), anchor="mm")

    # Label
    d.text((W // 2, H // 2 + 140), f"NATIONAL MOOD · {label.upper()}",
           fill=FG, font=_font(34, bold=True), anchor="mm")

    # Sub-score strip
    subs = mood.get("subscores") or {}
    line_y = H // 2 + 200
    parts = []
    for name in ("pocketbook", "jobs", "sentiment"):
        s = (subs.get(name) or {}).get("score")
        parts.append(f"{name} {round(s) if s is not None else '—'}")
    d.text((W // 2, line_y), "    ·    ".join(parts),
           fill=MUTED, font=_font(22), anchor="mm")

    return _png_bytes(img), "image/png"


def render_country_card(country: dict) -> tuple[bytes, str]:
    if not _PIL_AVAILABLE:
        return _placeholder("voter pulse — install Pillow to render cards"), "image/svg+xml"

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _chrome(d)

    name = country.get("name") or country.get("iso3") or "—"
    iso3 = country.get("iso3") or ""
    latest_stage = country.get("latest_stage") or {}
    stage_label = latest_stage.get("label") or "—"
    stage_n = latest_stage.get("stage")

    # Country name
    d.text((48, 140), name, fill=FG, font=_font(68, bold=True))
    d.text((48, 218), f"{iso3} · Clark–Fisher stage {stage_n}",
           fill=MUTED, font=_font(26))

    # Stage label as accent banner
    d.text((48, 280), stage_label, fill=ACCENT, font=_font(48, bold=True))

    # Sector triple
    if latest_stage:
        a = latest_stage.get("agriculture_pct") or 0
        i = latest_stage.get("industry_pct") or 0
        s = latest_stage.get("services_pct") or 0
        # Big horizontal bar
        bar_x, bar_y, bar_w, bar_h = 48, 400, W - 96, 60
        total = a + i + s or 1
        seg_a = int(bar_w * a / total)
        seg_i = int(bar_w * i / total)
        seg_s = bar_w - seg_a - seg_i
        d.rectangle([bar_x, bar_y, bar_x + seg_a, bar_y + bar_h], fill="#a78bfa")
        d.rectangle([bar_x + seg_a, bar_y, bar_x + seg_a + seg_i, bar_y + bar_h], fill="#f59e0b")
        d.rectangle([bar_x + seg_a + seg_i, bar_y, bar_x + bar_w, bar_y + bar_h], fill="#34d399")
        d.text((bar_x, bar_y + bar_h + 16),
               f"Agriculture {a:.1f}%        Industry {i:.1f}%        Services {s:.1f}%",
               fill=FG, font=_font(22, bold=True))

    return _png_bytes(img), "image/png"


def render_backtest_card(backtest: dict) -> tuple[bytes, str]:
    if not _PIL_AVAILABLE:
        return _placeholder("voter pulse — install Pillow to render cards"), "image/svg+xml"

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _chrome(d)

    headline = backtest.get("headline")
    if headline and headline.get("accuracy_pct") is not None:
        big = f"{headline['correct']}/{headline['n']}"
        acc = headline["accuracy_pct"]
        color = GOOD if acc >= 70 else WARN if acc >= 55 else BAD
        d.text((W // 2, H // 2 - 60), big, fill=color,
               font=_font(220, bold=True), anchor="mm")
        d.text((W // 2, H // 2 + 90),
               f"presidential elections called at the {headline['horizon_months']}-month horizon",
               fill=FG, font=_font(28), anchor="mm")
        d.text((W // 2, H // 2 + 140),
               f"{acc:.0f}% accuracy · same mood-index formula the live gauge uses",
               fill=MUTED, font=_font(22), anchor="mm")
    else:
        d.text((W // 2, H // 2), "election backtest unavailable",
               fill=MUTED, font=_font(36), anchor="mm")

    return _png_bytes(img), "image/png"


# ── HTML preview pages (with OG tags) ────────────────────────────────────────

def html_preview(kind: str, og_image_url: str, title: str, description: str,
                 canonical_url: str) -> str:
    """Tiny HTML page that unfurls into the card on social platforms.

    The page also links straight to the dashboard so anyone clicking
    through arrives at the live thing, not a static snapshot."""
    safe = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{safe(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta property="og:title" content="{safe(title)}">
<meta property="og:description" content="{safe(description)}">
<meta property="og:image" content="{safe(og_image_url)}">
<meta property="og:url" content="{safe(canonical_url)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Voter Pulse · narve.ai">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{safe(title)}">
<meta name="twitter:description" content="{safe(description)}">
<meta name="twitter:image" content="{safe(og_image_url)}">
<style>
  body {{ margin:0; background:#0e1117; color:#e6edf3;
          font:16px/1.5 -apple-system, BlinkMacSystemFont, sans-serif;
          display:flex; flex-direction:column; align-items:center; min-height:100vh; padding:48px 24px; }}
  img {{ max-width:100%; width:1000px; border:1px solid #30363d; border-radius:8px; display:block; }}
  h1 {{ font-size:20px; margin:0 0 8px; }}
  p  {{ color:#8b949e; margin:0 0 16px; text-align:center; max-width:680px; }}
  a  {{ color:#ec4899; text-decoration:none; padding:10px 18px; border:1px solid #ec4899; border-radius:6px; margin-top:24px; }}
  a:hover {{ background:rgba(236,72,153,0.1); }}
</style>
</head>
<body>
  <h1>{safe(title)}</h1>
  <p>{safe(description)}</p>
  <img src="{safe(og_image_url)}" alt="{safe(title)}">
  <a href="/">Open the live dashboard &rarr;</a>
</body>
</html>
"""

"""Open Graph share-card renderer (SVG).

Generates 1200×630 SVG cards for /api/og/<iso>.svg endpoints. SVG is used
deliberately instead of PNG so the dashboard ships with zero binary-image
dependencies (no Pillow); platforms that don't support SVG as og:image
(notably Facebook) just fall back to no rich preview, but the share link
still works.

For platforms that need PNG: run any svg→png converter in the operator's
ingress (e.g. cloudflare-resvg, sharp). Recommended in production but not
required to ship.
"""

from __future__ import annotations

from xml.sax.saxutils import escape


def _iso2_flag(iso2: str | None) -> str:
    if not iso2 or len(iso2) != 2:
        return "🏳"
    a = ord(iso2[0].upper()) - ord("A") + 0x1F1E6
    b = ord(iso2[1].upper()) - ord("A") + 0x1F1E6
    return chr(a) + chr(b)


def _fmt(v, d: int = 1) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.{d}f}"
    except (TypeError, ValueError):
        return "—"


TIER_LABELS = {"H": "High income", "UM": "Upper-middle income",
               "LM": "Lower-middle income", "L": "Low income"}

# ---- card primitives ------------------------------------------------------

W, H = 1200, 630
BG = "#0b1220"
PANEL = "#0f172a"
BORDER = "#243042"
TEXT_1 = "#f1f5f9"
TEXT_2 = "#94a3b8"
TEXT_3 = "#64748b"
ACCENT = "#f472b6"

FONT_STACK = "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif"


def _shell(inner: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="{FONT_STACK}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{PANEL}"/>
      <stop offset="100%" stop-color="{BG}"/>
    </linearGradient>
  </defs>
  <rect width="{W}" height="{H}" fill="url(#bg)"/>
  <rect x="40" y="40" width="{W - 80}" height="{H - 80}" rx="20" fill="{PANEL}" stroke="{BORDER}"/>
  {inner}
  <g font-size="20" fill="{TEXT_3}">
    <text x="70" y="{H - 70}">♥ State of Love · narve.ai</text>
    <text x="{W - 70}" y="{H - 70}" text-anchor="end">love.narve.ai/api/og</text>
  </g>
</svg>"""


def _subscore_bar(x: int, y: int, w: int, label: str, value):
    pct = max(0.0, min(100.0, float(value or 0)))
    fill_w = int((pct / 100.0) * w)
    val_str = _fmt(value, 0) if value is not None else "—"
    return f"""<g>
  <text x="{x}" y="{y - 8}" font-size="16" fill="{TEXT_3}" letter-spacing="2">{escape(label.upper())}</text>
  <rect x="{x}" y="{y}" width="{w}" height="10" rx="5" fill="#1e293b"/>
  <rect x="{x}" y="{y}" width="{fill_w}" height="10" rx="5" fill="{ACCENT}"/>
  <text x="{x + w + 16}" y="{y + 12}" font-size="22" font-weight="700" fill="{TEXT_1}" font-variant-numeric="tabular-nums">{escape(val_str)}</text>
</g>"""


# ---- country card --------------------------------------------------------

def render_country_card(country: dict) -> bytes:
    name = country.get("name") or country.get("iso3") or "—"
    iso2 = country.get("iso2") or ""
    iso3 = country.get("iso3") or ""
    composite = country.get("composite")
    tier = TIER_LABELS.get(country.get("income_tier") or "", "")
    region = country.get("region") or ""
    subs = country.get("subscores") or {}

    flag = _iso2_flag(iso2)
    composite_str = _fmt(composite, 1)
    meta_str = " · ".join(filter(None, [region, tier, f"ISO {iso3}"]))

    bars = ""
    for i, (k, label) in enumerate([("connection","Connection"),
                                     ("partnership","Partnership"),
                                     ("stability","Stability"),
                                     ("activity","Activity")]):
        y = 360 + i * 56
        bars += _subscore_bar(80, y, 560, label, subs.get(k))

    inner = f"""
  <g>
    <text x="70" y="120" font-size="56">{escape(flag)}</text>
    <text x="160" y="120" font-size="56" font-weight="700" fill="{TEXT_1}">{escape(name)}</text>
    <text x="70" y="170" font-size="22" fill="{TEXT_2}">{escape(meta_str)}</text>
  </g>
  <g>
    <text x="70" y="270" font-size="20" letter-spacing="2" fill="{TEXT_3}">LOVE INDEX</text>
    <text x="70" y="345" font-size="120" font-weight="700" fill="{ACCENT}" font-variant-numeric="tabular-nums">{escape(composite_str)}</text>
    <text x="280" y="345" font-size="22" fill="{TEXT_3}">/100</text>
  </g>
  {bars}
  <g>
    <text x="{W - 80}" y="270" text-anchor="end" font-size="20" letter-spacing="2" fill="{TEXT_3}">SUBSCORES</text>
  </g>"""

    return _shell(inner).encode("utf-8")


# ---- global card ---------------------------------------------------------

def render_global_card(summary: dict) -> bytes:
    composite = summary.get("global_index")
    n = summary.get("n_countries") or 0
    n_meta = summary.get("n_meta") or 0
    as_of = summary.get("as_of") or "—"
    subs_avg = summary.get("subscores_avg") or {}

    composite_str = _fmt(composite, 1)
    coverage = f"{n} of {n_meta} countries ranked · as of {as_of}"

    bars = ""
    for i, (k, label) in enumerate([("connection","Connection"),
                                     ("partnership","Partnership"),
                                     ("stability","Stability"),
                                     ("activity","Activity")]):
        y = 360 + i * 56
        bars += _subscore_bar(80, y, 560, label, subs_avg.get(k))

    inner = f"""
  <g>
    <text x="70" y="120" font-size="56" font-weight="700" fill="{TEXT_1}">♥ Global State of Love</text>
    <text x="70" y="170" font-size="22" fill="{TEXT_2}">{escape(coverage)}</text>
  </g>
  <g>
    <text x="70" y="270" font-size="20" letter-spacing="2" fill="{TEXT_3}">WORLD AVERAGE</text>
    <text x="70" y="345" font-size="120" font-weight="700" fill="{ACCENT}" font-variant-numeric="tabular-nums">{escape(composite_str)}</text>
    <text x="280" y="345" font-size="22" fill="{TEXT_3}">/100</text>
  </g>
  {bars}
  <g>
    <text x="{W - 80}" y="270" text-anchor="end" font-size="20" letter-spacing="2" fill="{TEXT_3}">SUBSCORE AVERAGES</text>
  </g>"""

    return _shell(inner).encode("utf-8")

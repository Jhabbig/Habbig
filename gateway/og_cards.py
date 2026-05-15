"""Open Graph card generator.

Renders 1200×630 PNG social cards on demand using Pillow. Cards are
monochrome to match the narve.ai aesthetic and cached in-memory so they
aren't regenerated on every request.

Relies only on fonts bundled with Pillow (DejaVu Sans) so no extra
font files need to ship with the repo. System-level fonts are attempted
first where available.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

# Pillow decompression-bomb guard (audit MED, 2026-05-15). Pillow's
# default ``MAX_IMAGE_PIXELS`` (~89M) only warns, not raises. The
# ``_paste_logo`` helper below opens ``LOGO_PATH`` straight off disk —
# if the static logo asset is ever swapped for an attacker-controlled
# PNG (e.g. via a future user-uploaded brand mark), a crafted file
# inside a small envelope could balloon to multi-GB on decode and OOM
# the worker. 16M pixels = 4096×4096, well above the 44px target size
# we resize to. Matches the cap set in ``profile_routes.py`` for
# avatars — both code paths now trip a hard
# ``Image.DecompressionBombError`` on anything larger.
Image.MAX_IMAGE_PIXELS = 16_000_000

log = logging.getLogger("gateway.og_cards")

CARD_W, CARD_H = 1200, 630
PAD = 72
BG = (13, 13, 13)
FG = (255, 255, 255)
MUTED = (170, 170, 170)
ACCENT = (255, 255, 255)

STATIC_DIR = Path(__file__).parent / "static"
LOGO_PATH = STATIC_DIR / "img" / "logo.png"


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Try a couple of common system fonts, then fall back to Pillow's DejaVu."""
    candidates = []
    if bold:
        candidates += [
            "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "DejaVuSans-Bold.ttf",
        ]
    else:
        candidates += [
            "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "DejaVuSans.ttf",
        ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_w: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """Greedy word-wrap that respects the pixel width of the font."""
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    line = words[0]
    for word in words[1:]:
        candidate = f"{line} {word}"
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_w:
            line = candidate
        else:
            lines.append(line)
            line = word
    lines.append(line)
    return lines


def _draw_logo_mark(draw: ImageDraw.ImageDraw, x: int, y: int) -> None:
    """Draw a small monochrome wordmark — used when the PNG logo is missing."""
    font = _load_font(28, bold=True)
    draw.text((x, y), "narve.ai", fill=FG, font=font)


def _paste_logo(img: Image.Image, x: int, y: int, size: int = 44) -> None:
    """Paste the logo PNG if present, falling back to a wordmark."""
    if not LOGO_PATH.exists():
        _draw_logo_mark(ImageDraw.Draw(img), x, y + 10)
        return
    try:
        logo = Image.open(LOGO_PATH).convert("RGBA")
        ratio = size / max(logo.size)
        new_size = (max(1, int(logo.size[0] * ratio)), max(1, int(logo.size[1] * ratio)))
        logo = logo.resize(new_size, Image.LANCZOS)
        # Invert to white for the dark card background.
        rgba = logo.load()
        for j in range(logo.height):
            for i in range(logo.width):
                r, g, b, a = rgba[i, j]
                rgba[i, j] = (255 - r, 255 - g, 255 - b, a)
        img.paste(logo, (x, y), logo)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Failed to paste logo onto OG card: %s", exc)
        _draw_logo_mark(ImageDraw.Draw(img), x + 60, y + 10)


def _render(
    *,
    eyebrow: str,
    heading: str,
    stat_value: Optional[str] = None,
    stat_label: Optional[str] = None,
    footer: Optional[str] = None,
) -> bytes:
    """Core renderer used by all card types."""
    img = Image.new("RGB", (CARD_W, CARD_H), BG)
    draw = ImageDraw.Draw(img)

    # Logo + wordmark top-left
    _paste_logo(img, PAD, PAD)
    wordmark_font = _load_font(28, bold=True)
    draw.text((PAD + 56, PAD + 6), "narve.ai", fill=FG, font=wordmark_font)

    # Eyebrow
    eyebrow_font = _load_font(18, bold=True)
    draw.text((PAD, PAD + 90), eyebrow.upper(), fill=MUTED, font=eyebrow_font)

    # Heading — wrap to fit width
    heading_font = _load_font(64, bold=True)
    max_w = CARD_W - 2 * PAD
    lines = _wrap(heading, heading_font, max_w, draw)
    y = PAD + 130
    for line in lines[:3]:  # cap at 3 lines
        draw.text((PAD, y), line, fill=FG, font=heading_font)
        y += 78

    # Optional big stat (source profiles)
    if stat_value:
        stat_font = _load_font(200, bold=True)
        draw.text((PAD, CARD_H - 290), stat_value, fill=FG, font=stat_font)
        if stat_label:
            label_font = _load_font(20, bold=True)
            draw.text((PAD, CARD_H - 110), stat_label.upper(), fill=MUTED, font=label_font)

    # Footer
    if footer:
        footer_font = _load_font(18)
        draw.text((PAD, CARD_H - PAD - 10), footer, fill=MUTED, font=footer_font)

    # Corner mark bottom-right
    domain_font = _load_font(18, bold=True)
    bbox = draw.textbbox((0, 0), "narve.ai", font=domain_font)
    draw.text(
        (CARD_W - PAD - (bbox[2] - bbox[0]), CARD_H - PAD - 10),
        "narve.ai",
        fill=FG,
        font=domain_font,
    )

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── Card variants ────────────────────────────────────────────────────────────


def default_card() -> bytes:
    return _render(
        eyebrow="Prediction market intelligence",
        heading="Credibility-scored signals for serious Polymarket traders.",
        footer="narve.ai — invite only",
    )


def pricing_card() -> bytes:
    return _render(
        eyebrow="Pricing",
        heading="Plans from £75/month. Invite only.",
        footer="Six dashboards · live signals · market edge",
    )


def calendar_card() -> bytes:
    return _render(
        eyebrow="Market resolution calendar",
        heading="Upcoming prediction market resolutions, ranked by edge.",
        footer="Updated live from Polymarket and Kalshi",
    )


def source_card(handle: str, credibility: Optional[float], accuracy: Optional[float], count: int) -> bytes:
    value = f"{credibility:.2f}" if credibility is not None else "—"
    if accuracy is not None:
        footer = f"{accuracy * 100:.0f}% accuracy · {count} tracked predictions"
    elif count:
        footer = f"{count} tracked predictions"
    else:
        footer = "Credibility-scored source"
    return _render(
        eyebrow=f"@{handle}",
        heading="Credibility-scored prediction source",
        stat_value=value,
        stat_label="Credibility score",
        footer=footer,
    )


def market_card(
    title: str,
    *,
    market_price: Optional[float],
    narve_price: Optional[float],
    platform: str,
) -> bytes:
    market_pct = f"{market_price * 100:.0f}%" if market_price is not None else "—"
    narve_pct = f"{narve_price * 100:.0f}%" if narve_price is not None else "—"
    edge_txt = ""
    if market_price is not None and narve_price is not None:
        edge = (narve_price - market_price) * 100
        sign = "+" if edge >= 0 else ""
        edge_txt = f"{sign}{edge:.0f}pp edge · "
    footer = f"{edge_txt}Market {market_pct} vs narve.ai {narve_pct} · {platform}"
    return _render(
        eyebrow="Market",
        heading=title,
        footer=footer,
    )


# ── Share card renderers (feature: shareable artifacts) ─────────────────────
#
# These wrap the existing _render primitive with copy that's tuned for the
# invite-gated share destinations. The renderers stay here (rather than in
# routes_sharing.py) so caching semantics are uniform across every OG card
# the gateway serves.


def render_shared_market_card(
    *, market_slug: str, sharer_handle: str,
) -> bytes:
    """Social card for /s/m/{token}. Intentionally sparse — the full
    market question may be long, so we key off the slug and trust the
    social platform's title line to carry the rest. Sharer attribution
    only renders if the sharer opted into a public handle."""
    attribution = f"Shared by @{sharer_handle}" if sharer_handle else "Shared via narve.ai"
    return _render(
        eyebrow="narve.ai · market signal",
        heading=market_slug.replace("-", " ").title(),
        footer=f"{attribution} · invite-only",
    )


def render_shared_source_card(
    *, source_handle: str, sharer_handle: str,
) -> bytes:
    """Social card for /s/s/{token}. The credibility number isn't in the
    signed token (it moves over time — a 4-week-old link would render a
    stale score), so the card just leads with the handle and routes the
    reader into the invite path for the live number."""
    attribution = f"Shared by @{sharer_handle}" if sharer_handle else "Shared via narve.ai"
    return _render(
        eyebrow="narve.ai · source profile",
        heading=f"@{source_handle}",
        footer=f"{attribution} · credibility-scored · invite-only",
    )


def render_shared_prediction_card(
    *, user_prediction_id: int, sharer_handle: str,
) -> bytes:
    """Social card for /s/p/{token}. Called only for resolved-correct
    predictions (the db_sharing.create_shared_prediction invariant).
    The ``resolved correct`` eyebrow is the whole value prop — someone
    calling their shot right is worth a card."""
    attribution = f"@{sharer_handle}" if sharer_handle else "A narve.ai user"
    return _render(
        eyebrow="resolved correct",
        heading=f"{attribution} called it.",
        footer="Track your own accuracy on narve.ai — invite-only",
    )


# ── Cache shim ───────────────────────────────────────────────────────────────
#
# Delegates to the process-wide TTL cache (cache/ttl.py). Keys are namespaced
# under `og_card:*` so /admin/cache can attribute hits per-prefix. Cards are
# ~30–80 KB PNGs — keep them out of SQLite and off the render path.


def cached(key: str, ttl_seconds: int, factory) -> bytes:
    """Return cached bytes for ``key`` or compute via ``factory()``.

    Callers pass unprefixed keys like ``"default"``, ``"source:sho"``,
    ``"market:foo"``. We prefix with ``og_card:`` so the cache namespace
    matches the invalidation helpers and admin stats.
    """
    from cache import ttl_cache  # local import to sidestep circular refs
    full_key = f"og_card:{key}"
    return ttl_cache.get_or_compute(full_key, factory, ttl_seconds)


def clear_cache() -> None:
    """Test hook — drops every og_card:* entry."""
    from cache import ttl_cache
    ttl_cache.delete_prefix("og_card:")

"""Shared formatters for Telegram + Discord bot output.

Telegram and Discord each have their own flavour of Markdown plus some
native "rich" primitives (Discord embeds, Telegram inline keyboards).
Rather than branch formatting inside each bot handler, we centralise:

* ``format_best_bet_telegram(bet)``    → Markdown V2 string
* ``format_best_bet_discord(bet)``     → discord.Embed kwargs dict

Callers turn the discord dict into an ``discord.Embed`` at send time.
Keeping ``formatters.py`` free of the discord.py import means it works
in dev/test without the (heavy) dep installed.

The ``bet`` input shape is what the best-bets pipeline emits:

    {
      "market_slug": str,
      "question": str,
      "platform": "polymarket" | "kalshi",
      "side": "yes" | "no",
      "betyc_probability": float,
      "market_price": float,
      "edge_pct": float,
      "confidence": "high" | "medium" | "low",
      "credibility_avg": float,
      "source_count": int,
      "top_sources": [{"handle": str, "credibility": float}],
      "category": str | None,
    }
"""

from __future__ import annotations

from typing import Any


# Discord colors — hex for narve's palette.
_COLOR_HIGH_EDGE = 0x7FE29A
_COLOR_MEDIUM_EDGE = 0xF0C27B
_COLOR_LOW_EDGE = 0x9AA0A6


def _pct(x: Any, digits: int = 1) -> str:
    if x is None:
        return "—"
    return f"{float(x) * 100:.{digits}f}%"


def _edge_pct(x: Any, digits: int = 1) -> str:
    if x is None:
        return "—"
    return f"{float(x):+.{digits}f}%"


def _narve_url(slug: str) -> str:
    return f"https://narve.ai/markets/{slug}"


# ── Telegram ────────────────────────────────────────────────────────────────


_TELEGRAM_ESCAPE = str.maketrans({
    "_": r"\_", "*": r"\*", "[": r"\[", "]": r"\]", "(": r"\(",
    ")": r"\)", "~": r"\~", "`": r"\`", ">": r"\>", "#": r"\#",
    "+": r"\+", "-": r"\-", "=": r"\=", "|": r"\|", "{": r"\{",
    "}": r"\}", ".": r"\.", "!": r"\!",
})


def _tg(s: Any) -> str:
    return str(s or "").translate(_TELEGRAM_ESCAPE)


def format_best_bet_telegram(bet: dict) -> str:
    """Format a best-bet for Telegram MarkdownV2.

    The leading emoji uses plain unicode (no flag emoji) so every
    Telegram client renders it. Source list is truncated to 3 to keep
    alert density reasonable.
    """
    edge = bet.get("edge_pct")
    header = "🎯" if (edge or 0) >= 10 else "📈"
    side = (bet.get("side") or "").upper()
    prob = _pct(bet.get("betyc_probability"))
    market = _pct(bet.get("market_price"))
    conf = bet.get("confidence") or "—"
    sources = bet.get("top_sources") or []
    src_line = ""
    if sources:
        rendered = [f"@{_tg(s['handle'])} \\({int((s.get('credibility') or 0) * 100)}\\)"
                    for s in sources[:3]]
        src_line = f"\n*Sources:* {_tg(' · ').join(rendered) if False else ' · '.join(rendered)}"
    url = _narve_url(bet.get("market_slug", ""))
    category = bet.get("category") or ""
    return (
        f"{header} *{_tg(bet.get('question') or 'Market')}*\n"
        f"Side: *{_tg(side)}*  \\|  Edge: *{_tg(_edge_pct(edge))}*  \\|  "
        f"Confidence: *{_tg(conf)}*\n"
        f"narve YES: *{_tg(prob)}*   Market YES: *{_tg(market)}*\n"
        f"Category: {_tg(category)}   Sources: {int(bet.get('source_count') or 0)}"
        f"{src_line}\n"
        f"[Open on narve\\.ai]({_tg(url)})"
    )


# ── Discord ─────────────────────────────────────────────────────────────────


def format_best_bet_discord(bet: dict) -> dict:
    """Return a dict the bot turns into a ``discord.Embed`` at send time.

    Shape matches the Embed constructor's kwargs so the bot can do
    ``discord.Embed(**format_best_bet_discord(bet))``.
    """
    edge = bet.get("edge_pct")
    if (edge or 0) >= 10:
        color = _COLOR_HIGH_EDGE
    elif (edge or 0) >= 5:
        color = _COLOR_MEDIUM_EDGE
    else:
        color = _COLOR_LOW_EDGE
    side = (bet.get("side") or "").upper()
    category = bet.get("category") or "market"
    fields = [
        {"name": "Side", "value": side or "—", "inline": True},
        {"name": "Edge", "value": _edge_pct(edge), "inline": True},
        {"name": "Confidence", "value": bet.get("confidence") or "—", "inline": True},
        {"name": "narve YES", "value": _pct(bet.get("betyc_probability")), "inline": True},
        {"name": "Market YES", "value": _pct(bet.get("market_price")), "inline": True},
        {"name": "Sources", "value": str(int(bet.get("source_count") or 0)), "inline": True},
    ]
    sources = bet.get("top_sources") or []
    if sources:
        rendered = "  ·  ".join(
            f"@{s['handle']} ({int((s.get('credibility') or 0) * 100)})"
            for s in sources[:3]
        )
        fields.append({"name": "Top sources", "value": rendered, "inline": False})
    return {
        "title": (bet.get("question") or "Market")[:256],
        "url": _narve_url(bet.get("market_slug", "")),
        "description": f"**{category.title()}** · narve.ai signal",
        "color": color,
        "fields": fields,
    }


# ── Best-bet loader (shared fetcher) ────────────────────────────────────────


def load_best_bets(limit: int = 5, min_ev: float = 0.05,
                   min_cred: float = 0.6) -> list[dict]:
    """Load best bets from the gateway DB.

    Best-effort: if the expected helper isn't present on ``db`` we
    return an empty list so the bots don't crash. Bot send jobs fall
    back to "no fresh bets" messaging in that case.
    """
    try:
        import sys, os
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(here)
        sys.path.insert(0, os.path.join(root, "gateway"))
        import db  # type: ignore[import]
    except Exception:
        return []
    fn = getattr(db, "list_best_bets", None) or getattr(db, "best_bets", None)
    if not callable(fn):
        return []
    try:
        rows = fn(limit=limit, min_ev=min_ev, min_cred=min_cred)
        # Normalise to plain dicts for the formatters.
        return [dict(r) for r in rows]
    except TypeError:
        try:
            return [dict(r) for r in fn(limit)]
        except Exception:
            return []
    except Exception:
        return []

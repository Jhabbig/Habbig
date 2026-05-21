from __future__ import annotations
"""Outbound webhook delivery for big movements.

Subscribes to the same per-cycle data the alert worker uses, and posts to
configured URLs when a race moves more than ``threshold_pp`` since the
last fire. Dedup is per (webhook_id, race_key) so a slowly-trending race
doesn't fire on every cycle — we only re-fire when the delta grows by
another threshold or reverses past the previous fire.

Three target formats — pick the one that matches the destination:

  generic  — our canonical JSON. Use this for custom services / RSS-ish.
  slack    — Slack incoming-webhook block format. Drop into a #channel.
  discord  — Discord webhook with embed. Drop into a server channel.

Failures are logged on the row (last_error / last_status); we never
retry inline — the next cycle picks up the next big move.
"""

import asyncio
import json
import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


def _public_url(race_key: str) -> str:
    base = os.getenv("PUBLIC_BASE_URL", "https://midterm.narve.ai").rstrip("/")
    return f"{base}/race/{race_key}"


def format_generic(*, race_key: str, race_title: str, source: str, delta_pp: float,
                   from_prob: float, to_prob: float) -> dict:
    """Canonical shape — recipient can rename or render however."""
    return {
        "type": "midtermedge.movement",
        "race_key": race_key,
        "race_title": race_title,
        "source": source,
        "from_pct": round(from_prob * 100, 1),
        "to_pct": round(to_prob * 100, 1),
        "delta_pp": round(delta_pp, 1),
        "direction": "up" if delta_pp >= 0 else "down",
        "url": _public_url(race_key),
    }


def format_slack(*, race_key: str, race_title: str, source: str, delta_pp: float,
                 from_prob: float, to_prob: float) -> dict:
    """Slack incoming-webhook payload. Uses block kit for nicer rendering."""
    direction = "▲" if delta_pp >= 0 else "▼"
    color = "good" if delta_pp >= 0 else "danger"
    url = _public_url(race_key)
    return {
        "text": f"{race_title}: {source} moved {delta_pp:+.1f}pp",
        "attachments": [{
            "color": color,
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*<{url}|{race_title}>*\n"
                                f"{source}: {from_prob * 100:.0f}% → {to_prob * 100:.0f}% "
                                f"{direction} *{abs(delta_pp):.1f}pp*",
                    },
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"<{url}|View on MidtermEdge →>"}],
                },
            ],
        }],
    }


def format_discord(*, race_key: str, race_title: str, source: str, delta_pp: float,
                   from_prob: float, to_prob: float) -> dict:
    """Discord webhook payload with an embed."""
    direction = "📈" if delta_pp >= 0 else "📉"
    color = 0x10B981 if delta_pp >= 0 else 0xEF4444
    url = _public_url(race_key)
    return {
        "content": None,
        "embeds": [{
            "title": f"{direction} {race_title}",
            "url": url,
            "color": color,
            "description": f"**{source}** moved {delta_pp:+.1f}pp "
                           f"({from_prob * 100:.0f}% → {to_prob * 100:.0f}%)",
            "footer": {"text": "MidtermEdge"},
        }],
    }


FORMATTERS = {
    "generic": format_generic,
    "slack": format_slack,
    "discord": format_discord,
}


async def deliver(session: aiohttp.ClientSession, url: str, payload: dict, *, timeout: float = 8.0) -> tuple[bool, str]:
    """POST the payload. Returns (success, error_or_status)."""
    try:
        async with session.post(
            url, json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"User-Agent": "MidtermEdge-Webhook/1.0"},
        ) as resp:
            body = await resp.text()
            if 200 <= resp.status < 300:
                return True, f"{resp.status}"
            return False, f"HTTP {resp.status}: {body[:200]}"
    except asyncio.TimeoutError:
        return False, "timeout"
    except aiohttp.ClientError as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"
    except Exception as e:
        return False, f"unexpected: {str(e)[:200]}"


def should_fire(*, wm: dict | None, delta_pp: float, threshold_pp: float) -> bool:
    """Threshold-crossing dedup logic.

    Fire if EITHER:
      - |delta| >= threshold AND no prior fire (or movement has grown by another threshold since last fire), OR
      - There's a prior fire AND |delta - prev| >= threshold (reversal or
        big swing from the last reported value — even if the new absolute
        delta is small, the *change in narrative* is significant)
    """
    abs_delta = abs(delta_pp)
    prev = (wm or {}).get("last_delta_pp")

    # Reversal / big swing from prior fire — always interesting
    if prev is not None and abs(delta_pp - prev) >= threshold_pp:
        return True

    # First time we're seeing this race, or no prior baseline
    if not wm or prev is None:
        return abs_delta >= threshold_pp

    # Prior fire exists but swing is small — no re-fire
    return False


async def run_webhook_cycle(
    db, session: aiohttp.ClientSession,
    top_by_race: dict[str, dict[str, float]],
    race_titles: dict[str, str],
    movement_history: dict[str, dict[str, list[float]]] | None = None,
) -> int:
    """Process all enabled webhooks against the latest movements.

    ``top_by_race`` is what the alert worker already computes: race_key →
    {source: top_probability}. We use the divergence-history table for the
    delta calculation — same source of truth as the alert worker.

    Returns the count of webhook fires this cycle.
    """
    webhooks = db.get_webhooks(enabled_only=True)
    if not webhooks:
        return 0

    fires = 0
    for wh in webhooks:
        rt_filter = (wh.get("race_type_filter") or "").strip().lower()
        st_filter = (wh.get("state_filter") or "").strip().upper()
        threshold_pp = float(wh.get("threshold_pp") or 5.0)
        fmt_name = (wh.get("format") or "generic").lower()
        formatter = FORMATTERS.get(fmt_name, format_generic)

        for race_key, source_probs in top_by_race.items():
            # Optional per-webhook filters
            if rt_filter and not race_key.startswith(rt_filter + "_"):
                continue
            if st_filter and st_filter not in race_key.upper():
                continue

            # Compute delta from the divergence history (24h window default)
            hist = db.get_divergence_history(race_key=race_key, days=2)
            if len(hist) < 2:
                continue
            hist = sorted(hist, key=lambda h: h.get("snapshot_time") or "")

            # Use the source with the largest absolute move
            best_source = None
            best_delta = 0.0
            best_from = 0.0
            best_to = 0.0
            # Map source name → snapshot column. Local copy so webhooks.py
            # has no import cycle with main.
            _COLS = {
                "polymarket": "polymarket_prob", "kalshi": "kalshi_prob",
                "predictit": "predictit_prob", "polling": "polling_avg",
                "manifold": "manifold_prob", "metaculus": "metaculus_prob",
            }
            for src, col in _COLS.items():
                vals = [h.get(col) for h in hist if h.get(col) is not None]
                if len(vals) < 2:
                    continue
                delta = (vals[-1] - vals[0]) * 100
                if abs(delta) > abs(best_delta):
                    best_source = src
                    best_delta = delta
                    best_from = vals[0]
                    best_to = vals[-1]

            if not best_source:
                continue

            wm = db.get_webhook_dedup(wh["id"], race_key)
            if not should_fire(wm=wm, delta_pp=best_delta, threshold_pp=threshold_pp):
                continue

            payload = formatter(
                race_key=race_key,
                race_title=race_titles.get(race_key, race_key),
                source=best_source,
                delta_pp=best_delta,
                from_prob=best_from,
                to_prob=best_to,
            )
            ok, info = await deliver(session, wh["url"], payload)
            if ok:
                db.record_webhook_fired(wh["id"], race_key, best_delta)
                fires += 1
                logger.info(f"Webhook {wh['id']} fired for {race_key} ({best_delta:+.1f}pp)")
            else:
                db.record_webhook_error(wh["id"], info)
                logger.warning(f"Webhook {wh['id']} delivery failed: {info}")

    return fires

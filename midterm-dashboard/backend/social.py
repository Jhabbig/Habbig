"""Social auto-poster.

When a race moves materially AND we have a grounded LLM explanation in
cache, format a tweet-sized post and ship it to every "social"-format
outbound webhook the admin has configured. Same pipeline as the
real-time Slack/Discord webhooks (``webhooks.py``) — but with longer
dedup (24h per race instead of per-cycle) and a character-budget-aware
formatter.

Decoupled from the actual social platform: an admin wires their webhook
to Zapier / IFTTT / n8n / Make / a tiny self-hosted bridge that handles
the X/Twitter/Bluesky/Mastodon API call. We never touch those APIs
directly — that way we don't need to manage OAuth tokens for every
platform, and platform changes don't break us.

Two complementary firing modes:
  - **Auto** — background loop scans every 30 min for big moves with
    a cached explanation
  - **Manual** — admin can request a post for any race + window via
    POST /admin/social/post (useful for narrating a story that the
    automatic threshold missed)

Both go through the same delivery + dedup path.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# Per-race dedup window — once we post about senate_GA, we wait this
# long before posting about senate_GA again, even if it moves more.
DEDUP_HOURS = 24

# Tweet-style character budget. X allows 280; we leave room for the URL
# (~25 chars after t.co shortening) + safety margin.
MAX_BODY_CHARS = 240


def _public_url(race_key: str) -> str:
    base = os.getenv("PUBLIC_BASE_URL", "https://midterm.narve.ai").rstrip("/")
    return f"{base}/race/{race_key}"


def format_post(
    *, race_title: str, race_key: str, source: str, delta_pp: float,
    from_prob: float, to_prob: float,
    explanation_summary: str | None = None,
) -> str:
    """Tweet-sized text + the deep link.

    Composes::

        {race_title}: {source} {↑/↓}{delta}pp ({from}% → {to}%)
        Why: {summary}
        {url}

    Each section is trimmed to fit MAX_BODY_CHARS. If no explanation is
    available the "Why:" line is omitted entirely rather than left empty.
    """
    direction = "↑" if delta_pp >= 0 else "↓"
    head = (
        f"{race_title}: {source} {direction}{abs(delta_pp):.1f}pp "
        f"({from_prob * 100:.0f}% → {to_prob * 100:.0f}%)"
    )
    url = _public_url(race_key)
    parts = [head]
    if explanation_summary:
        summary = explanation_summary.strip().replace("\n", " ")
        # Leave ~60 chars headroom for the URL on the next line
        cap = MAX_BODY_CHARS - len(head) - 8  # "Why: \n\n"
        if cap > 30:
            if len(summary) > cap:
                summary = summary[: cap - 1].rstrip() + "…"
            parts.append(f"Why: {summary}")
    parts.append(url)
    text = "\n".join(parts)
    # Belt-and-suspenders cap — if a long URL pushed us over, trim head
    if len(text) > 280:
        text = text[:279] + "…"
    return text


async def deliver_to_webhook(
    session: aiohttp.ClientSession, url: str, payload: dict,
    *, timeout: float = 8.0,
) -> tuple[bool, str]:
    """POST the payload. Identical shape to webhooks.deliver but kept
    separate so changes to one path don't surprise the other."""
    try:
        async with session.post(
            url, json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"User-Agent": "MidtermEdge-Social/1.0"},
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


def post_payload(text: str, race_key: str) -> dict:
    """Canonical JSON sent to each social-format webhook. Tiny on purpose
    so Zapier / IFTTT can wire it straight to the platform's text field."""
    return {
        "type": "midtermedge.social_post",
        "race_key": race_key,
        "text": text,
        "url": _public_url(race_key),
    }


def _was_recently_posted(db, race_key: str) -> bool:
    """True if we posted about this race inside the dedup window."""
    last = db.last_social_post_at(race_key)
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    return (datetime.now(timezone.utc) - last_dt) < timedelta(hours=DEDUP_HOURS)


def _best_movement_for(
    db, race_key: str, *, hours: int,
    divergence_col_map: dict[str, str],
) -> dict | None:
    """Return ``{source, delta_pp, from, to}`` for the biggest move in the
    window, or None if nothing crossed the threshold."""
    days = max(1, hours // 24 + 1)
    hist = db.get_divergence_history(race_key=race_key, days=days)
    if len(hist) < 2:
        return None
    hist = sorted(hist, key=lambda h: h.get("snapshot_time") or "")
    best = None
    for src, col in divergence_col_map.items():
        vals = [h.get(col) for h in hist if h.get(col) is not None]
        if len(vals) < 2:
            continue
        delta_pp = (vals[-1] - vals[0]) * 100
        if best is None or abs(delta_pp) > abs(best["delta_pp"]):
            best = {
                "source": src, "delta_pp": delta_pp,
                "from": vals[0], "to": vals[-1],
            }
    return best


async def post_for_race(
    db, session: aiohttp.ClientSession,
    *, race_key: str, race_title: str,
    movement: dict, explanation_summary: str | None,
    webhook_urls: list[str],
) -> int:
    """Deliver one race's post to every configured social webhook URL.

    Returns the number of successful deliveries. Dedup logging happens
    here, not in the caller, so manual ``/admin/social/post`` and the
    background loop share the same path.
    """
    text = format_post(
        race_title=race_title, race_key=race_key,
        source=movement["source"], delta_pp=movement["delta_pp"],
        from_prob=movement["from"], to_prob=movement["to"],
        explanation_summary=explanation_summary,
    )
    payload = post_payload(text, race_key)
    delivered = 0
    for url in webhook_urls:
        ok, status = await deliver_to_webhook(session, url, payload)
        db.log_social_post(
            race_key=race_key, platform_url=url, text=text,
            status=status if ok else f"error:{status}",
            delta_pp=movement["delta_pp"], source=movement["source"],
        )
        if ok:
            delivered += 1
        else:
            logger.warning(f"Social post to {url} failed: {status}")
    return delivered

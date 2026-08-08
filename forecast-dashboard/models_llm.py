"""LLM forecaster (source='llm') — a Claude forecaster that gets SCORED.

For a capped daily selection of upcoming board events, asks Claude for an
independent calibrated probability (the market price is deliberately NOT
shown, so the calibration page can honestly compare llm vs market Brier).
Rows land in model_probs with source 'llm'; the rationale digest rides in
the detail JSON. Disabled unless FORECAST_LLM_ENABLED=1 (it spends API
tokens). Never emits trade advice — probabilities and factors only.

Contract (same as the other model modules): async compute(markets) -> rows
for db.insert_model_prob: {market_uid, source: 'llm', model_prob,
prob_method: 'llm_forecast', detail: <json str>}.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import anthropic

import db

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"
MAX_TOKENS = 2000
MAX_CONCURRENT = 3
DEFAULT_DAILY_CAP = 25
HORIZON_DAYS = 3

_VENUES = ("polymarket", "kalshi")

# Mirrors server._MICRO_RE (recurring intraday micro markets). Copied, not
# imported: model modules must never import the app spine.
_MICRO_RE = re.compile(
    r"\b(up or down|above .* at \d{1,2}:\d{2}\s*(AM|PM)|\d{1,2}:\d{2}\s*(AM|PM)\s*-\s*\d{1,2}:\d{2}\s*(AM|PM)"
    r"|map \d|total rounds|rounds handicap|games total|game handicap|\bo/u \d|over/under \d+\.5|1st half|first half|quarter \d)\b",
    re.IGNORECASE,
)

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "probability": {"type": "number"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "rationale": {"type": "string"},
        "key_factors": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["probability", "confidence", "rationale", "key_factors"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You are a calibrated superforecaster. Estimate the probability that the stated event resolves YES by its resolution time. "
    "Start from base rates for events of this kind, then adjust for current evidence — use web search when recent news could move the estimate. "
    "NEVER give trading or investment advice, and never mention buying or selling anything: your output is a probability and the factors behind it, nothing more. "
    "Respond with only the JSON object matching the required schema — no prose before or after it."
)

_auth_logged = False


def _log_auth_once(err: Exception) -> None:
    global _auth_logged
    if not _auth_logged:
        _auth_logged = True
        log.error("llm model: Anthropic auth unavailable (%s); skipping until resolved", err)


def has_credentials(client) -> bool:
    """False when the SDK constructed with zero credentials (it would then
    raise TypeError on every request, so callers abort up front). The
    sentinel default keeps duck-typed test doubles — which lack these
    attributes entirely — out of the gate. Shared with ask._make_client so
    the credentialless check lives in one place.
    """
    _missing = object()
    return not (getattr(client, "api_key", _missing) is None and getattr(client, "auth_token", _missing) is None and getattr(client, "_token_cache", _missing) is None)


def _daily_cap() -> int:
    try:
        return int(os.environ.get("FORECAST_LLM_DAILY_CAP", DEFAULT_DAILY_CAP))
    except ValueError:
        return DEFAULT_DAILY_CAP


def _uid(m: dict) -> str:
    return m.get("uid") or f"{m.get('venue')}:{m.get('venue_id')}"


def _parse_end(end_date: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat((end_date or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _eligible(markets: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=HORIZON_DAYS)
    out = []
    for m in markets:
        if (m.get("venue") or "").lower() not in _VENUES:
            continue
        if _MICRO_RE.search(m.get("question") or ""):
            continue
        end = _parse_end(m.get("end_date"))
        if end is None or end < now or end > cutoff:
            continue
        out.append(m)
    out.sort(key=lambda m: m.get("liquidity") or 0.0, reverse=True)
    return out


def _scored_today_uids() -> set[str]:
    day_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT market_uid FROM model_probs WHERE source = 'llm' AND ts >= ?",
            (day_start,),
        ).fetchall()
        return {r["market_uid"] for r in rows}
    finally:
        conn.close()


def stored_headlines(uid: str, limit: int = 5) -> list[dict]:
    """Stored headlines for a market via a fresh short-lived conn (same
    pattern as _scored_today_uids). Shared with ask's forecast path."""
    conn = db.get_conn()
    try:
        return db.news_for(conn, uid, limit)
    finally:
        conn.close()


def format_headlines(items: list[dict]) -> str | None:
    """'Recent headlines:' prompt block — title + source + date only. No
    URLs and no market prices, so the forecast stays independent of market
    pricing. Shared with ask's forecast path."""
    lines = []
    for it in (items or [])[:5]:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        date = (it.get("published") or (it.get("ts") or "")[:10] or "").strip()
        meta = ", ".join(x for x in ((it.get("source") or "").strip(), date) if x)
        lines.append(f"- {title}" + (f" ({meta})" if meta else ""))
    if not lines:
        return None
    return (
        "Recent headlines:\n"
        + "\n".join(lines)
        + "\nThe headlines above are stored context and may be incomplete — you may still use web search when newer or fuller information could move the estimate."
    )


async def _forecast_one(client, m: dict) -> dict | None:
    # The prompt deliberately excludes the market price/baseline so the
    # forecast is independent and the calibration comparison stays honest.
    user_content = f"Question: {m.get('question')}\nResolution time (UTC): {m.get('end_date')}\nToday's date: {datetime.now(timezone.utc).date().isoformat()}"
    block = format_headlines(await asyncio.to_thread(stored_headlines, _uid(m)))
    if block:
        user_content += "\n\n" + block
    request = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "tools": [WEB_SEARCH_TOOL],
        "output_config": {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    }
    messages: list[dict] = [{"role": "user", "content": user_content}]
    resp = await client.messages.create(messages=messages, **request)
    blocks = list(resp.content or [])
    if resp.stop_reason == "pause_turn":
        messages = messages + [{"role": "assistant", "content": resp.content}]
        resp = await client.messages.create(messages=messages, **request)
        blocks.extend(resp.content or [])
    if resp.stop_reason in ("refusal", "pause_turn"):
        log.info("llm model: %s skipped (stop_reason=%s)", _uid(m), resp.stop_reason)
        return None

    text = None
    searched = False
    for b in blocks:
        btype = getattr(b, "type", None)
        if btype in ("server_tool_use", "web_search_tool_result"):
            searched = True
        elif btype == "text":
            text = b.text
    if text is None:
        log.info("llm model: %s returned no text block; skipping", _uid(m))
        return None

    data = json.loads(text)
    prob = min(0.99, max(0.01, float(data["probability"])))
    return {
        "market_uid": _uid(m),
        "source": "llm",
        "model_prob": round(prob, 4),
        "prob_method": "llm_forecast",
        "detail": json.dumps(
            {
                "confidence": data.get("confidence"),
                "rationale": data.get("rationale"),
                "key_factors": data.get("key_factors"),
                "model": MODEL,
                "searched": searched,
            }
        ),
    }


async def compute(markets: list[dict]) -> list[dict]:
    if os.environ.get("FORECAST_LLM_ENABLED") != "1":
        return []

    candidates = _eligible(markets)
    if not candidates:
        return []

    scored_today = await asyncio.to_thread(_scored_today_uids)
    cap = _daily_cap()
    budget = cap - len(scored_today)
    if budget <= 0:
        log.info("llm model: daily cap (%d) reached; skipping this pass", cap)
        return []
    batch = [m for m in candidates if _uid(m) not in scored_today][:budget]
    if not batch:
        return []

    try:
        # No explicit key: the SDK resolves ANTHROPIC_API_KEY or an ant-auth profile.
        client = anthropic.AsyncAnthropic()
    except Exception as e:
        _log_auth_once(e)
        return []

    if not has_credentials(client):
        _log_auth_once(RuntimeError("no API key, auth token, or credential profile resolved"))
        return []

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    state = {"auth_failed": False}

    async def worker(m: dict) -> dict | None:
        async with sem:
            if state["auth_failed"]:
                return None
            try:
                return await _forecast_one(client, m)
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
                state["auth_failed"] = True
                _log_auth_once(e)
                return None
            except Exception as e:
                log.warning("llm model: %s failed: %s", _uid(m), e)
                return None

    results = await asyncio.gather(*(worker(m) for m in batch))
    return [r for r in results if r is not None]

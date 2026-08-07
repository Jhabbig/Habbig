"""Ask-a-question surface (backs GET /api/ask).

Contract: async ask(question) -> {
    question: str,      # echo of the (stripped) query
    matches: [dict],    # board-row-shaped dicts (same keys as /api/board rows)
                        # for existing markets whose question contains every
                        # whitespace-separated term of the query
    llm: dict | None,   # {probability, confidence, rationale, key_factors,
                        #  model, uid} — on-demand forecast for the question,
                        # logged under a venue='custom' market whose uid is
                        # returned here; None when no forecast was produced
    note: str,          # human-readable status (why llm is None, cache info,
                        # whether the outcome will be auto-checked)
}

Custom markets never get snapshots (a free-form question has no market
price), so they never appear on /api/board or in search matches — they live
via /api/ask and are settled by resolver.adjudicate_custom. When a deadline
was parsed from the question, a calibration row (source='llm',
market_prob=None) is logged so adjudication has something to score; with no
deadline the forecast is stored but cannot be tracked for accuracy.

Probabilities and factors only — never trading or investment advice.
"""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import anthropic

import db
import models_llm

log = logging.getLogger(__name__)

CACHE_HOURS = 6

# ── deadline parsing ─────────────────────────────────────────────────────────

_MONTHS: dict[str, int] = {}
for _i, _name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"),
    start=1,
):
    _MONTHS[_name] = _i
    _MONTHS[_name[:3]] = _i
_MONTHS["sept"] = 9

_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

_ISO_RE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
_MDY_RE = re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:\s*,?\s*(20\d{{2}}))?", re.IGNORECASE)
_DMY_RE = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_ALT})\b(?:\s*,?\s*(20\d{{2}}))?", re.IGNORECASE)
_MY_RE = re.compile(rf"\b({_MONTH_ALT})\.?\s+(20\d{{2}})\b", re.IGNORECASE)
# Month with no day/year needs a deadline-ish preposition so topical mentions
# ("the March jobs report") don't read as deadlines.
_MONTH_ONLY_RE = re.compile(rf"\b(?:by|in|before|until|till|during|this|next)\s+({_MONTH_ALT})\b", re.IGNORECASE)
_YEAR_END_RE = re.compile(r"\bthis year\b|\bend of (?:the )?year\b|\byear[- ]end\b", re.IGNORECASE)


def _fmt(y: int, mo: int, d: int) -> str:
    return f"{y:04d}-{mo:02d}-{d:02d}T23:59:59Z"


def _eod(y: int, mo: int, d: int) -> datetime:
    return datetime(y, mo, d, 23, 59, 59, tzinfo=timezone.utc)


def _next_occurrence(now: datetime, mo: int, d: int, y: int | None) -> str | None:
    try:
        if y is not None:
            _eod(y, mo, d)
            return _fmt(y, mo, d)
        if _eod(now.year, mo, d) >= now:
            return _fmt(now.year, mo, d)
        _eod(now.year + 1, mo, d)
        return _fmt(now.year + 1, mo, d)
    except ValueError:
        return None


def parse_deadline(question: str, now: datetime | None = None) -> str | None:
    """Best-effort deadline from free text -> end-of-day ISO Z, or None.

    Handles explicit dates (ISO, 'March 3, 2027', '3rd of March'), month+year,
    bare month after a deadline preposition ('by March' -> end of month, next
    occurrence), and relative phrases (today/tomorrow/this week/this month/...).
    Dates with no year roll forward to the next occurrence.
    """
    now = now or datetime.now(timezone.utc)

    m = _ISO_RE.search(question)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            _eod(y, mo, d)
        except ValueError:
            return None
        return _fmt(y, mo, d)

    m = _MDY_RE.search(question)
    if m:
        return _next_occurrence(now, _MONTHS[m.group(1).lower()], int(m.group(2)), int(m.group(3)) if m.group(3) else None)

    m = _DMY_RE.search(question)
    if m:
        return _next_occurrence(now, _MONTHS[m.group(2).lower()], int(m.group(1)), int(m.group(3)) if m.group(3) else None)

    m = _MY_RE.search(question)
    if m:
        mo, y = _MONTHS[m.group(1).lower()], int(m.group(2))
        return _fmt(y, mo, calendar.monthrange(y, mo)[1])

    m = _MONTH_ONLY_RE.search(question)
    if m:
        mo = _MONTHS[m.group(1).lower()]
        y = now.year
        if _eod(y, mo, calendar.monthrange(y, mo)[1]) < now:
            y += 1
        return _fmt(y, mo, calendar.monthrange(y, mo)[1])

    ql = question.lower()
    if re.search(r"\btoday\b", ql):
        return _fmt(now.year, now.month, now.day)
    if re.search(r"\btomorrow\b", ql):
        t = now + timedelta(days=1)
        return _fmt(t.year, t.month, t.day)
    if "this week" in ql or "next week" in ql:
        sunday = now + timedelta(days=6 - now.weekday())
        if "next week" in ql:
            sunday += timedelta(days=7)
        return _fmt(sunday.year, sunday.month, sunday.day)
    if "this month" in ql:
        return _fmt(now.year, now.month, calendar.monthrange(now.year, now.month)[1])
    if "next month" in ql:
        y, mo = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        return _fmt(y, mo, calendar.monthrange(y, mo)[1])
    if _YEAR_END_RE.search(ql):
        return _fmt(now.year, 12, 31)
    return None


# ── matches (board-row shaping) ──────────────────────────────────────────────


def _latest_model(model_probs: dict) -> tuple[str | None, dict | None]:
    latest_source, latest = None, None
    for source, mp in (model_probs or {}).items():
        if latest is None or (mp.get("ts") or "") > (latest.get("ts") or ""):
            latest_source, latest = source, mp
    return latest_source, latest


def _search_matches(question: str) -> list[dict]:
    conn = db.get_conn()
    try:
        rows = db.search_markets(conn, question)
        cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = []
        for r in rows:
            model_source, model = _latest_model(r.get("model_probs") or {})
            model_prob = model["model_prob"] if model else None
            prev = db.baseline_asof(conn, r["uid"], cutoff_24h)
            move_24h = None
            if prev is not None and r["baseline_prob"] is not None:
                move_24h = round(r["baseline_prob"] - prev, 4)
            tier = db.compute_baseline(r.get("yes_bid"), r.get("yes_ask"), r.get("last_price"), r.get("liquidity"))["tier"]
            out.append(
                {
                    "uid": r["uid"],
                    "venue": r["venue"],
                    "question": r["question"],
                    "category": r["category"],
                    "url": r["url"],
                    "end_date": r["end_date"],
                    "display_prob": model_prob if model_prob is not None else r["baseline_prob"],
                    "baseline_prob": r["baseline_prob"],
                    "model_source": model_source,
                    "model_prob": model_prob,
                    "move_24h": move_24h,
                    "new": prev is None,
                    "spread": r["spread"],
                    "confidence": tier,
                    "liquidity": r["liquidity"],
                    "volume_24h": r["volume_24h"],
                    # Cross-venue disagreement needs the full link map; the ask
                    # surface doesn't render it, so it stays None here.
                    "disagreement": None,
                    "resolved": r["resolved"],
                    "outcome": r["outcome"],
                    "resolved_at": r["resolved_at"],
                    "snapshot_ts": r["snapshot_ts"],
                }
            )
        return out
    finally:
        conn.close()


# ── Claude plumbing (shared with resolver.adjudicate_custom) ─────────────────


def _make_client():
    """AsyncAnthropic or None when no credentials resolve (models_llm sentinel)."""
    try:
        client = anthropic.AsyncAnthropic()
    except Exception as e:
        log.error("ask: Anthropic client construction failed: %s", e)
        return None
    if not models_llm.has_credentials(client):
        log.error("ask: no API key, auth token, or credential profile resolved")
        return None
    return client


async def _claude_json(client, system: str, user_content: str, schema: dict) -> tuple[dict | None, bool]:
    """One schema-constrained call with web search. -> (parsed dict | None, searched).

    None means refusal / unresolved pause_turn / no text block. Mirrors
    models_llm._forecast_one: a pause_turn is continued exactly once.
    """
    request = {
        "model": models_llm.MODEL,
        "max_tokens": models_llm.MAX_TOKENS,
        "system": system,
        "tools": [models_llm.WEB_SEARCH_TOOL],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    messages: list[dict] = [{"role": "user", "content": user_content}]
    resp = await client.messages.create(messages=messages, **request)
    blocks = list(resp.content or [])
    if resp.stop_reason == "pause_turn":
        messages = messages + [{"role": "assistant", "content": resp.content}]
        resp = await client.messages.create(messages=messages, **request)
        blocks.extend(resp.content or [])

    searched = False
    text = None
    for b in blocks:
        btype = getattr(b, "type", None)
        if btype in ("server_tool_use", "web_search_tool_result"):
            searched = True
        elif btype == "text":
            text = b.text
    if resp.stop_reason in ("refusal", "pause_turn") or text is None:
        return None, searched
    return json.loads(text), searched


async def _forecast_question(client, question: str, end_date: str | None) -> tuple[dict | None, bool]:
    lines = [
        f"Question: {question}",
        f"Today's date: {datetime.now(timezone.utc).date().isoformat()}",
    ]
    if end_date:
        lines.append(f"Resolution deadline (UTC): {end_date}")
    else:
        lines.append("No explicit deadline was stated; judge the question as asked.")
    return await _claude_json(client, models_llm.SYSTEM_PROMPT, "\n".join(lines), models_llm.OUTPUT_SCHEMA)


# ── storage ──────────────────────────────────────────────────────────────────


def _upsert_and_cached(question: str, end_date: str | None) -> tuple[str, str | None, dict | None]:
    """Upsert the custom market; return (uid, stored_end_date, cached_llm|None).

    cached is the newest source='llm' model_probs row when younger than
    CACHE_HOURS, reshaped into the response's llm dict.
    """
    conn = db.get_conn()
    try:
        uid = db.create_custom_market(conn, question, end_date)
        stored_end = (db.get_market(conn, uid) or {}).get("end_date")
        row = conn.execute(
            "SELECT ts, model_prob, detail FROM model_probs WHERE market_uid = ? AND source = 'llm' ORDER BY ts DESC, id DESC LIMIT 1",
            (uid,),
        ).fetchone()
        cached = None
        if row is not None:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat((row["ts"] or "").replace("Z", "+00:00"))
            except ValueError:
                age = None
            if age is not None and age < timedelta(hours=CACHE_HOURS):
                detail = json.loads(row["detail"] or "{}")
                cached = {
                    "probability": row["model_prob"],
                    "confidence": detail.get("confidence"),
                    "rationale": detail.get("rationale"),
                    "key_factors": detail.get("key_factors"),
                    "model": detail.get("model"),
                    "uid": uid,
                }
        return uid, stored_end, cached
    finally:
        conn.close()


def _store_forecast(uid: str, stored_end: str | None, prob: float, data: dict, searched: bool) -> None:
    conn = db.get_conn()
    try:
        db.insert_model_prob(
            conn,
            {
                "market_uid": uid,
                "source": "llm",
                "model_prob": prob,
                "prob_method": "llm_ask",
                "detail": json.dumps(
                    {
                        "confidence": data.get("confidence"),
                        "rationale": data.get("rationale"),
                        "key_factors": data.get("key_factors"),
                        "model": models_llm.MODEL,
                        "searched": searched,
                    }
                ),
            },
        )
        # No snapshot: custom markets have no market price. A calibration row
        # (market_prob=None) is logged only when a deadline exists, so the
        # adjudicator's backfill has something to score.
        if stored_end:
            db.log_calibration_row(
                conn,
                {
                    "market_uid": uid,
                    "platform": "custom",
                    "category": "custom",
                    "target_date": stored_end,
                    "source": "llm",
                    "model_prob": prob,
                    "market_prob": None,
                    "displayed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "prob_method": "llm_ask",
                },
            )
    finally:
        conn.close()


# ── entry point ──────────────────────────────────────────────────────────────


def _tracking_note(end_date: str | None) -> str:
    if end_date:
        return f"The outcome will be checked automatically after {end_date[:10]}."
    return (
        "No deadline could be parsed from the question, so this forecast cannot be tracked for accuracy. Include an explicit date (for example 'by 2026-12-31') to enable automatic outcome checking."
    )


async def ask(question: str) -> dict:
    """Answer a free-form forecast question. See module docstring for shape."""
    question = (question or "").strip()
    matches = await asyncio.to_thread(_search_matches, question)
    base = {"question": question, "matches": matches}

    if os.environ.get("FORECAST_LLM_ENABLED") != "1":
        return {**base, "llm": None, "note": "On-demand forecasting is disabled — set FORECAST_LLM_ENABLED=1 to enable it. Showing matching tracked markets only."}

    parsed_end = parse_deadline(question)
    uid, stored_end, cached = await asyncio.to_thread(_upsert_and_cached, question, parsed_end)
    if cached is not None:
        return {**base, "llm": cached, "note": f"Cached forecast (this question was asked within the last {CACHE_HOURS} hours). " + _tracking_note(stored_end)}

    client = _make_client()
    if client is None:
        return {**base, "llm": None, "note": "Forecast unavailable: Anthropic credentials are not configured."}

    try:
        data, searched = await _forecast_question(client, question, stored_end)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
        log.error("ask: Anthropic auth unavailable (%s)", e)
        return {**base, "llm": None, "note": "Forecast unavailable: Anthropic authentication failed."}
    except Exception as e:
        log.warning("ask: forecast failed for %r: %s", question, e)
        return {**base, "llm": None, "note": "Forecast attempt failed; please try again later."}

    if data is None:
        return {**base, "llm": None, "note": "The forecaster declined this question — no probability was produced."}

    try:
        prob = round(min(0.99, max(0.01, float(data["probability"]))), 4)
    except (KeyError, TypeError, ValueError):
        log.warning("ask: malformed forecast payload for %r", question)
        return {**base, "llm": None, "note": "Forecast returned malformed output; please try again later."}

    await asyncio.to_thread(_store_forecast, uid, stored_end, prob, data, searched)
    llm = {
        "probability": prob,
        "confidence": data.get("confidence"),
        "rationale": data.get("rationale"),
        "key_factors": data.get("key_factors"),
        "model": models_llm.MODEL,
        "uid": uid,
    }
    return {**base, "llm": llm, "note": "Forecast recorded. " + _tracking_note(stored_end)}

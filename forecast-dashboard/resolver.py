"""Market resolution against official venue settlement APIs.

Polymarket (gamma): GET /markets?condition_ids=<id>&closed=true. Verified
live 2026-08-07: plain condition_ids returns [] for closed markets, and the
/markets/<id> path form only accepts gamma's numeric id (422 on condition
ids), so the closed=true list query is the one working form — an empty list
simply means "not closed yet". outcomePrices and outcomes arrive as
JSON-encoded strings, e.g. '["1", "0"]' / '["Yes", "No"]'.

Kalshi: GET /trade-api/v2/markets/<ticker> -> {"market": {...}} with
status 'settled' or 'finalized' and result 'yes'/'no'.

Metaculus: GET /api2/questions/<id>/ (token header via ingest_metaculus when
METACULUS_API_TOKEN is set — anonymous API reads 403 as of 2026-08).
resolution yes/1.0 -> 1, no/0.0 -> 0. ambiguous/annulled (legacy -1/-2) get
the same no-poison handling as manifold CANCEL: unresolved calibration rows
are deleted, never backfilled, and the market is deactivated unscored.

Options (self-issued markets from ingest_options): venue_id encodes symbol,
expiry, and strike; the official close for the expiry date comes from the
Yahoo daily chart API and the market resolves close >= strike. Left pending
until that day's bar exists. When Yahoo is banned (per-IP 429s lasting
days), CBOE delayed quotes serve as fallback — but only while the
underlying's last_trade_time still carries the expiry date, since that
endpoint has no history.

Manifold: GET /v0/market/<id> -> isResolved with resolution YES/NO/MKT/CANCEL.
YES/NO score normally. MKT (resolved to a probability) rounds
resolutionProbability to a binary outcome. CANCEL reverts all trades upstream,
so nothing about it is scoreable — its unresolved calibration rows are deleted,
never backfilled. Settlements without a usable outcome are deactivated
unscored so they leave the pending queue.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import anthropic
import httpx

import ask
import db
import ingest_metaculus
import ingest_options

log = logging.getLogger("forecast.resolver")

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
KALSHI_MARKET_URL = "https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}"
MANIFOLD_MARKET_URL = "https://api.manifold.markets/v0/market/{market_id}"
METACULUS_QUESTION_URL = "https://www.metaculus.com/api2/questions/{qid}/"
YAHOO_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
CBOE_QUOTES_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
# Yahoo blocks non-browser UAs on some edges (see models_fed.py).
_YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}

REQUEST_TIMEOUT = 15
BATCH_LIMIT = 500
GRACE_HOURS = 6
SETTLE_EPS = 0.01


def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers={"User-Agent": "narve-forecast/1.0"})


def _parse_json_list(raw) -> list | None:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, list) else None
    return None


def _poly_outcome_from_market(m: dict) -> int | None:
    if not m.get("closed"):
        return None
    prices = _parse_json_list(m.get("outcomePrices"))
    if not prices or len(prices) != 2:
        return None
    try:
        p = [float(x) for x in prices]
    except (TypeError, ValueError):
        return None

    yes_idx = 0
    outcomes = _parse_json_list(m.get("outcomes")) or []
    for i, name in enumerate(outcomes):
        if isinstance(name, str) and name.strip().lower() == "yes":
            yes_idx = i
            break
    no_idx = 1 - yes_idx

    if p[yes_idx] >= 1 - SETTLE_EPS and p[no_idx] <= SETTLE_EPS:
        return 1
    if p[yes_idx] <= SETTLE_EPS and p[no_idx] >= 1 - SETTLE_EPS:
        return 0
    return None


async def _poly_outcome(client: httpx.AsyncClient, venue_id: str) -> int | None:
    resp = await client.get(GAMMA_MARKETS_URL, params={"condition_ids": venue_id, "closed": "true"})
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list) or not data:
        return None
    return _poly_outcome_from_market(data[0])


async def _kalshi_outcome(client: httpx.AsyncClient, venue_id: str) -> int | None:
    resp = await client.get(KALSHI_MARKET_URL.format(ticker=venue_id))
    resp.raise_for_status()
    market = (resp.json() or {}).get("market") or {}
    if market.get("status") not in ("settled", "finalized"):
        return None
    result = (market.get("result") or "").strip().lower()
    if result == "yes":
        return 1
    if result == "no":
        return 0
    return None


async def _yahoo_daily_close(client: httpx.AsyncClient, symbol: str, expiry) -> float | None:
    day_start = datetime(expiry.year, expiry.month, expiry.day, tzinfo=timezone.utc)
    params = {
        "period1": int((day_start - timedelta(days=1)).timestamp()),
        "period2": int((day_start + timedelta(days=2)).timestamp()),
        "interval": "1d",
    }
    try:
        resp = await client.get(YAHOO_CHART_URL.format(symbol=symbol), params=params, headers=_YAHOO_HEADERS)
        resp.raise_for_status()
        results = ((resp.json() or {}).get("chart") or {}).get("result") or []
    except Exception as e:
        log.warning("options: Yahoo chart failed for %s: %s", symbol, e)
        return None
    if not results or not isinstance(results[0], dict):
        return None
    result = results[0]
    timestamps = result.get("timestamp") or []
    quotes = (((result.get("indicators") or {}).get("quote") or [{}])[0]) or {}
    closes = quotes.get("close") or []
    # US daily bars are stamped at session open (13:30/14:30 UTC), so the
    # stamp's UTC date equals the trading date.
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        if datetime.fromtimestamp(ts, tz=timezone.utc).date() == expiry:
            try:
                return float(close)
            except (TypeError, ValueError):
                return None
    return None


async def _cboe_expiry_close(client: httpx.AsyncClient, symbol: str, expiry) -> float | None:
    """Expiry-day close via CBOE delayed quotes (keyless, no history).

    Only trusted while the underlying's last_trade_time still carries the
    expiry date — i.e. between that session's close and the next session's
    first print. With the resolver passing every few minutes that window is
    hit reliably; outside it the market simply stays pending for Yahoo."""
    try:
        resp = await client.get(CBOE_QUOTES_URL.format(symbol=symbol))
        if resp.status_code != 200:
            return None
        data = (resp.json() or {}).get("data") or {}
    except Exception as e:
        log.warning("options: CBOE quote failed for %s: %s", symbol, e)
        return None
    ltt = str(data.get("last_trade_time") or "")
    try:
        traded = datetime.strptime(ltt[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    if traded != expiry:
        return None
    try:
        close = float(data.get("close"))
    except (TypeError, ValueError):
        return None
    return close if close > 0 else None


async def _options_outcome(client: httpx.AsyncClient, venue_id: str) -> int | None:
    parsed = ingest_options.parse_venue_id(venue_id)
    if not parsed:
        return None
    symbol, expiry, strike = parsed
    close_dt = datetime(expiry.year, expiry.month, expiry.day, ingest_options.CLOSE_HOUR_UTC, tzinfo=timezone.utc)
    if datetime.now(timezone.utc) < close_dt + timedelta(hours=GRACE_HOURS):
        return None
    close = await _yahoo_daily_close(client, symbol, expiry)
    if close is None:
        close = await _cboe_expiry_close(client, symbol, expiry)
    if close is None:
        return None
    if ingest_options.is_direction(venue_id):
        # "ends UP" excludes a flat close: strictly greater.
        return 1 if close > strike else 0
    return 1 if close >= strike else 0


async def _manifold_settlement(client: httpx.AsyncClient, venue_id: str) -> tuple[str, int | None] | None:
    """None while unresolved; else (resolution, rounded resolutionProbability).

    resolution is one of YES/NO/MKT/CANCEL; the second element is
    1/0 from resolutionProbability >= 0.5, or None when absent.
    """
    resp = await client.get(MANIFOLD_MARKET_URL.format(market_id=venue_id))
    resp.raise_for_status()
    m = resp.json()
    if not isinstance(m, dict) or not m.get("isResolved"):
        return None
    resolution = (m.get("resolution") or "").strip().upper()
    if resolution not in ("YES", "NO", "MKT", "CANCEL"):
        return None
    try:
        rp_outcome = 1 if float(m.get("resolutionProbability")) >= 0.5 else 0
    except (TypeError, ValueError):
        rp_outcome = None
    return resolution, rp_outcome


def _deactivate_unscored(conn: sqlite3.Connection, uid: str) -> None:
    """Permanently drop a market that settled without a scoreable outcome.

    mark_resolved requires an integer outcome, and unresolved_past_close
    filters only on resolved=0 — active=0 alone would leave the market
    re-polled every pass forever, so resolved flips too (outcome stays NULL,
    which get_brier_score ignores).
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE markets SET active = 0, resolved = 1, resolved_at = ? WHERE uid = ?", (now, uid))
    conn.commit()


def _drop_unscored_calibration(conn: sqlite3.Connection, uid: str) -> int:
    """Delete a voided market's unresolved calibration rows (never backfill
    them — a settlement without a real outcome must not poison the Brier
    ledger). Returns the number of rows deleted."""
    deleted = conn.execute("DELETE FROM calibration WHERE market_uid = ? AND outcome IS NULL", (uid,)).rowcount
    conn.commit()
    return deleted


def _persist_outcome(conn: sqlite3.Connection, uid: str, outcome: int) -> bool:
    db.mark_resolved(conn, uid, outcome)
    backfilled = db.backfill_calibration_outcomes(conn, uid, outcome)
    log.info("resolved %s outcome=%s (calibration rows backfilled: %s)", uid, outcome, backfilled)
    return True


def _persist_manifold(conn: sqlite3.Connection, uid: str, settlement: tuple[str, int | None]) -> bool:
    """Apply a manifold settlement. Returns True when the market left the
    pending set (scored, or permanently deactivated)."""
    resolution, rp_outcome = settlement
    if resolution in ("YES", "NO"):
        return _persist_outcome(conn, uid, 1 if resolution == "YES" else 0)
    if resolution == "MKT":
        if rp_outcome is None:
            _deactivate_unscored(conn, uid)
            log.info("manifold %s resolved MKT without resolutionProbability; deactivated unscored", uid)
            return True
        db.mark_resolved(conn, uid, rp_outcome)
        backfilled = db.backfill_calibration_outcomes(conn, uid, rp_outcome)
        log.info("resolved %s outcome=%s from MKT resolutionProbability (calibration rows backfilled: %s)", uid, rp_outcome, backfilled)
        return True
    # CANCEL: trades were reverted upstream, so no forecast on this market is
    # scoreable — calibration rows are dropped, never backfilled.
    deleted = _drop_unscored_calibration(conn, uid)
    if rp_outcome is not None:
        db.mark_resolved(conn, uid, rp_outcome)
    else:
        _deactivate_unscored(conn, uid)
    log.info("manifold %s CANCEL handled (outcome=%s, unresolved calibration rows deleted: %d)", uid, rp_outcome, deleted)
    return True


# Sentinel for a Metaculus question voided by the venue (ambiguous/annulled):
# distinct from None (still pending) and from 0/1 (scoreable outcome).
METACULUS_VOID = "void"


def _metaculus_resolution(payload) -> int | str | None:
    """1/0 for yes/no, METACULUS_VOID for ambiguous/annulled, None if pending.

    Handles both API generations: the rewrite nests a 'question' object whose
    resolution is a string (yes/no/ambiguous/annulled); legacy api2 exposed a
    top-level numeric resolution (1.0/0.0, -1 ambiguous, -2 annulled).
    """
    if not isinstance(payload, dict):
        return None
    q = payload.get("question") if isinstance(payload.get("question"), dict) else payload
    res = q.get("resolution")
    if res is None and q is not payload:
        res = payload.get("resolution")
    if isinstance(res, str):
        s = res.strip().lower()
        if s == "yes":
            return 1
        if s == "no":
            return 0
        if s in ("ambiguous", "annulled"):
            return METACULUS_VOID
        try:
            res = float(s)
        except ValueError:
            return None
    if isinstance(res, bool):
        return 1 if res else 0
    if isinstance(res, (int, float)):
        f = float(res)
        if abs(f - 1.0) <= SETTLE_EPS:
            return 1
        if abs(f) <= SETTLE_EPS:
            return 0
        if f < 0:
            return METACULUS_VOID
    return None


async def _metaculus_settlement(client: httpx.AsyncClient, venue_id: str) -> int | str | None:
    resp = await client.get(METACULUS_QUESTION_URL.format(qid=venue_id), headers=ingest_metaculus.api_headers())
    resp.raise_for_status()
    return _metaculus_resolution(resp.json())


def _persist_metaculus(conn: sqlite3.Connection, uid: str, settlement: int | str) -> bool:
    """Apply a Metaculus settlement. Returns True when the market left the
    pending set (scored, or permanently deactivated)."""
    if settlement in (0, 1):
        return _persist_outcome(conn, uid, settlement)
    # Ambiguous/annulled: no scoreable outcome exists — same no-poison
    # treatment as manifold CANCEL.
    deleted = _drop_unscored_calibration(conn, uid)
    _deactivate_unscored(conn, uid)
    log.info("metaculus %s voided (%s; unresolved calibration rows deleted: %d)", uid, settlement, deleted)
    return True


async def _venue_outcome(client: httpx.AsyncClient, market: dict) -> int | None:
    venue = (market.get("venue") or "").lower()
    venue_id = market.get("venue_id")
    if not venue_id:
        return None
    if venue == "polymarket":
        return await _poly_outcome(client, venue_id)
    if venue == "kalshi":
        return await _kalshi_outcome(client, venue_id)
    if venue == "options":
        return await _options_outcome(client, venue_id)
    return None


async def resolve_pending(conn: sqlite3.Connection) -> int:
    candidates = db.unresolved_past_close(conn, grace_hours=GRACE_HOURS)[:BATCH_LIMIT]
    if not candidates:
        return 0

    resolved = 0
    async with _make_client() as client:
        for market in candidates:
            uid = market.get("uid")
            venue = (market.get("venue") or "").lower()
            try:
                # Venues whose settlement carries more than a binary outcome
                # (manifold MKT/CANCEL, metaculus void) dispatch before the
                # generic yes/no path; _venue_outcome stays unaware of them.
                if venue == "manifold" and market.get("venue_id"):
                    settlement = await _manifold_settlement(client, market["venue_id"])
                    persist = _persist_manifold
                elif venue == "metaculus" and market.get("venue_id"):
                    settlement = await _metaculus_settlement(client, market["venue_id"])
                    persist = _persist_metaculus
                else:
                    settlement = await _venue_outcome(client, market)
                    persist = _persist_outcome
            except Exception as e:
                log.warning("resolver: outcome check failed for %s: %s", uid, e)
                continue
            if settlement is None:
                continue
            try:
                if persist(conn, uid, settlement):
                    resolved += 1
            except Exception as e:
                log.error("resolver: failed to persist %s: %s", uid, e)
    return resolved


ADJUDICATE_GRACE_HOURS = 12
ADJUDICATE_GIVEUP_DAYS = 7
ADJUDICATE_CAP = 10

ADJUDICATE_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "evidence": {"type": "string"},
    },
    "required": ["outcome", "evidence"],
    "additionalProperties": False,
}

ADJUDICATE_SYSTEM_PROMPT = (
    "You are a careful fact-checker settling whether a predicted event actually happened by its deadline. "
    "Use web search to check the public record. Answer 'yes' only when the event clearly happened by the deadline, "
    "'no' only when it clearly did not, and 'unclear' when the record is ambiguous or not yet available. "
    "NEVER give trading or investment advice — your output is a factual verdict and its evidence, nothing more. "
    "Respond with only the JSON object matching the required schema — no prose before or after it."
)


def _parse_end_iso(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _has_llm_forecast(conn: sqlite3.Connection, uid: str) -> bool:
    return conn.execute("SELECT 1 FROM model_probs WHERE market_uid = ? AND source = 'llm' LIMIT 1", (uid,)).fetchone() is not None


def _adjudicate_prompt(market: dict) -> str:
    return (
        f"Predicted event: {market.get('question')}\n"
        f"Deadline (UTC): {market.get('end_date')}\n"
        f"Today's date: {datetime.now(timezone.utc).date().isoformat()}\n"
        "Did this event actually happen by the deadline?"
    )


async def adjudicate_custom(conn: sqlite3.Connection) -> int:
    """Resolve venue='custom' (ask-created) markets past their end_date.

    Custom questions have no venue settlement API, so a schema-constrained
    Claude call (web search allowed) judges the outcome from the public
    record. Candidates: active customs past end_date + 12h grace that have an
    llm forecast to score. yes/no -> mark_resolved + calibration backfill;
    unclear -> left pending for the next cycle; 7 days past the deadline the
    market is deactivated unscored (no calibration outcome). At most
    ADJUDICATE_CAP Claude calls per pass; FORECAST_LLM_ENABLED-gated.
    Returns the number of markets scored yes/no.
    """
    if os.environ.get("FORECAST_LLM_ENABLED") != "1":
        return 0

    now = datetime.now(timezone.utc)
    candidates = [
        m for m in db.unresolved_past_close(conn, grace_hours=ADJUDICATE_GRACE_HOURS) if (m.get("venue") or "").lower() == "custom" and m.get("active") == 1 and _has_llm_forecast(conn, m["uid"])
    ]
    if not candidates:
        return 0

    pending = []
    for m in candidates:
        end = _parse_end_iso(m.get("end_date"))
        if end is not None and now > end + timedelta(days=ADJUDICATE_GIVEUP_DAYS):
            _deactivate_unscored(conn, m["uid"])
            _drop_unscored_calibration(conn, m["uid"])
            log.info("adjudicate: gave up on %s (%d+ days past deadline, still unclear)", m["uid"], ADJUDICATE_GIVEUP_DAYS)
            continue
        pending.append(m)
    if not pending:
        return 0

    client = ask._make_client()
    if client is None:
        return 0

    resolved = 0
    for m in pending[:ADJUDICATE_CAP]:
        uid = m["uid"]
        try:
            data, _searched = await ask._claude_json(client, ADJUDICATE_SYSTEM_PROMPT, _adjudicate_prompt(m), ADJUDICATE_SCHEMA)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            log.error("adjudicate: Anthropic auth unavailable (%s); aborting pass", e)
            break
        except Exception as e:
            log.warning("adjudicate: %s failed: %s", uid, e)
            continue
        verdict = ((data or {}).get("outcome") or "").strip().lower()
        if verdict not in ("yes", "no"):
            # Refusals and 'unclear' both leave the market pending for the
            # next cycle (until the give-up window closes it).
            log.info("adjudicate: %s left pending (verdict=%s)", uid, verdict or "none")
            continue
        outcome = 1 if verdict == "yes" else 0
        try:
            db.mark_resolved(conn, uid, outcome)
            backfilled = db.backfill_calibration_outcomes(conn, uid, outcome)
        except Exception as e:
            log.error("adjudicate: failed to persist %s: %s", uid, e)
            continue
        resolved += 1
        log.info("adjudicated %s outcome=%s (calibration rows backfilled: %s) evidence: %.200s", uid, outcome, backfilled, (data or {}).get("evidence") or "")
    return resolved

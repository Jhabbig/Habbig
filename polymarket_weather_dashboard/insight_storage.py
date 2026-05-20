"""Insight persistence layer.

Three things use this:

  1. **Outcome ledger.** Every insight (user-triggered or auto) writes a
     row to `insight_log`. After the market resolves we pair it against
     `weather_resolutions` and compute PnL, win flag, and Brier on the
     recommendation enum (BUY_YES = predict prob 1, BUY_NO = predict 0,
     PASS / WAIT_AND_SEE = no bet). Calibration metrics flow from there.

  2. **Replay on the public track-record page.** Resolved insights are
     public — anyone can verify the model's pre-resolution call against
     the eventual outcome. We deliberately don't store a user_id; the
     log is global and tied to the market, not the requester.

  3. **Live feed + auto-mode.** The feed is just "newest N rows from
     `insight_log`"; auto-mode writes rows with `triggered_by = 'auto'`,
     and skips markets that already have a fresh insight (de-dup
     window).

The module is pure-ish — it takes a `conn_factory` callable so tests
can pass an in-memory SQLite connection, and it never imports server.py.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Write path ───────────────────────────────────────────────────────────────

def log_insight(conn_factory, *, market_id: str, context: dict,
                complete_data: dict, model: str, mode: str,
                latency_ms: Optional[int] = None,
                triggered_by: str = "user") -> Optional[int]:
    """Persist one fully-completed insight.

    `complete_data` is the dict yielded by `insight.stream_insight()` as
    its terminal `complete` chunk — `{insight, usage, model, stop_reason}`.
    `context` is the input dict we sent to the LLM (what the model saw
    when it made the call). We store both so replay is faithful.

    Returns the new row id, or None on failure (writes to the audit log
    must never crash the streaming response).
    """
    insight = (complete_data or {}).get("insight") or {}
    usage = (complete_data or {}).get("usage") or {}
    stop_reason = (complete_data or {}).get("stop_reason")

    # Pull denormalized columns for query speed — recommendations, edges,
    # and confidence are filtered/aggregated frequently and we don't want
    # to JSON-parse on every row.
    recommendation = insight.get("recommendation")
    confidence = insight.get("confidence")
    headline = insight.get("headline")
    suggested = insight.get("suggested_limit_cents")
    tail_warning = 1 if insight.get("tail_warning") else 0

    yes_price = context.get("yes_price")
    model_prob = context.get("model_prob")
    edge = context.get("edge")

    try:
        with conn_factory() as conn:
            cur = conn.execute(
                """INSERT INTO insight_log
                       (market_id, model, mode, yes_price, model_prob, edge,
                        recommendation, confidence, suggested_limit_cents,
                        tail_warning, headline, context_json, insight_json,
                        usage_input_tokens, usage_output_tokens,
                        usage_cache_creation, usage_cache_read,
                        stop_reason, latency_ms, triggered_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    market_id, model, mode,
                    yes_price, model_prob, edge,
                    recommendation, confidence,
                    int(suggested) if suggested is not None else None,
                    tail_warning, headline,
                    json.dumps(context, sort_keys=True, separators=(",", ":"), default=str),
                    json.dumps(insight, sort_keys=True, separators=(",", ":"), default=str),
                    int(usage.get("input_tokens") or 0),
                    int(usage.get("output_tokens") or 0),
                    int(usage.get("cache_creation_input_tokens") or 0),
                    int(usage.get("cache_read_input_tokens") or 0),
                    stop_reason,
                    int(latency_ms) if latency_ms is not None else None,
                    triggered_by,
                ),
            )
            return cur.lastrowid
    except Exception as e:
        logger.warning("insight log insert failed: %s", e)
        return None


# ─── Read path: feed + replay ─────────────────────────────────────────────────

def recent_insights(conn_factory, *, limit: int = 20,
                    min_abs_edge: Optional[float] = None,
                    recommendation: Optional[str] = None) -> list[dict]:
    """Newest-first list for the live feed. Excludes the heavy JSON
    blobs so the feed query stays small — call `get_insight(id)` for
    the full payload on click."""
    sql = ["""SELECT id, market_id, generated_at, model, mode, yes_price,
                     model_prob, edge, recommendation, confidence,
                     suggested_limit_cents, tail_warning, headline,
                     triggered_by
              FROM insight_log WHERE 1=1"""]
    args: list = []
    if min_abs_edge is not None:
        sql.append("AND ABS(COALESCE(edge, 0)) >= ?")
        args.append(float(min_abs_edge))
    if recommendation:
        sql.append("AND recommendation = ?")
        args.append(recommendation)
    # Tie-break on id when generated_at lands in the same ms — without
    # this, three rows inserted in a tight loop come back in insertion
    # order (oldest first), which is the opposite of what the feed wants.
    sql.append("ORDER BY generated_at DESC, id DESC LIMIT ?")
    args.append(int(max(1, min(200, limit))))
    with conn_factory(readonly=True) as conn:
        rows = conn.execute(" ".join(sql), tuple(args)).fetchall()
    return [dict(r) for r in rows]


def insights_for_market(conn_factory, market_id: str,
                        limit: int = 50) -> list[dict]:
    """Replay all insights generated for one market over time."""
    with conn_factory(readonly=True) as conn:
        rows = conn.execute(
            """SELECT id, generated_at, model, mode, yes_price, model_prob,
                      edge, recommendation, confidence, suggested_limit_cents,
                      tail_warning, headline, insight_json, triggered_by
               FROM insight_log WHERE market_id = ?
               ORDER BY generated_at DESC, id DESC LIMIT ?""",
            (market_id, int(limit)),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["insight"] = json.loads(d.pop("insight_json"))
        except (TypeError, ValueError):
            d["insight"] = None
        out.append(d)
    return out


def insights_for_date(conn_factory, date_iso: str,
                      limit: int = 100) -> list[dict]:
    """All insights generated on one UTC date. Powers the track-record
    page's per-day replay section."""
    with conn_factory(readonly=True) as conn:
        rows = conn.execute(
            """SELECT i.id, i.market_id, i.generated_at, i.model, i.mode,
                      i.yes_price, i.model_prob, i.edge, i.recommendation,
                      i.confidence, i.suggested_limit_cents, i.tail_warning,
                      i.headline, i.triggered_by,
                      r.actual_outcome, r.was_correct, r.pnl_per_dollar
               FROM insight_log i
               LEFT JOIN insight_resolutions r ON r.insight_id = i.id
               WHERE substr(i.generated_at, 1, 10) = ?
               ORDER BY i.generated_at DESC, i.id DESC LIMIT ?""",
            (date_iso, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def get_insight(conn_factory, insight_id: int) -> Optional[dict]:
    """Full payload for one row — context, insight JSON, usage."""
    with conn_factory(readonly=True) as conn:
        row = conn.execute(
            """SELECT i.*, r.actual_outcome, r.was_correct, r.pnl_per_dollar,
                      r.resolved_at
               FROM insight_log i
               LEFT JOIN insight_resolutions r ON r.insight_id = i.id
               WHERE i.id = ?""",
            (int(insight_id),),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    for key in ("context_json", "insight_json"):
        try:
            d[key.replace("_json", "")] = json.loads(d.pop(key))
        except (TypeError, ValueError):
            d[key.replace("_json", "")] = None
    return d


# ─── De-dup for auto-mode ─────────────────────────────────────────────────────

def has_recent_insight(conn_factory, market_id: str,
                       hours: float = 6.0) -> bool:
    """True if we generated an insight for this market in the last N
    hours. Cheap query — the auto loop checks this before firing."""
    with conn_factory(readonly=True) as conn:
        row = conn.execute(
            """SELECT 1 FROM insight_log
               WHERE market_id = ? AND generated_at >= datetime('now', ?)
               LIMIT 1""",
            (market_id, f"-{float(hours)} hours"),
        ).fetchone()
    return bool(row)


# ─── Resolution + calibration ─────────────────────────────────────────────────

def _bet_pnl_per_dollar(recommendation: Optional[str],
                        suggested_limit_cents: Optional[int],
                        yes_price: Optional[float],
                        outcome: str) -> Optional[float]:
    """PnL per $1 staked at the suggested limit, given the outcome.

    Convention matches `weather_calibration.py` + `backtest.py`:
        BUY_YES at p (cents): YES wins → (1-p/100); NO wins → -p/100
        BUY_NO at p (cents):  NO  wins → (1-p/100); YES wins → -p/100
    PASS / WAIT_AND_SEE: no bet → 0 PnL (counted as neither win nor loss).
    """
    if recommendation in ("PASS", "WAIT_AND_SEE", None):
        return 0.0
    # Prefer the LLM's suggested price; if missing, fall back to the
    # market price at generation time (some early rows may be incomplete).
    if suggested_limit_cents is None:
        if yes_price is None:
            return None
        price = yes_price if recommendation == "BUY_YES" else (1.0 - yes_price)
    else:
        price = float(suggested_limit_cents) / 100.0
    if recommendation == "BUY_YES":
        return round((1.0 - price) if outcome == "YES" else -price, 4)
    if recommendation == "BUY_NO":
        return round((1.0 - price) if outcome == "NO" else -price, 4)
    return None


def _was_correct(recommendation: Optional[str], outcome: str) -> Optional[int]:
    """1 if the recommendation matched the outcome, 0 if not, None if no
    bet was placed (PASS / WAIT_AND_SEE)."""
    if recommendation == "BUY_YES":
        return 1 if outcome == "YES" else 0
    if recommendation == "BUY_NO":
        return 1 if outcome == "NO" else 0
    return None


def resolve_insights(conn_factory, max_per_pass: int = 500) -> dict:
    """Pair every unresolved insight against `weather_resolutions` and
    write to `insight_resolutions`. Idempotent — re-running fills in
    rows for markets that have since resolved without touching ones
    already paired.

    Returns a stats dict the background loop can log.
    """
    with conn_factory(readonly=True) as conn:
        rows = conn.execute(
            """SELECT i.id, i.market_id, i.recommendation,
                      i.suggested_limit_cents, i.yes_price,
                      r.actual_outcome, r.resolved_at
               FROM insight_log i
               JOIN weather_resolutions r ON r.market_id = i.market_id
               LEFT JOIN insight_resolutions ir ON ir.insight_id = i.id
               WHERE ir.insight_id IS NULL
                 AND r.actual_outcome IN ('YES', 'NO')
               LIMIT ?""",
            (int(max_per_pass),),
        ).fetchall()
    stats = {"resolved": 0, "skipped_no_outcome": 0}
    for r in rows:
        outcome = r["actual_outcome"]
        if outcome not in ("YES", "NO"):
            stats["skipped_no_outcome"] += 1
            continue
        was_correct = _was_correct(r["recommendation"], outcome)
        pnl = _bet_pnl_per_dollar(
            r["recommendation"], r["suggested_limit_cents"],
            r["yes_price"], outcome,
        )
        try:
            with conn_factory() as conn:
                conn.execute(
                    """INSERT INTO insight_resolutions
                           (insight_id, market_id, actual_outcome,
                            was_correct, pnl_per_dollar, resolved_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (int(r["id"]), r["market_id"], outcome,
                     was_correct, pnl,
                     r["resolved_at"] or datetime.now(timezone.utc).isoformat()),
                )
            stats["resolved"] += 1
        except Exception as e:
            logger.warning("insight resolution insert failed for %s: %s",
                           r["id"], e)
    return stats


def calibration_stats(conn_factory, days: int = 90) -> dict:
    """Compute Brier + per-recommendation + per-confidence breakdowns
    for the calibration page. Brier on the recommendation enum treats
    BUY_YES as predicting prob=1, BUY_NO as prob=0, and skips
    PASS/WAIT_AND_SEE (the model is explicitly declining to predict).
    """
    days = max(1, min(365, int(days)))
    with conn_factory(readonly=True) as conn:
        rows = conn.execute(
            """SELECT i.recommendation, i.confidence, i.tail_warning,
                      i.suggested_limit_cents, i.yes_price,
                      r.actual_outcome, r.was_correct, r.pnl_per_dollar
               FROM insight_log i
               JOIN insight_resolutions r ON r.insight_id = i.id
               WHERE i.generated_at >= datetime('now', ?)""",
            (f"-{days} days",),
        ).fetchall()

    n_total = len(rows)
    by_rec: dict = {}
    by_conf: dict = {}
    bets = 0
    wins = 0
    pnl_sum = 0.0
    brier_terms = 0
    brier_sum = 0.0
    tail_warned_correct = 0
    tail_warned_total = 0

    for r in rows:
        rec = r["recommendation"] or "PASS"
        conf = r["confidence"] or "low"
        b = by_rec.setdefault(rec, {"n": 0, "wins": 0, "pnl": 0.0})
        b["n"] += 1
        c = by_conf.setdefault(conf, {"n": 0, "wins": 0, "n_betted": 0})
        c["n"] += 1
        if r["was_correct"] is not None:
            bets += 1
            b["wins"] += r["was_correct"]
            c["wins"] += r["was_correct"]
            c["n_betted"] += 1
        if r["pnl_per_dollar"] is not None:
            b["pnl"] += float(r["pnl_per_dollar"])
            if rec in ("BUY_YES", "BUY_NO"):
                pnl_sum += float(r["pnl_per_dollar"])
        if rec in ("BUY_YES", "BUY_NO") and r["actual_outcome"] in ("YES", "NO"):
            predicted = 1.0 if rec == "BUY_YES" else 0.0
            actual = 1.0 if r["actual_outcome"] == "YES" else 0.0
            brier_sum += (predicted - actual) ** 2
            brier_terms += 1
        if r["tail_warning"]:
            tail_warned_total += 1
            if r["was_correct"] == 1:
                tail_warned_correct += 1
        if r["was_correct"] == 1:
            wins += 1

    def _round(v, n=4):
        return round(float(v), n) if v is not None else None

    return {
        "days": days,
        "n_total": n_total,
        "n_betted": bets,
        "n_wins": wins,
        "win_rate": _round(wins / bets) if bets else None,
        "brier_score": _round(brier_sum / brier_terms) if brier_terms else None,
        "n_brier_samples": brier_terms,
        "total_pnl_per_dollar": _round(pnl_sum, 4),
        "avg_pnl_per_bet": _round(pnl_sum / bets) if bets else None,
        "tail_warning_calls": tail_warned_total,
        "tail_warning_win_rate": (_round(tail_warned_correct / tail_warned_total)
                                   if tail_warned_total else None),
        "by_recommendation": {
            k: {
                "n": v["n"],
                "wins": v["wins"],
                "win_rate": _round(v["wins"] / v["n"]) if v["n"] else None,
                "total_pnl_per_dollar": _round(v["pnl"], 4),
                "avg_pnl_per_bet": _round(v["pnl"] / v["n"]) if v["n"] else None,
            }
            for k, v in by_rec.items()
        },
        "by_confidence": {
            k: {
                "n": v["n"],
                "n_betted": v["n_betted"],
                "wins": v["wins"],
                "win_rate": _round(v["wins"] / v["n_betted"]) if v["n_betted"] else None,
            }
            for k, v in by_conf.items()
        },
    }


# ─── Auto-mode candidate selection ────────────────────────────────────────────

def auto_candidates(conn_factory, markets: list[dict], *,
                    min_abs_edge: float = 0.05,
                    dedup_hours: float = 6.0,
                    limit: int = 10) -> list[dict]:
    """Pick which markets the auto loop should fire on this pass.

    Filters:
      * `|edge|` at least `min_abs_edge`
      * `target_date` is in the future (markets that already resolved
        don't need a fresh insight)
      * No insight in the last `dedup_hours` for this market
      * Capped at `limit` per pass

    Sorted by `|edge|` descending so the biggest signals fire first if
    the cap binds.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out: list[dict] = []
    sortable: list[tuple[float, dict]] = []
    for m in markets or []:
        edge = m.get("edge")
        if edge is None:
            continue
        try:
            edge_f = float(edge)
        except (TypeError, ValueError):
            continue
        if abs(edge_f) < float(min_abs_edge):
            continue
        target = m.get("target_date")
        if not target or target < today:
            continue
        mid = m.get("market_id")
        if not mid:
            continue
        if has_recent_insight(conn_factory, mid, hours=dedup_hours):
            continue
        sortable.append((abs(edge_f), m))
    sortable.sort(key=lambda x: -x[0])
    for _e, m in sortable[: int(limit)]:
        out.append(m)
    return out

"""Markets page — the single main surface for the 2026-06 pivot.

The founder meeting killed the multi-dashboard system in favour of ONE
page focused on market trades + core functionality, scoped to a single
vertical: MIDTERM US politics. This module is that page.

It is deliberately self-contained and ugly-is-fine (per the pivot):

  * GET /markets/active
        Server-rendered HTML listing the top ~15 highest-edge UNRESOLVED
        midterm (politics) predictions joined to source_credibility, with
        the latest market price from ``market_snapshots`` and the implied
        edge (predicted_probability - latest yes_price).

  * GET /api/analytics/prediction-accuracy?days=30&source=
        Read-only JSON over resolved predictions: accuracy %, Brier-based
        calibration score, sample size, resolved count.

Design notes / contracts honoured:

  * Keeps state access through its own sqlite3 handle and never imports
    db.py — same pattern as backtest_routes.py. DB path resolution mirrors
    db.py (GATEWAY_DB_PATH override, else auth.db beside this file).
  * ``market_snapshots`` is keyed on ``market_slug``. In this codebase the
    value stored in ``predictions.market_id`` IS the snapshot slug — see
    queries/markets.py:get_prediction_markers_for_market, which joins
    ``market_snapshots.market_slug = predictions.market_id`` directly. We
    reconcile the same way.
  * Calibration uses credibility/calibration.py:compute_brier_score when
    importable; it expects records keyed ``predicted_probability_stated`` +
    ``resolved_correct`` and returns None below 10 samples.
  * NOT pro-gated — the MVP demo needs these open.
  * Empty-data safe: today market_snapshots=0 and resolved=0, so latest
    price / edge fall back gracefully and the accuracy endpoint reports
    "no resolved data yet" with the count of predictions tracked.

Wire via ``markets_routes.register(app)`` from server.py (the integrator
does this — do not edit server.py here).
"""

from __future__ import annotations

import html
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse


log = logging.getLogger("markets_routes")


# The single vertical the pivot keeps: midterm US politics.
MIDTERM_CATEGORY = "politics"
TOP_N = 15


# ── DB ──────────────────────────────────────────────────────────────────────


def _db_path() -> Path:
    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path(__file__).parent / p)
    return Path(__file__).parent / "auth.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})"))
    except sqlite3.Error:
        return False


# ── Data access ──────────────────────────────────────────────────────────────


def _load_active_predictions(conn: sqlite3.Connection, limit: int = TOP_N) -> list[dict]:
    """Top-edge UNRESOLVED midterm predictions joined to credibility + price.

    Edge = predicted_probability - latest market yes_price (by market_slug).
    When no snapshot exists (today: market_snapshots is empty) we fall back
    to the stored ``edge_at_prediction`` column if present, else None — the
    template renders that as "—" and sorts those rows last.
    """
    if not _table_exists(conn, "predictions"):
        return []

    has_snapshots = _table_exists(conn, "market_snapshots")
    has_cred = _table_exists(conn, "source_credibility")
    has_stored_edge = _has_column(conn, "predictions", "edge_at_prediction")

    # Correlated subquery for the latest yes_price tied to this prediction's
    # market. Guarded so a missing table can't break the query.
    if has_snapshots:
        latest_price_sql = (
            "(SELECT ms.yes_price FROM market_snapshots ms "
            " WHERE ms.market_slug = p.market_id "
            " ORDER BY ms.snapshotted_at DESC LIMIT 1)"
        )
    else:
        latest_price_sql = "NULL"

    cred_select = "sc.global_credibility AS source_credibility" if has_cred else "NULL AS source_credibility"
    cred_join = "LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle" if has_cred else ""
    stored_edge_select = "p.edge_at_prediction AS stored_edge" if has_stored_edge else "NULL AS stored_edge"

    # Order key: absolute edge (live snapshot edge preferred, else stored
    # edge), NULLs last via COALESCE to -1.
    if has_snapshots and has_stored_edge:
        order_edge = (
            "COALESCE(p.predicted_probability - " + latest_price_sql + ", p.edge_at_prediction, -1)"
        )
    elif has_snapshots:
        order_edge = "COALESCE(p.predicted_probability - " + latest_price_sql + ", -1)"
    elif has_stored_edge:
        order_edge = "COALESCE(p.edge_at_prediction, -1)"
    else:
        order_edge = "-1"

    sql = f"""
        SELECT
            p.id,
            p.source_handle,
            p.market_id,
            p.category,
            p.content,
            p.predicted_probability AS predicted_probability,
            p.extracted_at,
            {stored_edge_select},
            {cred_select},
            {latest_price_sql} AS latest_yes_price
        FROM predictions p
        {cred_join}
        WHERE p.resolved = 0
          AND p.category = ?
          AND p.predicted_probability IS NOT NULL
        ORDER BY ABS({order_edge}) DESC, p.extracted_at DESC
        LIMIT ?
    """
    try:
        rows = conn.execute(sql, (MIDTERM_CATEGORY, int(limit))).fetchall()
    except sqlite3.Error:
        log.exception("markets_active query failed")
        return []

    out: list[dict] = []
    for r in rows:
        d = dict(r)
        pred = d.get("predicted_probability")
        price = d.get("latest_yes_price")
        if pred is not None and price is not None:
            edge: Optional[float] = float(pred) - float(price)
        elif d.get("stored_edge") is not None:
            edge = float(d["stored_edge"])
        else:
            edge = None
        d["edge"] = edge
        out.append(d)
    return out


def _total_predictions_tracked(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "predictions"):
        return 0
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM predictions").fetchone()
        return int(row["n"]) if row else 0
    except sqlite3.Error:
        return 0


def _compute_accuracy(conn: sqlite3.Connection, days: int, source: Optional[str]) -> dict:
    """Accuracy + calibration over resolved predictions.

    Filters to predictions resolved within the last ``days`` (by resolved_at
    when available; rows with a NULL resolved_at are kept so freshly-flipped
    rows aren't silently dropped). Optionally scoped to a single source.
    """
    tracked = _total_predictions_tracked(conn)
    base = {
        "accuracy_pct": None,
        "calibration_score": None,
        "sample_size": 0,
        "resolved_count": 0,
        "predictions_tracked": tracked,
        "days": days,
        "source": source,
    }
    if not _table_exists(conn, "predictions"):
        base["message"] = "no predictions table yet"
        return base

    clauses = ["resolved = 1"]
    args: list[Any] = []
    if days and days > 0 and _has_column(conn, "predictions", "resolved_at"):
        cutoff = int(time.time()) - int(days) * 86400
        # Keep rows with NULL resolved_at (older flips) rather than dropping them.
        clauses.append("(resolved_at IS NULL OR resolved_at >= ?)")
        args.append(cutoff)
    if source:
        clauses.append("source_handle = ?")
        args.append(source)

    where = " AND ".join(clauses)
    try:
        rows = conn.execute(
            f"SELECT predicted_probability, resolved_correct FROM predictions WHERE {where}",
            tuple(args),
        ).fetchall()
    except sqlite3.Error:
        log.exception("prediction-accuracy query failed")
        base["message"] = "query failed"
        return base

    resolved_count = len(rows)
    base["resolved_count"] = resolved_count
    if resolved_count == 0:
        base["message"] = f"no resolved data yet — {tracked} predictions tracked"
        return base

    # Accuracy: fraction of resolved_correct == 1.
    correct = sum(1 for r in rows if r["resolved_correct"])
    base["accuracy_pct"] = round(correct / resolved_count * 100.0, 2)
    base["sample_size"] = resolved_count

    # Calibration via Brier (credibility/calibration.py). It expects records
    # keyed predicted_probability_stated + resolved_correct, and returns None
    # below its MIN_SAMPLE (10).
    try:
        from credibility.calibration import compute_brier_score  # type: ignore

        records = [
            {
                "predicted_probability_stated": r["predicted_probability"],
                "resolved_correct": r["resolved_correct"],
            }
            for r in rows
            if r["predicted_probability"] is not None
        ]
        brier = compute_brier_score(records)
        if brier is not None:
            base["calibration_score"] = brier.get("calibration")
            base["brier"] = brier.get("brier")
        else:
            base["calibration_note"] = "need >=10 graded predictions for calibration"
    except Exception as exc:  # pragma: no cover - defensive, keep endpoint alive
        log.warning("calibration import/compute failed: %s", exc)
        base["calibration_note"] = "calibration unavailable"

    return base


# ── Rendering helpers ────────────────────────────────────────────────────────


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_signed_pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v) * 100:+.1f} pts"
    except (TypeError, ValueError):
        return "—"


def _fmt_cred(v: Optional[float]) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


def _freshness(extracted_at: Optional[int]) -> str:
    if not extracted_at:
        return "—"
    delta = int(time.time()) - int(extracted_at)
    if delta < 0:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _edge_class(edge: Optional[float]) -> str:
    if edge is None:
        return "neutral"
    if edge > 0:
        return "pos"
    if edge < 0:
        return "neg"
    return "neutral"


def _render_rows(preds: list[dict]) -> str:
    if not preds:
        return (
            "<tr><td colspan='7' class='empty'>No active midterm predictions "
            "to show yet. As the pipeline ingests posts they will appear here, "
            "ranked by edge.</td></tr>"
        )
    cells: list[str] = []
    for p in preds:
        question = p.get("content") or p.get("market_id") or "(untitled market)"
        slug = p.get("market_id") or "—"
        edge = p.get("edge")
        cells.append(
            "<tr>"
            f"<td class='q'><div class='qtext'>{html.escape(str(question)[:160])}</div>"
            f"<div class='slug'>{html.escape(str(slug))}</div></td>"
            f"<td>{html.escape(str(p.get('source_handle') or '—'))}</td>"
            f"<td class='num'>{_fmt_cred(p.get('source_credibility'))}</td>"
            f"<td class='num'>{_fmt_pct(p.get('predicted_probability'))}</td>"
            f"<td class='num'>{_fmt_pct(p.get('latest_yes_price'))}</td>"
            f"<td class='num edge {_edge_class(edge)}'>{_fmt_signed_pct(edge)}</td>"
            f"<td class='num'>{html.escape(_freshness(p.get('extracted_at')))}</td>"
            "</tr>"
        )
    return "".join(cells)


def _page_html(preds: list[dict], tracked: int, snapshots: int) -> str:
    rows_html = _render_rows(preds)
    note = ""
    if snapshots == 0:
        note = (
            "<div class='banner'>No live market snapshots yet — latest price "
            "and edge will populate once the snapshot pipeline runs. Showing "
            f"{len(preds)} active prediction(s) of {tracked} tracked.</div>"
        )
    return f"""<!DOCTYPE html><html lang='en'><head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Active Markets — narve.ai</title>
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=8'>
<style>
body{{background:var(--bg-base,#0c0c0c);color:var(--text-primary,#eee);
font-family:var(--font-ui,system-ui,sans-serif);padding:32px;max-width:1100px;margin:0 auto}}
h1{{font-family:var(--font-display,Georgia,serif);font-style:italic;font-size:38px;
letter-spacing:-0.02em;margin:0 0 4px}}
.sub{{color:var(--text-secondary,#999);margin:0 0 24px;font-size:14px}}
.banner{{background:var(--bg-surface,#161616);border:1px solid var(--border-default,#2a2a2a);
border-radius:10px;padding:12px 16px;margin:0 0 20px;font-size:13px;color:var(--text-secondary,#aaa)}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid var(--border-default,#222);vertical-align:top}}
th{{font-size:11px;text-transform:uppercase;letter-spacing:0.04em;color:var(--text-secondary,#888);font-weight:600}}
td.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
td.q{{max-width:420px}}
.qtext{{font-weight:500}}
.slug{{color:var(--text-secondary,#777);font-size:11px;font-family:var(--font-mono,ui-monospace,monospace);margin-top:2px}}
.edge.pos{{color:#46c98b}}
.edge.neg{{color:#e0607a}}
.edge.neutral{{color:var(--text-secondary,#888)}}
.empty{{text-align:center;color:var(--text-secondary,#888);padding:32px 12px}}
.acc{{margin-top:28px;font-size:13px;color:var(--text-secondary,#aaa)}}
.acc a{{color:inherit}}
</style></head><body>
<h1>Active Markets</h1>
<p class='sub'>Top {TOP_N} highest-edge unresolved midterm (US politics) predictions, ranked by absolute edge.</p>
{note}
<table>
<thead><tr>
<th>Market</th><th>Source</th><th>Cred</th><th>narve p</th><th>Mkt price</th><th>Edge</th><th>Fresh</th>
</tr></thead>
<tbody>
{rows_html}
</tbody>
</table>
<p class='acc'>Accuracy &amp; calibration JSON:
<a href='/api/analytics/prediction-accuracy?days=30'>/api/analytics/prediction-accuracy?days=30</a></p>
</body></html>"""


# ── Handlers ─────────────────────────────────────────────────────────────────


async def markets_active_page(request: Request):
    """GET /markets/active — the single main page. Open, not pro-gated."""
    conn = _connect()
    try:
        preds = _load_active_predictions(conn, TOP_N)
        tracked = _total_predictions_tracked(conn)
        snapshots = 0
        if _table_exists(conn, "market_snapshots"):
            try:
                snapshots = int(conn.execute("SELECT COUNT(*) AS n FROM market_snapshots").fetchone()["n"])
            except sqlite3.Error:
                snapshots = 0
    finally:
        conn.close()
    return HTMLResponse(_page_html(preds, tracked, snapshots))


async def prediction_accuracy(request: Request):
    """GET /api/analytics/prediction-accuracy?days=30&source= — read-only JSON."""
    qp = request.query_params
    try:
        days = int(qp.get("days", "30"))
    except (TypeError, ValueError):
        days = 30
    if days < 0:
        days = 0
    if days > 3650:
        days = 3650
    source = qp.get("source") or None
    if source:
        source = source.strip()[:200] or None

    conn = _connect()
    try:
        data = _compute_accuracy(conn, days, source)
    finally:
        conn.close()
    return JSONResponse(data)


# ── Registration ─────────────────────────────────────────────────────────────


def register(app) -> None:
    """Add the markets page + accuracy API routes. Called from server.py."""
    app.add_api_route(
        "/markets/active",
        markets_active_page,
        methods=["GET"],
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    app.add_api_route(
        "/api/analytics/prediction-accuracy",
        prediction_accuracy,
        methods=["GET"],
    )

"""Build a Pro user's weekly intelligence PDF.

``build_report_for_user(user_id, period_start, period_end)`` is the
single entry point. It:

  1. Loads the week's best-bets + resolutions from the DB (tolerant
     probes — the schema has shifted across branches).
  2. Computes weekly-simulated ROI (flat-stake replay).
  3. Picks the top-5 sources + top-5 signals of the week.
  4. Asks Claude Sonnet for a narrative exec summary, per-bet analysis,
     callouts, and a "one to watch next week". Missing API key → the
     report still renders, just without prose.
  5. Renders templates/weekly_report.html → PDF via WeasyPrint if
     installed, otherwise HTML only.
  6. Writes the PDF path + html_excerpt into weekly_reports (migration
     057).

Never raises: every failure path is caught and stored on the row so
the cron can keep going and the Pro dashboard can show the error.
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from ai import client as ai_client


log = logging.getLogger("reports.weekly")


REPORT_MAX_TOKENS = 2400


REPORT_SYSTEM_PROMPT = """\
You write a weekly intelligence briefing for a prediction-market analytics
product (narve.ai). Your reader is a trader reviewing last week's best-bets
and planning next week.

Given structured inputs (best bets + resolutions, top sources, top signals,
next week's resolving markets), produce a JSON object with EXACTLY this
shape — no extra keys, no prose outside the JSON:

{
  "executive_summary":   "<2-3 sentence overview of the week>",
  "per_bet_analysis": [  // 1 per resolved best-bet, in reader order
    {
      "market":   "<short>",
      "takeaway": "<1-2 sentences — why did it resolve this way, what's the lesson>"
    }
  ],
  "callouts": [          // 2-3 notable this-week observations
    "<1 sentence each>"
  ],
  "one_to_watch": {
    "market": "<short>",
    "why":    "<1-2 sentences — why this market matters next week>"
  }
}
"""


REPORTS_DIR = Path(os.environ.get("WEEKLY_REPORTS_DIR", "/tmp/narve-weekly-reports"))
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ── DB utilities ────────────────────────────────────────────────────────────


def _db_path() -> Path:
    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path(__file__).parent.parent / p)
    return Path(__file__).parent.parent / "auth.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


# ── Data gathering ──────────────────────────────────────────────────────────


def _load_best_bets(conn: sqlite3.Connection, start: int, end: int) -> list[dict]:
    # best_bets table layout varies — probe for the most recent known shape.
    if _table_exists(conn, "best_bets"):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(best_bets)")}
        if "resolved_at" in cols:
            rows = conn.execute(
                "SELECT * FROM best_bets "
                "WHERE resolved_at IS NOT NULL "
                "AND resolved_at >= ? AND resolved_at < ?",
                (start, end),
            ).fetchall()
            return [dict(r) for r in rows]
    # Fall back to predictions resolved in the window.
    if _table_exists(conn, "predictions"):
        rows = conn.execute(
            "SELECT content AS market, direction, resolved_correct, resolved_at "
            "FROM predictions WHERE resolved = 1 "
            "AND resolved_at >= ? AND resolved_at < ? LIMIT 12",
            (start, end),
        ).fetchall()
        return [dict(r) for r in rows]
    return []


def _load_top_sources(conn: sqlite3.Connection, start: int, end: int) -> list[dict]:
    if not _table_exists(conn, "source_credibility"):
        return []
    rows = conn.execute(
        "SELECT source_handle, global_credibility, total_predictions, correct_predictions "
        "FROM source_credibility "
        "WHERE accuracy_unlocked = 1 "
        "ORDER BY global_credibility DESC LIMIT 5"
    ).fetchall()
    return [dict(r) for r in rows]


def _load_top_signals(conn: sqlite3.Connection, start: int, end: int) -> list[dict]:
    # Signals in narve.ai live in several tables depending on which pass
    # is running. We look for a generic "signals" surface first.
    if _table_exists(conn, "signals"):
        rows = conn.execute(
            "SELECT * FROM signals WHERE created_at >= ? AND created_at < ? "
            "ORDER BY created_at DESC LIMIT 5",
            (start, end),
        ).fetchall()
        return [dict(r) for r in rows]
    return []


def _load_next_week_markets(conn: sqlite3.Connection, start: int, end: int) -> list[dict]:
    # 7 days forward from the end of the period.
    upper = end + 7 * 86400
    if _table_exists(conn, "market_snapshots"):
        rows = conn.execute(
            "SELECT market_slug, market_title, close_time, yes_price "
            "FROM market_snapshots "
            "WHERE close_time >= ? AND close_time < ? "
            "GROUP BY market_slug "
            "ORDER BY close_time ASC LIMIT 10",
            (end, upper),
        ).fetchall()
        return [dict(r) for r in rows]
    return []


def _simulated_roi(best_bets: list[dict]) -> dict:
    """Flat $100 stake per bet, even-odds simplification — sufficient for
    a weekly dashboard summary. If the row doesn't have prices, it
    counts as a push.
    """
    wins = 0
    losses = 0
    pushes = 0
    pnl = 0.0
    for bet in best_bets:
        correct = bet.get("resolved_correct")
        if correct is None:
            pushes += 1
            continue
        price = bet.get("yes_price") or bet.get("price_at_bet") or 0.5
        try:
            price = float(price)
        except (TypeError, ValueError):
            price = 0.5
        price = max(0.01, min(0.99, price))
        stake = 100.0
        direction = str(bet.get("direction") or "YES").upper()
        effective_price = price if direction == "YES" else (1.0 - price)
        if correct:
            # Win pays (1 − price) * stake on each $1 of stake.
            pnl += stake * (1.0 - effective_price) / effective_price
            wins += 1
        else:
            pnl -= stake
            losses += 1
    total = wins + losses
    roi_pct = (pnl / (total * 100.0) * 100.0) if total else 0.0
    return {
        "wins": wins, "losses": losses, "pushes": pushes,
        "pnl_usd": round(pnl, 2), "roi_pct": round(roi_pct, 2),
    }


# ── Claude narrative ────────────────────────────────────────────────────────


async def _call_claude_narrative(payload: dict) -> Optional[dict]:
    text = await ai_client.call_claude(
        feature="weekly_report",
        system=REPORT_SYSTEM_PROMPT,
        user=json.dumps(payload)[:12000],
        model=ai_client.ANTHROPIC_MODELS["weekly_report"],
        max_tokens=REPORT_MAX_TOKENS,
    )
    if text is None:
        return None
    text = text.strip()
    if text.startswith("```"):
        import re as _re
        text = _re.sub(r"^```[a-zA-Z]*\n?", "", text)
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("weekly_report: JSON parse failed")
        return None


# ── HTML + PDF rendering ────────────────────────────────────────────────────


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>narve.ai — weekly intelligence {period}</title>
  <style>
    body {{ font-family: 'Geist', system-ui, sans-serif; color: #111; max-width: 720px; margin: 0 auto; padding: 40px; }}
    h1 {{ font-family: 'Instrument Serif', serif; font-style: italic; font-size: 40px; letter-spacing: -0.02em; margin: 0 0 8px; }}
    h2 {{ font-family: 'Geist', sans-serif; font-size: 14px; text-transform: uppercase; letter-spacing: 0.1em; margin: 32px 0 10px; color: #555; }}
    .meta {{ color: #888; font-size: 13px; }}
    .kv {{ display: flex; gap: 16px; flex-wrap: wrap; margin: 12px 0; font-size: 13px; }}
    .kv div {{ padding: 10px 14px; background: #f7f7f7; border-radius: 8px; }}
    .kv strong {{ display: block; font-size: 18px; }}
    ul {{ padding-left: 20px; }}
    li {{ margin: 6px 0; }}
    .prose {{ font-size: 14px; line-height: 1.55; }}
    .mono {{ font-family: 'Geist Mono', monospace; font-size: 12px; }}
  </style>
</head>
<body>
  <h1>Weekly intelligence</h1>
  <p class="meta">{period}</p>

  <div class="kv">
    <div><strong>{wins}–{losses}</strong><span>best-bet record</span></div>
    <div><strong>${pnl}</strong><span>simulated P&amp;L</span></div>
    <div><strong>{roi}%</strong><span>ROI</span></div>
    <div><strong>{pushes}</strong><span>pushes</span></div>
  </div>

  <h2>Executive summary</h2>
  <p class="prose">{exec_summary}</p>

  <h2>Per-bet analysis</h2>
  <ul class="prose">{per_bet_items}</ul>

  <h2>Top sources this week</h2>
  <ul class="prose">{source_items}</ul>

  <h2>Notable callouts</h2>
  <ul class="prose">{callout_items}</ul>

  <h2>One to watch</h2>
  <p class="prose"><strong>{watch_market}.</strong> {watch_why}</p>

  <h2>Resolving next week</h2>
  <ul class="prose">{next_week_items}</ul>

  <p class="meta" style="margin-top: 40px; font-size: 11px;">
    Generated by narve.ai · report id #{report_id}
  </p>
</body>
</html>"""


def _render_html(period: str, stats: dict, narrative: dict, data: dict, report_id: int) -> str:
    per_bet_items = "".join(
        f"<li><strong>{_html.escape(b.get('market',''))}</strong> — "
        f"{_html.escape(b.get('takeaway',''))}</li>"
        for b in (narrative.get("per_bet_analysis") or [])
    ) or "<li>No resolved best-bets this week.</li>"

    callout_items = "".join(
        f"<li>{_html.escape(str(c))}</li>"
        for c in (narrative.get("callouts") or [])
    ) or "<li>No notable callouts.</li>"

    source_items = "".join(
        f"<li><span class='mono'>@{_html.escape(s.get('source_handle',''))}</span> · "
        f"credibility {float(s.get('global_credibility') or 0):.2f} · "
        f"{int(s.get('correct_predictions') or 0)}/{int(s.get('total_predictions') or 0)} correct</li>"
        for s in data.get("top_sources") or []
    ) or "<li>Not enough rated sources for this week.</li>"

    next_week_items = "".join(
        f"<li>{_html.escape(m.get('market_title') or m.get('market_slug',''))} — "
        f"closes {_dt.datetime.utcfromtimestamp(int(m.get('close_time') or 0)).strftime('%a %d %b')}</li>"
        for m in data.get("next_week_markets") or []
    ) or "<li>No markets resolving in the next week.</li>"

    watch = narrative.get("one_to_watch") or {}

    return _HTML_TEMPLATE.format(
        period=period,
        wins=stats["wins"], losses=stats["losses"],
        pushes=stats["pushes"],
        pnl=stats["pnl_usd"], roi=stats["roi_pct"],
        exec_summary=_html.escape(narrative.get("executive_summary") or "No summary produced this week."),
        per_bet_items=per_bet_items,
        callout_items=callout_items,
        source_items=source_items,
        next_week_items=next_week_items,
        watch_market=_html.escape(watch.get("market") or "—"),
        watch_why=_html.escape(watch.get("why") or ""),
        report_id=report_id,
    )


def _render_pdf(html: str, output_path: Path) -> bool:
    try:
        from weasyprint import HTML  # type: ignore
    except Exception:
        log.info("weasyprint not installed — skipping PDF render")
        return False
    try:
        HTML(string=html).write_pdf(str(output_path))
        return True
    except Exception as exc:
        log.error("weekly_report: PDF render failed: %s", exc)
        return False


# ── Public entry ────────────────────────────────────────────────────────────


async def build_report_for_user(
    user_id: int,
    period_start: int,
    period_end: int,
) -> dict:
    """Build + persist the weekly report for one user.

    Returns ``{report_id, status, pdf_path, html_excerpt, stats}``.
    Status is ``"ready"`` on success or ``"failed"`` on exception.
    """
    conn = _connect()
    now = int(time.time())
    row = None
    report_id = 0
    try:
        if not _table_exists(conn, "weekly_reports"):
            return {"status": "failed", "error": "weekly_reports table missing"}

        conn.execute(
            "INSERT OR REPLACE INTO weekly_reports "
            "(user_id, period_start, period_end, status, created_at) "
            "VALUES (?,?,?,?,?)",
            (user_id, period_start, period_end, "generating", now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM weekly_reports WHERE user_id=? AND period_start=?",
            (user_id, period_start),
        ).fetchone()
        report_id = int(row["id"]) if row else 0

        best_bets = _load_best_bets(conn, period_start, period_end)
        top_sources = _load_top_sources(conn, period_start, period_end)
        top_signals = _load_top_signals(conn, period_start, period_end)
        next_week_markets = _load_next_week_markets(conn, period_start, period_end)
        stats = _simulated_roi(best_bets)

        # If there's genuinely no data, bail out gracefully rather than
        # generating a near-empty PDF and burning tokens on a Claude call.
        if not best_bets and not top_sources and not next_week_markets:
            conn.execute(
                "UPDATE weekly_reports SET status='skipped', completed_at=?, "
                "error_message=? WHERE id=?",
                (int(time.time()), "no data for the week", report_id),
            )
            conn.commit()
            return {"status": "skipped", "report_id": report_id}

        payload = {
            "period_start": period_start, "period_end": period_end,
            "best_bets": best_bets[:12],
            "top_sources": top_sources,
            "top_signals": top_signals,
            "next_week_markets": next_week_markets,
        }
        narrative = await _call_claude_narrative(payload) or {
            "executive_summary": "Claude unavailable this run — showing stats only.",
            "per_bet_analysis": [],
            "callouts": [],
            "one_to_watch": {},
        }

        period_label = (
            f"{_dt.datetime.utcfromtimestamp(period_start).strftime('%d %b')} – "
            f"{_dt.datetime.utcfromtimestamp(period_end).strftime('%d %b %Y')}"
        )
        html = _render_html(
            period_label, stats, narrative,
            {"top_sources": top_sources, "next_week_markets": next_week_markets},
            report_id,
        )
        pdf_path = REPORTS_DIR / f"user-{user_id}-week-{period_start}.pdf"
        pdf_rendered = _render_pdf(html, pdf_path)

        excerpt = html[:8000]
        conn.execute(
            "UPDATE weekly_reports SET status=?, pdf_path=?, html_excerpt=?, "
            "completed_at=? WHERE id=?",
            (
                "ready" if pdf_rendered else "ready_html_only",
                str(pdf_path) if pdf_rendered else None,
                excerpt,
                int(time.time()),
                report_id,
            ),
        )
        conn.commit()
        return {
            "report_id": report_id,
            "status": "ready" if pdf_rendered else "ready_html_only",
            "pdf_path": str(pdf_path) if pdf_rendered else None,
            "html_excerpt": excerpt,
            "stats": stats,
        }
    except Exception as exc:
        log.exception("weekly_report: build failed for user %d", user_id)
        try:
            conn.execute(
                "UPDATE weekly_reports SET status='failed', error_message=?, "
                "completed_at=? WHERE id=?",
                (str(exc)[:500], int(time.time()), report_id),
            )
            conn.commit()
        except Exception:
            pass
        return {"status": "failed", "error": str(exc), "report_id": report_id}
    finally:
        conn.close()
